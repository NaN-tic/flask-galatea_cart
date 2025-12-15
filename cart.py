import csv
import codecs
import uuid
from flask import Blueprint, render_template, current_app, abort, g, url_for, \
    flash, redirect, session, request, jsonify
from galatea.tryton import tryton
from galatea.csrf import csrf
from galatea.utils import thumbnail
from galatea.helpers import login_required, customer_required
from flask_babel import gettext as _, ngettext
from flask_login import current_user
from trytond.transaction import Transaction
from trytond.exceptions import UserError
from trytond.modules.sale_stock_quantity.exceptions import StockQuantityError
from werkzeug.utils import secure_filename
from .forms import SaleForm, PartyForm, ShipmentAddressForm, InvoiceAddressForm
from decimal import Decimal
from emailvalid import check_email
import stdnum.eu.vat as vat

ALLOWED_EXTENSIONS = ['csv']
try:
    import openpyxl
    ALLOWED_EXTENSIONS.append('xlsx')
except:
    pass

cart = Blueprint('cart', __name__, template_folder='templates')

GALATEA_WEBSITE = current_app.config.get('TRYTON_GALATEA_SITE')
SHOP = current_app.config.get('TRYTON_SALE_SHOP')
SHOPS = current_app.config.get('TRYTON_SALE_SHOPS')
DELIVERY_INVOICE_ADDRESS = current_app.config.get('TRYTON_SALE_DELIVERY_INVOICE_ADDRESS', True)
CART_ANONYMOUS = current_app.config.get('TRYTON_CART_ANONYMOUS', True)
CART_CROSSSELLS = current_app.config.get('TRYTON_CART_CROSSSELLS', True)
LIMIT_CROSSELLS = current_app.config.get('TRYTON_CATALOG_LIMIT_CROSSSELLS', 10)
MINI_CART_CODE = current_app.config.get('TRYTON_CATALOG_MINI_CART_CODE', False)
SALE_KIT = current_app.config.get('TRYTON_SALE_KIT', False)
SALE_RULE = current_app.config.get('TRYTON_SALE_RULE', False)
REDIRECT_TO_PAYMENT_GATEWAY = current_app.config.get('REDIRECT_TO_PAYMENT_GATEWAY', False)
GALATEA_CART_FILE = current_app.config.get('TRYTON_GALATEA_CART_FILE', False)
GALATEA_CART_FILE_LOGIN = current_app.config.get('TRYTON_GALATEA_CART_FILE_LOGIN', True)
GALATEA_CART_FILE_FOUND_LIMIT = current_app.config.get('TRYTON_GALATEA_CART_FILE_FOUND_LIMIT')
SALE_STATE_EXCLUDE = current_app.config.get('TRYTON_SALE_STATE_EXCLUDE', [])

Date = tryton.pool.get('ir.date')
Website = tryton.pool.get('galatea.website')
GalateaUser = tryton.pool.get('galatea.user')
Template = tryton.pool.get('product.template')
Product = tryton.pool.get('product.product')
Shop = tryton.pool.get('sale.shop')
Carrier = tryton.pool.get('carrier')
CarrierSelection = tryton.pool.get('carrier.selection')
Party = tryton.pool.get('party.party')
Address = tryton.pool.get('party.address')
Sale = tryton.pool.get('sale.sale')
SaleLine = tryton.pool.get('sale.line')
Country = tryton.pool.get('country.country')
Subdivision = tryton.pool.get('country.subdivision')
PaymentType = tryton.pool.get('account.payment.type')

PRODUCT_TYPE_STOCK = ['goods', 'assets']


class Cart(object):
    '''
    This object is used to hold the settings used for sale configuration.
    '''
    def __init__(self, app=None):
        self.sale_form = SaleForm
        self.party_form = PartyForm
        self.shipment_address_form = ShipmentAddressForm
        self.invoice_address_form = InvoiceAddressForm

        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        if not hasattr(app, 'extensions'):
            app.extensions = {}
        app.extensions['Cart'] = self

@cart.route('/carriers', methods=['GET'], endpoint="carriers")
@tryton.transaction()
def carriers(lang):
    '''Return all carriers (JSON)'''
    address_id = request.args.get('address', None)
    postal_code = request.args.get('postal_code', None)
    country = request.args.get('country', None)
    untaxed = request.args.get('untaxed', None)
    tax = request.args.get('tax', None)
    total = request.args.get('total', None)
    payment = request.args.get('payment', None)
    customer = session.get('customer', None)

    shop = Shop(SHOP)
    decimals = "%0."+str(shop.currency.digits)+"f" # "%0.2f" euro

    if country is not None:
        try:
            country = Country(int(country))
        except ValueError:
            country = None

    carriers = Sale.get_esale_carriers(
        shop=shop,
        party=Party(customer) if customer else None,
        untaxed=Decimal(untaxed) if untaxed else 0,
        tax=Decimal(tax) if tax else 0,
        total=Decimal(total) if total else 0,
        payment=int(payment) if payment else None,
        address_id=int(address_id) if address_id else None,
        postal_code=postal_code,
        country=country,
        )

    return jsonify(result=[{
        'id': c['carrier'].id,
        'name': c['carrier'].rec_name,
        'price':  float(Decimal(decimals % c['price'])),
        'price_w_tax': float(Decimal(decimals % c['price_w_tax'])),
        'currency': shop.currency.symbol,
        } for c in carriers])

@cart.route('/json/my-cart', methods=['GET', 'PUT'], endpoint="my-cart")
@tryton.transaction()
def my_cart(lang):
    '''All Carts JSON'''
    items = []

    shop = Shop(SHOP)
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
        ('type', '=', 'line'),
        ]
    if session.get('user'): # login user
        domain.append(['OR',
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )

    lines = SaleLine.search(domain)

    decimals = "%0."+str(shop.currency.digits)+"f" # "%0.2f" euro
    for line in lines:
        img = line.product.template.esale_default_images
        image = current_app.config.get('BASE_IMAGE')
        if img.get('small'):
            thumbname = img['small']['name']
            filename = img['small']['digest']
            image = thumbnail(filename, thumbname, '200x200')
        items.append({
            'id': line.id,
            'name': line.product.code if MINI_CART_CODE else line.product.rec_name,
            'url': url_for('catalog.product_'+g.language, lang=g.language,
                slug=line.product.template.esale_slug),
            'quantity': line.quantity,
            'unit_price': float(Decimal(decimals % line.unit_price)),
            'unit_price_w_tax': float(Decimal(decimals % line.unit_price_w_tax)),
            'untaxed_amount': float(Decimal(decimals % line.amount)),
            'amount_w_tax': float(Decimal(decimals % line.amount_w_tax)),
            'image': image,
            })

    return jsonify(result={
        'currency': shop.currency.symbol,
        'items': items,
        })

@cart.route("/confirm/", methods=["POST"], endpoint="confirm")
@tryton.transaction()
def confirm(lang):
    '''Confirm and create a sale'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    shop = Shop(SHOP)
    data = request.form

    party = session.get('customer')
    if not party and not CART_ANONYMOUS:
        flash(_('Please login in to continue the checkout.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))

    form_sale = current_app.extensions['Cart'].sale_form()
    invoice_address = request.form.get('invoice_address')
    invoice_address = invoice_address if invoice_address != 'None' else None
    shipment_address = request.form.get('shipment_address')
    shipment_address = shipment_address if shipment_address != 'None' else None

    # Lines
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
        ('type', '=', 'line'),
        ]
    if session.get('user'): # login user
        domain.append(['OR',
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    lines = SaleLine.search(domain)
    if not lines:
        flash(_('There are not products in your cart.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))

    # Party
    if party:
        party = Party(party)
    else:
        email = data.get('invoice_email') or data.get('shipment_email')
        vat_country = data.get('vat_country', '')
        vat_code = data.get('vat_code', '')

        if not check_email(email):
            flash(_('Email "{email}" is not valid.').format(
                email=email), 'danger')
            return redirect(url_for('.cart', lang=g.language))

        if vat_country and vat_code:
            vat_code = '%s%s' % (vat_country.upper(), vat_code)
            if not vat.is_valid(vat_code):
                flash(_('We found some errors in your VAT. ' \
                    'Try again or contact us.'), 'danger')
                return redirect(url_for('.cart', lang=g.language))

        form_party = current_app.extensions['Cart'].party_form()
        form_party.load()
        if not form_party.validate_on_submit():
            errors = [_('We found some errors in your party data:')]
            for k, v in form_party.errors.items():
                errors.append('%s: %s' % (getattr(form_party, k).label.text, ', '.join(v)))
            flash(errors, 'danger')

            return redirect(url_for('.cart', lang=g.language))

        values = form_party.get_party()
        party = Party.esale_create_party(shop, values)
        session['customer'] = party.id

    # Invoice Address
    if request.form.get('invoice_id'):
        invoice_address = Address(request.form.get('invoice_id'))
    else:
        form_invoice_address = current_app.extensions['Cart'].invoice_address_form(
            invoice_country=request.form.get('invoice_country'),
            invoice_subdivision=request.form.get('invoice_subdivision'))
        form_invoice_address.invoice_id.data = ''
        form_invoice_address.load()

        if not form_invoice_address.validate_on_submit():
            errors = [_('We found some errors in your invoice address data:')]
            for k, v in form_invoice_address.errors.items():
                errors.append('%s: %s' % (getattr(form_invoice_address, k).label.text, ', '.join(v)))
            flash(errors, 'danger')
            return redirect(url_for('.cart', lang=g.language))
        delivery = False if shipment_address != 'invoice-address' else True
        values = form_invoice_address.get_address(delivery=delivery)
        invoice_address = Address.esale_create_address(
            shop, party, values, type='invoice')

    # Shipment Address
    if request.form.get('shipment_id'):
        shipment_address = Address(request.form.get('shipment_id'))
    elif shipment_address == 'invoice-address':
        shipment_address = invoice_address
    else:
        form_shipment_address = current_app.extensions['Cart'].shipment_address_form()
        form_shipment_address.shipment_id.data = '' # None
        form_shipment_address.load()

        if not form_shipment_address.validate_on_submit():
            errors = [_('We found some errors in your delivery address data:')]
            for k, v in form_shipment_address.errors.items():
                errors.append('%s: %s' % (getattr(form_shipment_address, k).label.text, ', '.join(v)))
            flash(errors, 'danger')
            return redirect(url_for('.cart', lang=g.language))

        values = form_shipment_address.get_address()
        if not invoice_address:
            values['invoice'] = True
        shipment_address = Address.esale_create_address(
            shop, party, values, type='delivery')

    # explode sale kit
    if SALE_KIT:
        to_explode = [line for line in lines if line.product.kit and line.product.explode_kit_in_sales]
        if to_explode:
            kit_lines = SaleLine.explode_kit(to_explode)
            if kit_lines:
                lines.extend(kit_lines)

    if website.esale_stock:
        for line in lines:
            # checkout stock available
            if line.product.type not in PRODUCT_TYPE_STOCK:
                continue
            if website.esale_stock_qty == 'forecast_quantity':
                quantity = line.product.esale_forecast_quantity
            else:
                quantity = line.product.esale_quantity
            if not (line.quantity > 0 and line.quantity <= quantity):
                flash(_('Not enought stock for the product "{product}" (maximun: {quantity} units).').format(
                    product=line.product.rec_name, quantity=quantity), 'danger')
                return redirect(url_for('.cart', lang=g.language))

    sale = form_sale.get_sale(party=party, lines=lines, step='confirm')
    if invoice_address:
        sale.invoice_address = invoice_address
    if shipment_address:
        sale.shipment_address = shipment_address

    # Apply rules
    if SALE_RULE:
        with Transaction().set_context({'apply_rule': False}):
            sale.coupon = request.form.get('coupon', None)
            rule_lines = sale.apply_rule()
            if rule_lines:
                sale.lines += tuple(rule_lines,)

    # prevalidate + save sale
    try:
        sale.pre_validate()
        sale.save()
    except UserError as e:
        current_app.logger.info(e)
        return redirect(url_for('.cart', lang=g.language))
    except Exception as e:
        current_app.logger.info(e)
        flash(_('We found some errors when confirm your sale.' \
            'Try again or contact us.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))
    except:
        flash(_('We found some errors when confirm your sale.' \
            'Try again or contact us.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))

    with Transaction().set_context(_skip_warnings=True):
        # Convert draft to quotation
        try:
            Sale.quote([sale])
        except StockQuantityError as e:
            current_app.logger.info(e)
            flash(str(e), 'danger')
            sale_redirect = 'sale.sale' if 'draft' not in SALE_STATE_EXCLUDE else '.cart'
            return redirect(url_for(sale_redirect, lang=g.language))
        except UserError as e:
            current_app.logger.info(e)
            flash(_('We found some errors when quote your sale #%s. Contact Us.' % sale.id), 'danger')
            sale_redirect = 'sale.sale' if 'draft' not in SALE_STATE_EXCLUDE else '.cart'
            return redirect(url_for(sale_redirect, lang=g.language))
        except Exception as e:
            current_app.logger.info(e)
            flash(_('We found some errors when quote your sale #%s. Contact Us.' % sale.id), 'danger')
            sale_redirect = 'sale.sale' if 'draft' not in SALE_STATE_EXCLUDE else '.cart'
            return redirect(url_for(sale_redirect, lang=g.language))

    if current_app.debug:
        current_app.logger.info('Sale. Create sale %s' % sale.id)

    flash(_('Successfully created a new order.'), 'success')

    if (REDIRECT_TO_PAYMENT_GATEWAY and
            sale.payment_type and sale.payment_type.esale_code):
        return render_template('payment.html', sale=sale)

    return redirect(url_for('sale.sale', lang=g.language, id=sale.id))

@csrf.exempt
@cart.route("/add/", methods=["POST"], endpoint="add")
@tryton.transaction()
def add(lang):
    '''Add product item cart'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    cursor = Transaction().connection.cursor()

    if session.get('customer'):
        lock_id = ('1%%0%sd' % 7 ) % int(str(session['customer'])[:7])
    else:
        lock_id = int('2'+str(uuid.UUID(session.sid).int)[:7])

    cursor.execute("SELECT * FROM pg_try_advisory_xact_lock(%s)", (lock_id,))
    res = cursor.fetchone()
    if not res[0]:
        if request.is_json:
            return jsonify(result=False, messages={})
        else:
            return redirect(url_for('.cart', lang=g.language))

    to_create = []
    to_update = []
    to_remove = []
    to_remove_products = [] # Products in older cart and don't sell

    # Convert form values to dict values {'id': 'qty'}
    values = {}
    codes = []

    default_line = SaleLine.default_get(SaleLine._fields.keys(),
            with_rec_name=False)

    # json request
    if request.is_json:
        for data in request.json:
            if data.get('name'):
                prod = data.get('name').split('-')
                if not len(prod) == 2:
                    continue
                qty = data.get('value') or 1
                try:
                    qty = float(qty)
                except ValueError:
                    qty = 1
                # in case qty is 0, not add/update line
                if qty == 0:
                    continue
                try:
                    values[int(prod[1])] = qty
                except ValueError:
                    values[prod[1]] = qty
                    codes.append(prod[1])

        if not values:
            return jsonify(result=False)
    # post request
    else:
        for k, v in request.form.items():
            prod = k.split('-')
            if not len(prod) == 2:
                continue
            if prod[0] == 'product':
                try:
                    qty = float(v)
                except ValueError:
                    flash(_('You try to add no numeric quantity. ' \
                        'The request has been stopped.'))
                    return redirect(url_for('.cart', lang=g.language))
                # in case qty is 0, not add/update line
                if qty == 0:
                    continue
                try:
                    values[int(prod[1])] = qty
                except ValueError:
                    values[prod[1]] = qty
                    codes.append(prod[1])

    # transform product code to id
    if codes:
        products = Product.search([
            ('code', 'in', codes),
            ('salable', '=', True),
            ])
        # reset dict
        vals = values.copy()
        values = {}

        for k, v in vals.items():
            for prod in products:
                if prod.code == k:
                    values[prod.id] = v
                    break

    # Remove items in cart
    removes = request.form.getlist('remove')

    # Search current cart by user or session
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
        ('type', '=', 'line'),
        ]
    if session.get('user'): # login user
        domain.append(['OR',
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    lines = SaleLine.search(domain)

    party = None
    if session.get('customer'):
        party = Party(session.get('customer'))

    form_sale = current_app.extensions['Cart'].sale_form()
    sale = form_sale.get_sale(party=party, step='add')

    # Products Current Cart (products available in sale.cart)
    products_in_cart = [l.product.id for l in lines]

    domain = [
        ('id', 'in', [k for k, _ in values.items()]),
        ('template.esale_available', '=', True),
        ('template.esale_active', '=', True),
        ('template.shops', 'in', [SHOP]),
        ]
    if session.get('hidden_products'):
        domain += [('template.id', 'not in', session.get('hidden_products'))]
    # Get products to add
    products_by_id = dict((p.id, p) for p in Product.search(domain))

    # Delete products data
    if removes:
        for remove in removes:
            for line in lines:
                try:
                    if line.id == int(remove):
                        to_remove.append(line)
                        break
                except:
                    flash(_('You try to remove no numeric cart. ' \
                        'The request has been stopped.'))
                    return redirect(url_for('.cart', lang=g.language))

    # Add/Update products data
    for product_id, qty in values.items():
        product = products_by_id.get(product_id)
        if not product or not product.add_cart:
            continue

        # Add cart if have stock
        if website.esale_stock:
            if website.esale_stock_qty == 'forecast_quantity':
                quantity = product.esale_forecast_quantity
            else:
                quantity = product.esale_quantity
            if product.type in PRODUCT_TYPE_STOCK and not (quantity > 0 and qty <= quantity):
                flash(_('Not enough stock for the product "{product}" (maximun: {quantity} units).').format(
                    product=product.rec_name, quantity=quantity), 'danger')
                continue

        context = {}
        context['customer'] = session.get('customer', None)
        if party and getattr(party, 'sale_price_list'):
            context['price_list'] = party.sale_price_list.id if party.sale_price_list else None
        with Transaction().set_context(context):
            products_to_add = dict()
            if SALE_KIT and product.kit and not product.kit_fixed_list_price:
                kit_lines = list(product.kit_lines)
                while kit_lines:
                    kit_line = kit_lines.pop(0)
                    products_to_add[kit_line.product.id] = kit_line.quantity * qty
            else:
                products_to_add[product.id] = qty

            # update or delete lines
            for line in lines:
                if line.product.id in products_to_add:
                    quantity = products_to_add[line.product.id]
                    # allow show update message in case qty == line.quantity
                    if (quantity == line.quantity) or (quantity > 0):
                        line.quantity = quantity
                        line.on_change_quantity()
                        try:
                            line.pre_validate()
                            to_update.extend(([line], line._save_values))
                        except UserError as e:
                            flash(e.message, 'danger')
                    else:
                        # Remove data when qty <= 0
                        to_remove.append(line)
                    del products_to_add[line.product.id]

            # create lines
            for product_id, quantity in products_to_add.items():
                line = SaleLine(**default_line)
                line.sale = sale
                line.party = party
                line.quantity = quantity
                line.product = product_id
                line.shop = SHOP
                if session.get('user', None):
                    line.galatea_user = session['user']
                else:
                    line.sid = session.sid
                line.on_change_product()

                # Create data
                if product.id not in products_in_cart and quantity > 0:
                    line.on_change_quantity()
                    # set sale to none
                    line.sale = None
                    try:
                        line.pre_validate()
                        to_create.append(line._save_values)
                    except UserError as e:
                        flash(e.message, 'danger')

    # Add to remove older products
    if to_remove_products:
        for remove in to_remove_products:
            for line in lines:
                if line.product.id == remove:
                    to_remove.append(line)
                    break

    # Add Cart
    if to_create:
        # compatibility sale kit
        with Transaction().set_context(explode_kit=False):
            SaleLine.create(to_create)
        flash(ngettext(
            '%(num)s product has been added in your cart.',
            '%(num)s products have been added in your cart.',
            len(to_create)), 'success')

    # Update Cart
    if to_update:
        # compatibility sale kit
        with Transaction().set_context(explode_kit=False):
            SaleLine.write(*to_update)
        total = len(to_update)/2
        if to_remove:
            total = total-len(to_remove)
        flash(ngettext(
            '%(num)s product has been updated in your cart.',
            '%(num)s products have been updated in your cart.',
            int(total)), 'success')

    # Delete Cart
    if to_remove:
        SaleLine.delete(to_remove)
        flash(ngettext(
            '%(num)s product has been deleted in your cart.',
            '%(num)s products have been deleted in your cart.',
            len(to_remove)), 'success')

    if request.is_json:
        # Add JSON messages (success, warning)
        success = []
        warning = []
        for f in session.get('_flashes', []):
            if f[0] == 'success':
                success.append(f[1])
            else:
                warning.append(f[1])
        messages = {}
        messages['success'] = ",".join(success)
        messages['warning'] = ",".join(warning)

        session.pop('_flashes', None)
        return jsonify(result=True, messages=messages)
    else:
        return redirect(url_for('.cart', lang=g.language))

@cart.route("/checkout/", methods=["POST"], endpoint="checkout")
@tryton.transaction()
def checkout(lang):
    '''Checkout sale'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    errors = []
    shop = Shop(SHOP)
    countries = [(c.id, c.name) for c in shop.esale_countrys]

    email = request.form.get('invoice_email') or request.form.get('shipment_email')

    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
        ('type', '=', 'line'),
        ]
    if session.get('user'): # login user
        domain.append(['OR',
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    lines = SaleLine.search(domain)
    if not lines:
        flash(_('There are not products in your cart.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))

    # search user same email request
    if not session.get('logged_in') and email:
        users = GalateaUser.search([
            ('email', '=', email),
            ('active', '=', True),
            ('websites', 'in', [GALATEA_WEBSITE]),
            ], limit=1)
        if users:
            flash(_('Your email is already registed user. Please, login in.'), 'danger')
            return redirect(url_for('.cart', lang=g.language))

    form_sale = current_app.extensions['Cart'].sale_form()
    form_sale.load()

    party = None
    if session.get('customer'):
        party = Party(session.get('customer'))

    sale = form_sale.get_sale(party=party, lines=lines, step='checkout')

    if party:
        if session.get('b2b') or hasattr(Party, 'party_sale_payer'):
            sale.party = party
            sale.shipment_party = party
            sale.on_change_shipment_party()
    elif not CART_ANONYMOUS:
        flash(_('Please login in to continue the checkout.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))

    if website.esale_stock:
        for line in lines:
            # checkout stock available
            if line.product.type not in PRODUCT_TYPE_STOCK:
                continue
            if website.esale_stock_qty == 'forecast_quantity':
                quantity = line.product.esale_forecast_quantity
            else:
                quantity = line.product.esale_quantity
            if not (line.quantity > 0 and line.quantity <= quantity):
                flash(_('Not enought stock for the product "{product}" (maximun: {quantity} units).').format(
                    product=line.product.rec_name, quantity=quantity), 'danger')
                return redirect(url_for('.cart', lang=g.language))

    form_party = current_app.extensions['Cart'].party_form()
    form_party.load()
    if not form_party.invoice_address.data:
        form_party.invoice_address.data = sale.invoice_address and str(sale.invoice_address.id)

    vat_country = form_party.vat_country.data
    vat_code = form_party.vat_code.data
    if vat_country and vat_code:
        vat_code = '%s%s' % (vat_country.upper(), vat_code)
        if not vat.is_valid(vat_code):
            flash(_('We found some errors in your VAT. ' \
                'Try again or contact us.'), 'danger')
            return redirect(url_for('.cart', lang=g.language))

    # Invoice Address
    form_invoice_address = current_app.extensions['Cart'].invoice_address_form()
    form_invoice_address.invoice_country.choices = countries

    invoice_address = request.form.get('invoice_address') or 'new-address'
    if invoice_address == 'new-address':
        form_invoice_address.invoice_id.data = '' # None
        form_invoice_address.load()

        invoice_email = None
        if request.form.get('invoice_email'):
            invoice_email = request.form.get('invoice_email')
            if not check_email(invoice_email):
                errors.append(_('Email not valid.'))
        elif session.get('email'):
            invoice_email = session['email']
        if invoice_email:
            form_invoice_address.invoice_email.data = invoice_email

        invoice_country = request.form.get('invoice_country')
        if invoice_country:
            country = Country(invoice_country)
            form_invoice_address.invoice_country.choices = [(country.id, country.name)]
            form_invoice_address.invoice_country.data = country.id

        invoice_subdivision = request.form.get('invoice_subdivision')
        if invoice_subdivision and invoice_subdivision != '0':
            subdivision = Subdivision(invoice_subdivision)
            form_invoice_address.invoice_subdivision.label = subdivision.name
            form_invoice_address.invoice_subdivision.data = subdivision.id
    elif party:
        invoice_address_id = -1
        if isinstance(invoice_address, Address):
            invoice_address_id = invoice_address.id
        elif isinstance(invoice_address, int):
            invoice_address_id = invoice_address
        else:
            try:
                invoice_address_id = int(invoice_address)
            except ValueError:
                pass

        domain = [
            ('id', '=', invoice_address_id),
            ]
        if hasattr(Party, 'party_sale_payer') and party.party_sale_payer:
            domain.append(('party', '=', party.party_sale_payer))
        else:
            domain.append(('party', '=', party))
        addresses = Address.search(domain, limit=1)
        if addresses:
            invoice_address, = addresses
            form_invoice_address.invoice_id.data = str(invoice_address.id)
            form_invoice_address.load(address=invoice_address)
        else:
            invoice_address = None
            errors.append(_('We can not found a related address. '
                'Please, select a new address in Invoice Address'))

    if not form_invoice_address.validate_on_submit():
        errors.append(_('Error when validate the invoice address. '
            'Please, check the invoice address data.'))
        for k, v in form_invoice_address.errors.items():
            errors.append('%s: %s' % (
                getattr(form_invoice_address, k).label.text,
                ', '.join(v)))

    # Shipment Address
    form_shipment_address = current_app.extensions['Cart'].shipment_address_form()
    form_shipment_address.shipment_country.choices = countries

    shipment_address = request.form.get('shipment_address') or 'new-address'

    if shipment_address == 'new-address':
        form_shipment_address.shipment_id.data = '' # None
        form_shipment_address.load()

        shipment_email = None
        if request.form.get('shipment_email'):
            shipment_email = request.form.get('shipment_email')
            if not check_email(shipment_email):
                errors.append(_('Email not valid.'))
        elif session.get('email'):
            shipment_email = session['email']
        if shipment_email:
            form_shipment_address.shipment_email.data = shipment_email
    elif shipment_address == 'invoice-address' and invoice_address:
        form_shipment_address.shipment_id.data = form_invoice_address.invoice_id.data
        if invoice_address == 'new-address':
            form_shipment_address.load(type_='invoice')
        else:
            form_shipment_address.load(address=invoice_address)
    elif party:
        shipment_address_id = -1
        if isinstance(shipment_address, Address):
            shipment_address_id = shipment_address.id
        elif isinstance(shipment_address, int):
            shipment_address_id = shipment_address
        else:
            try:
                shipment_address_id = int(shipment_address)
            except ValueError:
                pass

        addresses = Address.search([
            ('party', '=', party),
            ('id', '=', shipment_address_id),
            ], limit=1)
        if addresses:
            address, = addresses
            form_shipment_address.shipment_id.data = str(address.id)
            form_shipment_address.load(address=address)
        else:
            errors.append(_('We can not found a related address. '
                'Please, select a new address in shipment Address'))

    if not form_shipment_address.validate_on_submit():
        errors.append(_('Error when validate the shipment address. '
            'Please, check the shipment address data.'))
        for k, v in form_shipment_address.errors.items():
            errors.append('%s: %s' % (
                getattr(form_shipment_address, k).label.text,
                ', '.join(v)))

    # Apply rules
    if SALE_RULE:
        with Transaction().set_context({'apply_rule': False}):
            coupon = request.form.get('coupon', None)
            form_sale.coupon.default = coupon
            sale.coupon = coupon
            rule_lines = sale.apply_rule()
            sale.lines += tuple(rule_lines,)
            sale.on_change_lines()

    if request.form.get('carrier'):
        carrier_id = request.form.get('carrier')
        carrier = Carrier(carrier_id)
        form_sale.carrier.label.text = carrier.rec_name

    form_sale.load()

    # Breadcumbs
    breadcrumbs = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }, {
        'slug': url_for('.cart', lang=g.language),
        'name': _('Checkout'),
        }]

    return render_template('checkout.html',
            website=website,
            breadcrumbs=breadcrumbs,
            shop=shop,
            sale=sale,
            errors=errors,
            form_sale=form_sale,
            form_party=form_party,
            form_invoice_address=form_invoice_address,
            form_shipment_address=form_shipment_address,
            )

@cart.route("/", endpoint="cart")
@tryton.transaction()
def cart_list(lang):
    '''Cart by user or session'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    shop = Shop(SHOP)
    countries = [(str(c.id), c.name) for c in shop.esale_countrys]

    # Products and lines
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
        ('type', '=', 'line'),
        ]
    if session.get('user'): # login user
        domain.append(['OR',
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    lines = SaleLine.search(domain)

    # Party and Addresses
    party = None
    addresses = []
    shipment_addresses = []
    invoice_addresses = []
    default_invoice_address = None
    default_shipment_address = None

    user = None
    if session.get('user'):
        user = GalateaUser(session['user'])

        if session.get('customer'):
            party = Party(session['customer'])
            for address in party.addresses:
                addresses.append(address)
                if address.invoice and user.display_invoice_address:
                    invoice_addresses.append(address)
                if address.delivery and user.display_shipment_address:
                    shipment_addresses.append(address)

        if user.invoice_address:
            default_invoice_address = user.invoice_address
            if not user.display_invoice_address:
                invoice_addresses.append(user.invoice_address)
        elif invoice_addresses:
            default_invoice_address = invoice_addresses[0]
        if user.shipment_address:
            default_shipment_address = user.shipment_address
            if not user.display_shipment_address:
                shipment_addresses.append(user.shipment_address)
        elif shipment_addresses:
            default_shipment_address = shipment_addresses[0]

    # Payment Types
    payment_types, default_payment_type = shop.get_esale_payments(party)

    # Carriers
    stockable = Carrier.get_products_stockable([l.product.id for l in lines])
    carriers = []
    default_carrier = None
    if stockable:
        untaxed_amount = Decimal(0)
        tax_amount = Decimal(0)
        total_amount = Decimal(0)
        for line in lines:
            untaxed_amount += line.amount
            tax_amount += line.amount_w_tax - line.amount
            total_amount += line.amount_w_tax

        carriers = Sale.get_esale_carriers(
            shop=shop,
            party=party,
            untaxed=untaxed_amount,
            tax=tax_amount,
            total=total_amount,
            payment=default_payment_type,
            address_id=default_shipment_address,
            postal_code=default_shipment_address.postal_code if default_shipment_address else None,
            country=default_shipment_address.country if default_shipment_address else None,
            )
        if party and hasattr(party, 'carrier'):
            if party.carrier:
                default_carrier = party.carrier
        if not default_carrier and carriers:
            default_carrier = carriers[0]['carrier']

    # Create forms
    form_sale = current_app.extensions['Cart'].sale_form(
        payment_type=default_payment_type.id if default_payment_type else None,
        carrier=default_carrier.id if default_carrier else None)
    form_sale.load()

    invoice_address_choices = [(a.id, a.full_address) for a in invoice_addresses]
    invoice_address_choices.append(('new-address', _('New address')))
    shipment_address_choices = [(a.id, a.full_address) for a in shipment_addresses]
    if DELIVERY_INVOICE_ADDRESS:
        shipment_address_choices.insert(0, ('invoice-address', _('Delivery to Invoice Address')))
    shipment_address_choices.append(('new-address', _('New address')))

    form_party = current_app.extensions['Cart'].party_form(
        vat_country=shop.esale_country.code,
        invoice_address=str(default_invoice_address.id) if default_invoice_address else invoice_address_choices[0][0],
        shipment_address=str(default_shipment_address.id) if default_shipment_address else shipment_address_choices[0][0],
        )
    form_party.load()
    form_party.invoice_address.choices = invoice_address_choices
    form_party.shipment_address.choices = shipment_address_choices

    # Invoice address country options
    form_invoice_address = current_app.extensions['Cart'].invoice_address_form(
        invoice_country=str(shop.esale_country.id) if shop.esale_country else None)
    form_invoice_address.invoice_country.choices = countries
    form_invoice_address.load()

    # Shipment address country options
    form_shipment_address = current_app.extensions['Cart'].shipment_address_form(
        shipment_country=shop.esale_country.id if shop.esale_country else None)
    form_shipment_address.shipment_country.choices = countries
    form_shipment_address.load()

    # Payment types options
    form_sale.payment_type.choices = [(p.id, p.name) for p in payment_types]
    if not default_payment_type and payment_types:
        default_payment_type = payment_types[0]
    if default_payment_type:
        form_sale.payment_type.default = default_payment_type.id

    # Carrier options
    form_sale.carrier.choices = [
        (c['carrier'].id, c['carrier'].rec_name) for c in carriers]
    if default_carrier:
        form_sale.carrier.default = default_carrier.id

    # Create a demo sale
    sale = form_sale.get_sale(party=party, step='list')
    if session.get('b2b') or hasattr(Party, 'party_sale_payer'):
        sale.party = party
        sale.shipment_party = party
        sale.on_change_shipment_party()
    if not sale.party:
        sale.party = party
    sale.invoice_address = default_invoice_address
    sale.shipment_address = default_shipment_address
    sale.payment_type = default_payment_type
    sale.carrier = default_carrier
    sale.lines = lines
    sale.on_change_lines()

    # Cross Sells
    crossells = []
    if CART_CROSSSELLS:
        template_ids = list({l.product.template.id for l in lines})
        templates = Template.browse(template_ids)
        crossells_ids = set()
        for template in templates:
            for crossell in template.esale_crosssells_by_shop:
                crossells_ids.add(crossell.id)
        if crossells_ids:
            with Transaction().set_context(without_special_price=True):
                crossells = Template.browse(list(crossells_ids)[:LIMIT_CROSSELLS])

    session['next'] = url_for('.cart', lang=g.language)

    # Breadcumbs
    breadcrumbs = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }]

    return render_template('cart.html',
            website=website,
            breadcrumbs=breadcrumbs,
            shop=shop,
            form_sale=form_sale,
            form_party=form_party,
            form_invoice_address=form_invoice_address,
            form_shipment_address=form_shipment_address,
            party=party,
            user=user,
            sale=sale,
            crossells=crossells,
            stockable=stockable,
            )

@cart.route("/pending", endpoint="cart-pending")
@login_required
@tryton.transaction()
def cart_pending(lang):
    '''Last cart pending'''
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
        ('type', '=', 'line'),
            ['OR',
                ('party', '=', session['customer']),
                ('galatea_user', '=', session['user']),
            ]
        ]
    lines = SaleLine.search(domain, offset=0, limit=10)

    breadcrumbs = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }, {
        'name': _('Pending'),
        }]

    return render_template('cart-pending.html',
        lines=lines,
        breadcrumbs=breadcrumbs,
    )

@cart.route("/clone/", methods=["POST"], endpoint="clone")
@login_required
@customer_required
@tryton.transaction()
def clone(lang):
    '''Copy Sale Lines to new carts'''
    id = request.form.get('id')
    if not id:
        flash(_('Error when clone. Select a sale to clone.'), "danger")
        return redirect(url_for('.cart', lang=g.language))

    sales = Sale.search([
        ('id', '=', id),
        ('shop', 'in', SHOPS),
        ('party', '=', session['customer']),
        ], limit=1)
    if not sales:
        flash(_('Error when clone. You not have permisions to clone.'), "danger")
        return redirect(url_for('.cart', lang=g.language))

    sale, = sales
    shop = sale.shop

    products = set()
    sale_lines = sale.get_esale_lines()
    for l in sale_lines:
        products.add(l.product.id)

    # Search current carts by user or session
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
        ('type', '=', 'line'),
        ]
    if session.get('user'): # login user
        domain.append(['OR',
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    lines = SaleLine.search(domain)

    # remove products that exist in current cart
    for l in lines:
        if l.product.id in products:
            products.remove(l.product.id)

    party = None
    if session.get('customer'):
        party = Party(session.get('customer'))

    context = {}
    context['customer'] = session.get('customer', None)
    if party and getattr(party, 'sale_price_list'):
        context['price_list'] = party.sale_price_list.id if party.sale_price_list else None
    elif shop.price_list:
        context['price_list'] = shop.price_list.id

    with Transaction().set_context(context):
        to_create = []
        for product_id in products:
            defaults = SaleLine.default_get(SaleLine._fields.keys(), with_rec_name=False)
            line = SaleLine(**defaults)
            line.shop = shop
            line.party = sale.party.id
            line.sid = session.sid
            line.galatea_user = session.get('user', None)
            line.quantity = 1
            line.product = product_id
            line.on_change_product()
            to_create.append(line._save_values)

    if to_create:
        SaleLine.create(to_create)
        flash(ngettext(
            '%(num)s product has been added in your cart.',
            '%(num)s products have been added in your cart.',
            len(to_create)), 'success')

    return redirect(url_for('.cart', lang=g.language))

@csrf.exempt
@cart.route("/cart-file/", methods=["POST"], endpoint="cart-file")
@tryton.transaction()
def cart_file(lang):
    if not GALATEA_CART_FILE:
        abort(404)
    if not current_user.is_authenticated and GALATEA_CART_FILE_LOGIN:
        return current_app.login_manager.unauthorized()

    def allowed_file(filename):
        if '.' in filename:
            for ext in ALLOWED_EXTENSIONS:
                if ext == filename.rsplit('.', 1)[1].lower():
                    return ext

    file = request.files.get('cart-file')

    if file.filename == '':
        flash(_('No selected file'))
        return redirect(url_for('.cart', lang=g.language))

    flines = {}
    to_create = []
    to_update = []
    extension = allowed_file(file.filename)

    if file and extension:
        filename = secure_filename(file.filename)

        # CSV
        if extension == 'csv':
            try:
                stream = codecs.iterdecode(file.stream, 'utf-8')
                rows = csv.reader(stream, dialect=csv.excel)
                next(rows)
            except:
                flash(_('Error reading "{filename}" file.').format(
                    filename=filename), 'danger')
                return redirect(url_for('.cart', lang=g.language))
            for row in rows:
                try:
                    code = str(row[0])
                    quantity = float(row[1])
                    if flines.get(code):
                        flines[code] += quantity
                    else:
                        flines[code] = quantity
                except (ValueError, TypeError):
                    flash(_('Error reading format cells in the "{filename}" file.').format(
                        filename=filename), 'danger')
                    return redirect(url_for('.cart', lang=g.language))
        # XLS
        elif extension == 'xlsx':
            try:
                wb = openpyxl.load_workbook(file)
                sheet = wb.active
            except:
                flash(_('Error reading "{filename}" file.').format(
                    filename=filename), 'danger')
                return redirect(url_for('.cart', lang=g.language))
            for row in sheet.iter_rows(values_only=True, min_row=2):
                try:
                    code = str(row[0])
                    quantity = float(row[1])
                    if flines.get(code):
                        flines[code] += quantity
                    else:
                        flines[code] = quantity
                except (ValueError, TypeError):
                    flash(_('Error reading format cells in the "{filename}" file.').format(
                        filename=filename), 'danger')
                    return redirect(url_for('.cart', lang=g.language))

        # Search current cart by user or session
        domain = [
            ('sale', '=', None),
            ('shop', '=', SHOP),
            ('type', '=', 'line'),
            ('product', '!=', None),
            ]
        if session.get('user'): # login user
            domain.append(['OR',
                ('sid', '=', session.sid),
                ('galatea_user', '=', session['user']),
                ])
        else: # anonymous user
            domain.append(
                ('sid', '=', session.sid),
                )
        lines = dict((l.product.code, l) for l in SaleLine.search(domain))

        codes = [k for k, v in flines.items()]
        codes_upper = [c.upper() for c in codes]
        codes += codes_upper
        codes += [c.lower() for c in codes]
        codes = set(codes)

        domain = [('salable', '=', True)]
        if hasattr(Product, 'customer_code'):
            domain.append(['OR',
                        ('customer_code', 'in', codes),
                        ('code', 'in', codes)])
        else:
            domain.append(('code', 'in', codes))

        products = Product.search(domain)
        products_by_code = dict((p.code.upper(), p) for p in products)
        if hasattr(Product, 'customer_code'):
            products_by_code.update(dict((p.customer_code.upper(), p) for p in products if p.customer_code))

        # check products by code/customer_code
        not_found = []
        if len(codes_upper) != len(products_by_code):
            for code in codes_upper:
                if not products_by_code.get(code):
                    not_found.append(code)
                if (GALATEA_CART_FILE_FOUND_LIMIT
                        and len(not_found) > GALATEA_CART_FILE_FOUND_LIMIT):
                    not_found.append('...')
                    break
            flash(_('Can not found "{not_found}" products in the "{filename}" file.').format(
                not_found= ', '.join(not_found), filename=filename), 'danger')

        party = None
        if session.get('customer'):
            party = Party(session.get('customer'))

        context = {}
        context['customer'] = session.get('customer', None)
        if party and getattr(party, 'sale_price_list'):
            context['price_list'] = party.sale_price_list.id if party.sale_price_list else None
        with Transaction().set_context(context):
            for code, qty in flines.items():
                code = code.upper()
                product = products_by_code.get(code)
                if not product:
                    continue

                if lines.get(code):
                    line = lines.get(code)
                    line.quantity = round(qty, product.sale_uom.digits)
                    line.on_change_quantity()
                    try:
                        line.pre_validate()
                        to_update.extend(([line], line._save_values))
                    except UserError as e:
                        flash(e.message, 'danger')
                        continue
                else:
                    line = SaleLine()
                    defaults = line.default_get(line._fields.keys(), with_rec_name=False)
                    for key in defaults:
                        setattr(line, key, defaults[key])
                    line.party = session.get('customer', None)
                    line.unit = product.sale_uom
                    line.quantity = round(qty, product.sale_uom.digits)
                    line.product = product
                    line.sid = session.sid
                    line.shop = SHOP
                    line.galatea_user = session.get('user', None)
                    line.on_change_product()
                    line.on_change_quantity()
                    try:
                        line.pre_validate()
                        to_create.append(line._save_values)
                    except UserError as e:
                        flash(e.message, 'danger')
                        continue

        if to_create:
            # compatibility sale kit
            with Transaction().set_context(explode_kit=False):
                SaleLine.create(to_create)
            flash(ngettext(
                '%(num)s product has been added in your cart.',
                '%(num)s products have been added in your cart.',
                len(to_create)), 'success')
        if to_update:
            # compatibility sale kit
            with Transaction().set_context(explode_kit=False):
                SaleLine.write(*to_update)
            total = len(to_update)/2
            flash(ngettext(
                '%(num)s product has been updated in your cart.',
                '%(num)s products have been updated in your cart.',
                int(total)), 'success')
    else:
        flash(_('Can not import selected file'))

    return redirect(url_for('.cart', lang=g.language))
