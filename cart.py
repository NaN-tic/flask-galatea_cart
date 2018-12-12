from flask import Blueprint, render_template, current_app, abort, g, url_for, \
    flash, redirect, session, request, jsonify
from galatea.tryton import tryton
from galatea.csrf import csrf
from galatea.utils import thumbnail
from galatea.helpers import login_required, customer_required
from flask_babel import gettext as _, ngettext
from trytond.transaction import Transaction
from .forms import SaleForm, PartyForm, ShipmentAddressForm, InvoiceAddressForm
from decimal import Decimal
from emailvalid import check_email
import stdnum.eu.vat as vat

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

def set_sale():
    '''Return a sale object with default values and all fields'''
    sale = Sale()

    sale_fields = Sale._fields.keys()
    # add default values in sale
    default_values = Sale.default_get(sale_fields, with_rec_name=False)
    for k in default_values:
        setattr(sale, k, default_values[k])
    # add all sale fields
    for k in sale_fields:
        if not hasattr(sale, k):
            setattr(sale, k, None)

    return sale

@cart.route('/carriers', methods=['GET'], endpoint="carriers")
@tryton.transaction()
def carriers(lang):
    '''Return all carriers (JSON)'''
    address = request.args.get('address', None)
    zip = request.args.get('zip', None)
    country = request.args.get('country', None)
    untaxed = request.args.get('untaxed', None)
    tax = request.args.get('tax', None)
    total = request.args.get('total', None)
    payment = request.args.get('payment', None)
    customer = session.get('customer', None)

    shop = Shop(SHOP)

    carriers = Sale.get_esale_carriers(
        shop=shop,
        party=Party(customer) if customer else None,
        untaxed=Decimal(untaxed) if untaxed else 0,
        tax=Decimal(tax) if tax else 0,
        total=Decimal(total) if total else 0,
        payment=int(payment) if payment else None,
        )

    if address or zip:
        pattern = {}
        if address and customer:
            addresses = Address.search([
                ('party', '=', customer),
                ('id', '=', address),
                ], limit=1)
            if addresses:
                address, = addresses
                zip = address.zip
                country = address.country.id if address.country else None
        if zip:
            pattern['shipment_zip'] = zip
        if country:
            pattern['to_country'] = country

        zip_carriers = CarrierSelection.get_carriers(pattern)
        new_carriers = []
        for c in carriers:
            if c in zip_carriers:
                new_carriers.append(c)
        carriers = new_carriers

    return jsonify(result=[{
        'id': carrier.id,
        'name': carrier.rec_name,
        } for carrier in carriers])

@cart.route('/json/my-cart', methods=['GET', 'PUT'], endpoint="my-cart")
@tryton.transaction()
def my_cart(lang):
    '''All Carts JSON'''
    items = []

    shop = Shop(SHOP)
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
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
    shop = Shop(SHOP)
    data = request.form

    party = session.get('customer')
    if not party and not CART_ANONYMOUS:
        flash(_('Please login in to continue the checkout.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))
    invoice_address = request.form.get('invoice_address')
    invoice_address = invoice_address if invoice_address != 'None' else None
    shipment_address = request.form.get('shipment_address')
    shipment_address = shipment_address if shipment_address != 'None' else None

    # Lines
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
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
        name = data.get('invoice_name') or data.get('shipment_name')
        email = data.get('invoice_email') or data.get('shipment_email')
        vat_country = data.get('vat_country', '')
        vat_number = data.get('vat_number', '')

        if not check_email(email):
            flash(_('Email "{email}" is not valid.').format(
                email=email), 'danger')
            return redirect(url_for('.cart', lang=g.language))

        if vat_country and vat_number:
            vat_code = '%s%s' % (vat_country.upper(), vat_number)
            if not vat.is_valid(vat_code):
                flash(_('We found some errors in your VAT. ' \
                    'Try again or contact us.'), 'danger')
                return redirect(url_for('.cart', lang=g.language))

        form_party = PartyForm()
        form_party.invoice_address.choices = [('', '')]
        form_party.shipment_address.choices = [('', '')]
        form_party.name.data = name
        form_party.esale_email.data = email
        form_party.vat_country.data = vat_country
        form_party.vat_number.data = vat_number
        form_party.invoice_address.data = ''
        form_party.shipment_address.data = ''

        if not form_party.validate_on_submit():
            flash(_('We found some errors in your party data. ' \
                'Try again or contact us.'), 'danger')
            return redirect(url_for('.cart', lang=g.language))

        party = Party.esale_create_party(shop, {
            'name': name,
            'esale_email': email,
            'vat_country': vat_country,
            'vat_code': vat_number,
            })
        session['customer'] = party.id

    # Invoice Address
    if invoice_address:
        if request.form.get('invoice_id'):
            invoice_address = Address(request.form.get('invoice_id'))
        else:
            name = data.get('invoice_name')
            street = data.get('invoice_street')
            city = data.get('invoice_city')
            zip = data.get('invoice_zip')
            phone = data.get('invoice_phone')
            email = data.get('invoice_email')

            form_invoice_address = InvoiceAddressForm(
                invoice_country=request.form.get('invoice_country'),
                invoice_subdivision=request.form.get('invoice_subdivision'))

            form_invoice_address.invoice_id.data = ''
            form_invoice_address.invoice_name.data = name
            form_invoice_address.invoice_street.data = street
            form_invoice_address.invoice_zip.data = zip
            form_invoice_address.invoice_city.data = city
            form_invoice_address.invoice_phone.data = phone
            form_invoice_address.invoice_email.data = email

            invoice_country = data.get('invoice_country')
            if invoice_country:
                country = Country(invoice_country)
                form_invoice_address.invoice_country.choices = [(country.id, country.name)]
                form_invoice_address.invoice_country.default = request.form.get('invoice_country')

            invoice_subdivision = data.get('invoice_subdivision')
            if invoice_subdivision:
                subdivision = Subdivision(invoice_subdivision)
                form_invoice_address.invoice_subdivision.choices = [(subdivision.id, subdivision.name)]
                form_invoice_address.invoice_subdivision.default = request.form.get('invoice_subdivision')

            if not form_invoice_address.validate_on_submit():
                flash(_('We found some errors in your invoice address data. ' \
                    'Try again or contact us.'), 'danger')
                return redirect(url_for('.cart', lang=g.language))

            values = {
                'name': name,
                'street': street,
                'city': city,
                'zip': zip,
                'country': country.code,
                'subdivision': subdivision,
                'phone': phone,
                'email': email,
                'fax': None,
                }
            if shipment_address == 'invoice-address':
                values['delivery'] = True
            invoice_address = Address.esale_create_address(
                shop, party, values, type='invoice')

    # Shipment Address
    if shipment_address:
        if request.form.get('shipment_id'):
            shipment_address = Address(request.form.get('shipment_id'))
        elif shipment_address == 'invoice-address':
            shipment_address = invoice_address
        else:
            name = data.get('shipment_name')
            street = data.get('shipment_street')
            city = data.get('shipment_city')
            zip = data.get('shipment_zip')
            phone = data.get('shipment_phone')
            email = data.get('shipment_email')

            form_shipment_address = ShipmentAddressForm()

            form_shipment_address.shipment_id.data = '' # None
            form_shipment_address.shipment_name.data = name
            form_shipment_address.shipment_street.data = street
            form_shipment_address.shipment_zip.data = zip
            form_shipment_address.shipment_city.data = city
            form_shipment_address.shipment_phone.data = phone
            form_shipment_address.shipment_email.data = email

            shipment_country = data.get('shipment_country')
            if shipment_country:
                country = Country(shipment_country)
                form_shipment_address.shipment_country.choices = [(country.id, country.name)]
                form_shipment_address.shipment_country.default = request.form.get('shipment_country')

            shipment_subdivision = data.get('shipment_subdivision')
            if shipment_subdivision:
                subdivision = Subdivision(shipment_subdivision)
                form_shipment_address.shipment_subdivision.choices = [(subdivision.id, subdivision.name)]
                form_shipment_address.shipment_subdivision.default = request.form.get('shipment_subdivision')

            if not form_shipment_address.validate_on_submit():
                flash(_('We found some errors in your shipment address data. ' \
                    'Try again or contact us.'), 'danger')
                return redirect(url_for('.cart', lang=g.language))

            values = {
                'name': name,
                'street': street,
                'city': city,
                'zip': zip,
                'country': country.code,
                'subdivision': subdivision,
                'phone': phone,
                'email': email,
                'fax': None,
                }
            if not invoice_address:
                values['invoice'] = True
            shipment_address = Address.esale_create_address(
                shop, party, values, type='delivery')

    sale = Sale.get_sale_data(party)
    sale.sale_date = Date.today()
    sale.shipment_cost_method = 'order' # force shipment invoice on order
    if invoice_address:
        sale.invoice_address = invoice_address
    if shipment_address:
        sale.shipment_address = shipment_address

    # Payment type
    payment_type = data.get('payment_type')
    if payment_type:
        sale.payment_type = int(payment_type)

    # Comment
    comment = data.get('comment')
    if comment:
        sale.comment = comment

    if session.get('user'): # login user
        sale.galatea_user = session['user']

    # explode sale kit
    if SALE_KIT:
        to_explode = [line for line in lines if line.product.kit and line.product.explode_kit_in_sales]
        if to_explode:
            kit_lines = SaleLine.explode_kit(to_explode)
            if kit_lines:
                lines.extend(kit_lines)

    # Add lines to sale
    sale.lines = lines

    # Carrier
    carrier = data.get('carrier')
    if carrier:
        sale.carrier = int(carrier)
        sale.set_shipment_cost() # add shipment line

    # Apply rules
    if SALE_RULE:
        with Transaction().set_context({'apply_rule': False}):
            sale.coupon = request.form.get('coupon', None)
            rule_lines = sale.apply_rule()
            if rule_lines:
                sale.lines += tuple(rule_lines,)

    # mark to esale
    sale.esale = True

    # overwrite to add custom fields from request form data
    sale.set_esale_sale(data)

    # prevalidate + save sale
    try:
        sale.pre_validate()
        sale.save()
    except Exception as e:
        current_app.logger.warn(e)
        flash(_('We found some errors when confirm your sale.' \
            'Try again or contact us.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))

    # Convert draft to quotation
    try:
        Sale.quote([sale])
    except Exception as e:
        current_app.logger.warn(e)
        flash(_('We found some errors when quote your sale.' \
            'Contact Us.'), 'danger')

    if current_app.debug:
        current_app.logger.info('Sale. Create sale %s' % sale.id)

    flash(_('Successfully created a new order.'), 'success')

    if REDIRECT_TO_PAYMENT_GATEWAY and sale.payment_type.esale_code:
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

    to_create = []
    to_update = []
    to_remove = []
    to_remove_products = [] # Products in older cart and don't sell

    # Convert form values to dict values {'id': 'qty'}
    values = {}
    codes = []

    # json request
    if request.json:
        for data in request.json:
            if data.get('name'):
                prod = data.get('name').split('-')
                try:
                    qty = float(data.get('value'))
                except:
                    qty = 1
                try:
                    values[int(prod[1])] = qty
                except:
                    values[prod[1]] = qty
                    codes.append(prod[1])

        if not values:
            return jsonify(result=False)
    # post request
    else:
        for k, v in request.form.iteritems():
            prod = k.split('-')
            if prod[0] == 'product':
                try:
                    qty = float(v)
                except:
                    flash(_('You try to add no numeric quantity. ' \
                        'The request has been stopped.'))
                    return redirect(url_for('.cart', lang=g.language))
                try:
                    values[int(prod[1])] = qty
                except:
                    values[prod[1]] = qty
                    codes.append(prod[1])

    # transform product code to id
    if codes:
        products = Product.search([('code', 'in', codes)])
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

    # Products Current User Cart (products to send)
    products_current_cart = [k for k,v in values.iteritems()]

    # Search current cart by user or session
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
        ('product.id', 'in', products_current_cart)
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

    # Products Current Cart (products available in sale.cart)
    products_in_cart = [l.product.id for l in lines]

    # Get product data
    products = Product.search([
        ('id', 'in', products_current_cart),
        ('template.esale_available', '=', True),
        ('template.esale_active', '=', True),
        ('template.shops', 'in', [SHOP]),
        ])

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
    for product_id, qty in values.iteritems():
        product = None
        for p in products:
            if p.id == product_id:
                product = p
                break

        if not product or not product.add_cart:
            continue

        # Add cart if have stock
        if website.esale_stock:
            if website.esale_stock_qty == 'forecast_quantity':
                quantity = product.esale_forecast_quantity
            else:
                quantity = product.esale_quantity
            if product.type in PRODUCT_TYPE_STOCK and not (quantity > 0 and qty <= quantity):
                flash(_('Not enought stock for the product "{product}" (maximun: {quantity} units).').format(
                    product=product.rec_name, quantity=quantity), 'danger')
                continue

        context = {}
        context['customer'] = session.get('customer', None)
        if party and getattr(party, 'sale_price_list'):
            context['price_list'] = party.sale_price_list.id if party.sale_price_list else None
        with Transaction().set_context(context):
            line = SaleLine()
            defaults = line.default_get(line._fields.keys(), with_rec_name=False)
            for key in defaults:
                setattr(line, key, defaults[key])
            line.party = session.get('customer', None)
            line.quantity = qty
            line.product = product.id
            line.sid = session.sid
            line.shop = SHOP
            line.galatea_user = session.get('user', None)
            line.on_change_product()

            # Create data
            if product_id not in products_in_cart and qty > 0:
                to_create.append(line._save_values)
            # Update data
            if product_id in products_in_cart:
                for line in lines:
                    if line.product.id == product_id:
                        if qty > 0:
                            line.quantity = qty
                            line.on_change_quantity()
                            to_update.extend(([line], line._save_values))
                        else: # Remove data when qty <= 0
                            to_remove.append(line)
                        break

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
            total), 'success')

    # Delete Cart
    if to_remove:
        SaleLine.delete(to_remove)
        flash(ngettext(
            '%(num)s product has been deleted in your cart.',
            '%(num)s products have been deleted in your cart.',
            len(to_remove)), 'success')

    if request.json:
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

    context = {}
    errors = []
    shop = Shop(SHOP)

    email = request.form.get('invoice_email') or request.form.get('shipment_email')

    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
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

    sale = set_sale()
    sale.shop = shop
    sale.currency = shop.currency
    sale.lines = lines
    sale.on_change_lines()

    party = None
    if session.get('customer'):
        party = Party(session.get('customer'))
        sale.party = party
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

    form_sale = SaleForm()

    form_party = PartyForm()
    form_party.name.data = request.form.get('invoice_name') or request.form.get('shipment_name')
    form_party.esale_email.data = request.form.get('invoice_email') or request.form.get('shipment_email')
    form_party.invoice_address.data = request.form.get('invoice_address')
    form_party.shipment_address.data = request.form.get('shipment_address')

    vat_country = request.form.get('vat_country', '')
    vat_number = request.form.get('vat_number', '')
    if vat_country and vat_number:
        vat_code = '%s%s' % (vat_country.upper(), vat_number)
        if not vat.is_valid(vat_code):
            flash(_('We found some errors in your VAT. ' \
                'Try again or contact us.'), 'danger')
            return redirect(url_for('.cart', lang=g.language))
    form_party.vat_country.data = vat_country
    form_party.vat_number.data = vat_number

    # Payment Type
    if request.form.get('payment_type'):
        payment_type_id = request.form.get('payment_type')

        payment_type = PaymentType(payment_type_id)
        sale.payment_type = payment_type

        form_sale.payment_type.label = payment_type.name
        form_sale.payment_type.choices = [(payment_type_id, payment_type.name)]
        form_sale.payment_type.default = '%s' % payment_type_id

    # Carrier
    if request.form.get('carrier'):
        carrier_id = request.form.get('carrier')
        form_sale.carrier.default = carrier_id

        carrier = Carrier(carrier_id)
        sale.carrier = carrier

        # calculate shipment price
        context['record'] = sale
        context['carrier'] = carrier
        with Transaction().set_context(context):
            carrier_price = carrier.get_sale_price() # return price, currency
        shipment_price = carrier_price[0]

        shipment_line = sale.get_shipment_cost_line(shipment_price)
        shipment_line.unit_price_w_tax = shipment_line.on_change_with_unit_price_w_tax()
        shipment_line.amount_w_tax = shipment_line.on_change_with_amount_w_tax()

        sale.lines += (shipment_line,)
        sale.on_change_lines()

        form_sale.carrier.label = carrier.rec_name
        form_sale.carrier.choices = [(carrier_id, carrier.rec_name)]
        form_sale.carrier.default = '%s' % carrier_id

    # Comment
    form_sale.comment.data = request.form.get('comment')

    # Invoice Address
    form_invoice_address = InvoiceAddressForm(
        invoice_country=request.form.get('invoice_country'),
        invoice_subdivision=request.form.get('invoice_subdivision'))

    invoice_address = request.form.get('invoice_address')
    if invoice_address:
        if invoice_address == 'new-address':
            form_invoice_address.invoice_id.data = '' # None
            form_invoice_address.invoice_name.data = request.form.get('invoice_name')
            form_invoice_address.invoice_street.data = request.form.get('invoice_street')
            form_invoice_address.invoice_zip.data = request.form.get('invoice_zip')
            form_invoice_address.invoice_city.data = request.form.get('invoice_city')
            form_invoice_address.invoice_phone.data = request.form.get('invoice_phone')

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
                form_invoice_address.invoice_country.label = country.name
                form_invoice_address.invoice_country.choices = [(country.id, country.name)]
                form_invoice_address.invoice_country.default = request.form.get('invoice_country')

            invoice_subdivision = request.form.get('invoice_subdivision')
            if invoice_subdivision:
                subdivision = Subdivision(invoice_subdivision)
                form_invoice_address.invoice_subdivision.label = subdivision.name
                form_invoice_address.invoice_subdivision.choices = [(subdivision.id, subdivision.name)]
                form_invoice_address.invoice_subdivision.default = request.form.get('invoice_subdivision')

        elif party:
            addresses = Address.search([
                ('party', '=', party),
                ('id', '=', int(invoice_address)),
                ], limit=1)
            if addresses:
                address, = addresses

                form_invoice_address.invoice_id.data = address.id
                form_invoice_address.invoice_name.data = address.name
                form_invoice_address.invoice_street.data = address.street
                form_invoice_address.invoice_zip.data = address.zip
                form_invoice_address.invoice_city.data = address.city
                form_invoice_address.invoice_email.data = address.email or session['email']
                form_invoice_address.invoice_phone.data = address.phone

                if address.country:
                    form_invoice_address.invoice_country.label = address.country.name
                    form_invoice_address.invoice_country.choices = [(address.country.id, address.country.name)]
                    form_invoice_address.invoice_country.default = '%s' % address.country.id
                if address.subdivision:
                    form_invoice_address.invoice_subdivision.label = address.subdivision.name
                    form_invoice_address.invoice_subdivision.choices = [(address.subdivision.id, address.subdivision.name)]
                    form_invoice_address.invoice_subdivision.default = '%s' % address.subdivision.id
            else:
                errors.append(_('We can not found a related address. ' \
                    'Please, select a new address in Invoice Address'))

        if not form_invoice_address.validate_on_submit():
            errors.append(_('Error when validate the invoice address. ' \
                'Please, check the invoice address data.'))

    # Shipment Address
    form_shipment_address = ShipmentAddressForm(
        shipment_country=request.form.get('shipment_country'),
        invoice_subdivision=request.form.get('shipment_subdivision'))

    shipment_address = request.form.get('shipment_address')
    if shipment_address:
        if shipment_address == 'new-address':
            form_shipment_address.shipment_id.data = '' # None
            form_shipment_address.shipment_name.data = request.form.get('shipment_name')
            form_shipment_address.shipment_street.data = request.form.get('shipment_street')
            form_shipment_address.shipment_zip.data = request.form.get('shipment_zip')
            form_shipment_address.shipment_city.data = request.form.get('shipment_city')
            form_shipment_address.shipment_phone.data = request.form.get('shipment_phone')

            shipment_email = None
            if request.form.get('shipment_email'):
                shipment_email = request.form.get('shipment_email')
                if not check_email(shipment_email):
                    errors.append(_('Email not valid.'))
            elif session.get('email'):
                shipment_email = session['email']
            if shipment_email:
                form_shipment_address.shipment_email.data = shipment_email

            shipment_country = request.form.get('shipment_country')
            if shipment_country:
                country = Country(shipment_country)
                form_shipment_address.shipment_country.label = country.name
                form_shipment_address.shipment_country.choices = [(country.id, country.name)]
                form_shipment_address.shipment_country.default = request.form.get('shipment_country')

            shipment_subdivision = request.form.get('shipment_subdivision')
            if shipment_subdivision:
                subdivision = Subdivision(shipment_subdivision)
                form_shipment_address.shipment_subdivision.label = subdivision.name
                form_shipment_address.shipment_subdivision.choices = [(subdivision.id, subdivision.name)]
                form_shipment_address.shipment_subdivision.default = request.form.get('shipment_subdivision')
        elif shipment_address == 'invoice-address' and invoice_address:
            shipment_address = invoice_address
            form_shipment_address.shipment_id.data = form_invoice_address.invoice_id.data
            form_shipment_address.shipment_name.data = form_invoice_address.invoice_name.data
            form_shipment_address.shipment_street.data = form_invoice_address.invoice_street.data
            form_shipment_address.shipment_zip.data = form_invoice_address.invoice_zip.data
            form_shipment_address.shipment_city.data = form_invoice_address.invoice_city.data
            form_shipment_address.shipment_email.data = form_invoice_address.invoice_email.data
            form_shipment_address.shipment_phone.data = form_invoice_address.invoice_phone.data
            form_shipment_address.shipment_country.label = form_invoice_address.invoice_country.label
            form_shipment_address.shipment_country.choices = form_invoice_address.invoice_country.choices
            form_shipment_address.shipment_country.default = form_invoice_address.invoice_country.default
            form_shipment_address.shipment_subdivision.label = form_invoice_address.invoice_subdivision.label
            form_shipment_address.shipment_subdivision.choices = form_invoice_address.invoice_subdivision.choices
            form_shipment_address.shipment_subdivision.default = form_invoice_address.invoice_subdivision.default
        elif party:
            addresses = Address.search([
                ('party', '=', party),
                ('id', '=', int(shipment_address)),
                ], limit=1)
            if addresses:
                address, = addresses

                form_shipment_address.shipment_id.data = '%s' % address.id
                form_shipment_address.shipment_name.data = address.name
                form_shipment_address.shipment_street.data = address.street
                form_shipment_address.shipment_zip.data = address.zip
                form_shipment_address.shipment_city.data = address.city
                form_shipment_address.shipment_email.data = address.email or session['email']
                form_shipment_address.shipment_phone.data = address.phone

                if address.country:
                    form_shipment_address.shipment_country.label = address.country.name
                    form_shipment_address.shipment_country.choices = [(address.country.id, address.country.name)]
                    form_shipment_address.shipment_country.default = '%s' % address.country.id
                if address.subdivision:
                    form_shipment_address.shipment_subdivision.label = address.subdivision.name
                    form_shipment_address.shipment_subdivision.choices = [(address.subdivision.id, address.subdivision.name)]
                    form_shipment_address.shipment_subdivision.default = '%s' % address.subdivision.id
            else:
                errors.append(_('We can not found a related address. ' \
                    'Please, select a new address in shipment Address'))

        if not form_shipment_address.validate_on_submit():
            errors.append(_('Error when validate the shipment address. ' \
                'Please, check the shipment address data.'))

    # Apply rules
    if SALE_RULE:
        with Transaction().set_context({'apply_rule': False}):
            coupon = request.form.get('coupon', None)
            form_sale.coupon.default = coupon
            sale.coupon = coupon
            rule_lines = sale.apply_rule()
            sale.lines += tuple(rule_lines,)
            sale.on_change_lines()

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

    # Products and lines
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
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
    if session.get('customer'):
        party = Party(session['customer'])
        for address in party.addresses:
            addresses.append(address)
            if address.delivery:
                shipment_addresses.append(address)
            if address.invoice:
                invoice_addresses.append(address)

    default_invoice_address = None
    default_shipment_address = None
    if session.get('user'):
        user = GalateaUser(session['user'])
        if user.invoice_address:
            default_invoice_address = user.invoice_address
        elif invoice_addresses:
            default_invoice_address = invoice_addresses[0]
        if user.shipment_address:
            default_shipment_address = user.shipment_address
        elif shipment_addresses:
            default_shipment_address = shipment_addresses[0]

    # Payment Types
    payment_types = []
    default_payment_type = None
    if shop.esale_payments:
        default_payment_type = shop.esale_payments[0].payment_type
        payment_types = [payment.payment_type for payment in shop.esale_payments]
        if party:
            if hasattr(party, 'customer_payment_type'):
                if party.customer_payment_type:
                    default_payment_type = party.customer_payment_type
    if party and hasattr(party, 'customer_payment_type'):
        customer_payment = party.customer_payment_type
        if customer_payment and not customer_payment in payment_types:
            payment_types.append(customer_payment)

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
            payment=default_payment_type)
        if party and hasattr(party, 'carrier'):
            if party.carrier:
                default_carrier = party.carrier
        if not default_carrier and carriers:
            default_carrier = carriers[0]

    # Create forms
    form_sale = SaleForm(
        payment_type=default_payment_type.id if default_payment_type else None,
        carrier=default_carrier.id if default_carrier else None)

    invoice_address_choices = [(a.id, a.full_address) for a in invoice_addresses]
    invoice_address_choices.append(('new-address', _('New address')))
    shipment_address_choices = [(a.id, a.full_address) for a in shipment_addresses]
    if DELIVERY_INVOICE_ADDRESS:
        shipment_address_choices.insert(0, ('invoice-address', _('Delivery to Invoice Address')))
    shipment_address_choices.append(('new-address', _('New address')))

    form_party = PartyForm(
        vat_country=shop.esale_country.code,
        invoice_address=default_invoice_address.id if default_invoice_address else invoice_address_choices[0][0],
        shipment_address=default_shipment_address.id if default_shipment_address else shipment_address_choices[0][0],
        )
    form_party.invoice_address.choices = invoice_address_choices
    form_party.shipment_address.choices = shipment_address_choices

    # Invoice address country options
    form_invoice_address = InvoiceAddressForm(
        country=shop.esale_country.id)
    countries = [(c.id, c.name) for c in shop.esale_countrys]
    form_invoice_address.invoice_country.choices = countries

    # Shipment address country options
    form_shipment_address = ShipmentAddressForm(
        country=shop.esale_country.id)
    countries = [(c.id, c.name) for c in shop.esale_countrys]
    form_shipment_address.shipment_country.choices = countries

    # Payment types options
    form_sale.payment_type.choices = [(p.id, p.name) for p in payment_types]
    if not default_payment_type and payment_types:
        default_payment_type = payment_types[0]
    if default_payment_type:
        form_sale.payment_type.default = '%s' % default_payment_type.id

    # Carrier options
    form_sale.carrier.choices = [(c.id, c.rec_name) for c in carriers]
    if default_carrier:
        form_sale.carrier.default = '%s' % default_carrier.id

    # Create a demo sale
    sale = set_sale()
    sale.shop = shop
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
            crossells = Template.browse(list(crossells_ids)[:LIMIT_CROSSELLS])

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
        return redirect(url_for('.sales', lang=g.language))

    sales = Sale.search([
        ('id', '=', id),
        ('shop', 'in', SHOPS),
        ('party', '=', session['customer']),
        ], limit=1)
    if not sales:
        flash(_('Error when clone. You not have permisions to clone.'), "danger")
        return redirect(url_for('.sales', lang=g.language))

    sale, = sales

    products = set()
    for l in sale.lines:
        if (l.product and l.product.esale_available and
                (l.shipment_cost == None or l.shipment_cost == 0)):
            products.add(l.product.id)

    # Search current carts by user or session
    domain = [
        ('sale', '=', None),
        ('shop', '=', SHOP),
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

    to_create = []
    for product_id in products:
        line = SaleLine()
        defaults = line.default_get(line._fields.keys(), with_rec_name=False)
        for key in defaults:
            setattr(line, key, defaults[key])
        line.party = sale.party.id
        line.quantity = 1
        line.product = product_id
        line.sid = session.sid
        line.galatea_user = session.get('user', None)
        line.on_change_product()
        line.shop = SHOP
        to_create.append(line._save_values)

    if to_create:
        SaleLine.create(to_create)
        flash(ngettext(
            '%(num)s product has been added in your cart.',
            '%(num)s products have been added in your cart.',
            len(to_create)), 'success')

    return redirect(url_for('.cart', lang=g.language))
