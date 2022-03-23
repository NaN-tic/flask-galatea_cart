import stdnum.eu.vat as vat
from flask import current_app, request, session
from galatea.tryton import tryton
from flask_babel import lazy_gettext
from flask_wtf import FlaskForm as Form
from wtforms import (IntegerField, TextAreaField, StringField, SelectField,
        RadioField, validators)
from trytond.transaction import Transaction

Party = tryton.pool.get('party.party')
Address = tryton.pool.get('party.address')
Country = tryton.pool.get('country.country')
Subdivision = tryton.pool.get('country.subdivision')
Sale = tryton.pool.get('sale.sale')
PaymentType = tryton.pool.get('account.payment.type')
Date = tryton.pool.get('ir.date')
Carrier = tryton.pool.get('carrier')
Shop = tryton.pool.get('sale.shop')

SHOP = current_app.config.get('TRYTON_SALE_SHOP')

# VAT Countries
VAT_COUNTRIES = [('', '')]
for country in vat.MEMBER_STATES:
    VAT_COUNTRIES.append((country, country.upper()))


class SaleForm(Form):
    '''Sale form'''
    payment_type = RadioField(lazy_gettext('Payment Type'))
    carrier = RadioField(lazy_gettext('Carrier'))
    comment = TextAreaField(lazy_gettext('Comment'), [])
    coupon = StringField(lazy_gettext('Coupon'))

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True

    def load(self):
        self.comment.data = request.form.get('comment')

        if request.form.get('payment_type'):
            payment_type_id = request.form.get('payment_type')
            payment_type = PaymentType(payment_type_id)
            self.payment_type.label = payment_type.name
            self.payment_type.choices = [(payment_type_id, payment_type.name)]
            self.payment_type.default = payment_type_id

        if request.form.get('carrier'):
            self.carrier.default = request.form.get('carrier')

    def get_sale(self, party=None, lines=[]):
        shop = Shop(SHOP)
        default_values = Sale.default_get(Sale._fields.keys(),
            with_rec_name=False)
        sale = Sale(**default_values)
        sale.esale = True
        sale.on_change_shop()
        sale.warehouse = shop.warehouse
        if session.get('b2b'):
            sale.shipment_party = party
            sale.on_change_shipment_party()
        else:
           sale.party = party
           sale.on_change_party()

        sale.sale_date = Date.today()
        # set shipment invoice on order
        sale.shipment_cost_method = 'order'
        # Payment Type
        if request.form.get('payment_type'):
            payment_type_id = request.form.get('payment_type')
            payment_type = PaymentType(payment_type_id)
            sale.payment_type = payment_type
        # Payment type
        payment_type = request.form.get('payment_type')
        if payment_type:
            sale.payment_type = int(payment_type)
        # Comment
        comment = request.form.get('comment')
        if comment:
            sale.comment = comment
        sale.galatea_user = session.get('user')

        sale.lines = lines
        # not set sale in lines because when confirm could be loop recursion (save)
        if request.endpoint != 'cart.confirm':
            for line in sale.lines:
                line.sale = sale

        # Carrier
        if request.form.get('carrier'):
            # add shipment line
            carrier_id = request.form.get('carrier')
            carrier = Carrier(carrier_id)
            sale.carrier = carrier
            sale.on_change_lines()

            # calculate shipment price
            sale_vals = sale._save_values
            sale_vals['untaxed_amount'] = sale.untaxed_amount
            sale_vals['total_amount'] = sale.total_amount
            if 'lines' in sale_vals:
                del sale_vals['lines']

            context = {}
            context['record'] = sale_vals
            context['record_model'] = 'sale.sale'
            context['carrier'] = carrier
            with Transaction().set_context(context):
                carrier_price = carrier.get_sale_price() # return price, currency

            shipment_price = carrier_price[0]
            shipment_line = sale.get_shipment_cost_line(shipment_price)
            shipment_line.unit_price_w_tax = shipment_line.on_change_with_unit_price_w_tax()
            shipment_line.amount_w_tax = shipment_line.on_change_with_amount_w_tax()

            sale.lines += (shipment_line,)

        extra_lines = sale._get_extra_lines()
        if extra_lines:
            sale.lines += tuple(extra_lines)

        sale.on_change_lines()
        return sale


class PartyForm(Form):
    "Party form"
    name = StringField(lazy_gettext('Name'), [validators.DataRequired()])
    vat_country = SelectField(lazy_gettext('VAT Country'), choices=VAT_COUNTRIES)
    vat_code = StringField(lazy_gettext('VAT Code'))
    invoice_address = RadioField(lazy_gettext('Invoice Address'), [validators.Optional()])
    shipment_address = RadioField(lazy_gettext('Shipment Address'), [validators.Optional()])
    esale_email = StringField(lazy_gettext('E-mail'), [validators.DataRequired(), validators.Email()])

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True

    def load(self):
        self.name.data = request.form.get('invoice_name') or request.form.get('shipment_name')
        self.esale_email.data = request.form.get('invoice_email') or request.form.get('shipment_email')
        self.vat_country.data = request.form.get('vat_country', '')
        self.vat_code.data = request.form.get('vat_code', '')
        self.invoice_address.choices = []
        invoice_address = request.form.get('invoice_address')
        if invoice_address:
            self.invoice_address.data = invoice_address
            self.invoice_address.choices = [(invoice_address, invoice_address)]
        self.shipment_address.choices = []
        shipment_address = request.form.get('shipment_address')
        if shipment_address:
            self.shipment_address.data = shipment_address
            self.shipment_address.choices = [(shipment_address, shipment_address)]
        if not self.vat_country.data:
            self.vat_country.data = request.form.get('vat_country', '')
        self.vat_code.data = request.form.get('vat_code', '')

    def get_party(self):
        # not create object because esale_create_party require some keys
        # that has not defined in party class
        return {
            'name': request.form.get('invoice_name') or request.form.get('shipment_name'),
            'esale_email': request.form.get('invoice_email') or request.form.get('shipment_email'),
            'vat_country': request.form.get('vat_country', ''),
            'vat_code': request.form.get('vat_code', ''),
            }


class ShipmentAddressForm(Form):
    "Shipment Address form"
    shipment_id = StringField('ID')
    shipment_name = StringField(lazy_gettext('Name'))
    shipment_street = StringField(lazy_gettext('Street'), [validators.DataRequired()])
    shipment_city = StringField(lazy_gettext('City'), [validators.DataRequired()])
    shipment_postal_code = StringField(lazy_gettext('Postal Code'), [validators.DataRequired()])
    shipment_country = SelectField(lazy_gettext('Country'), [validators.DataRequired(),], coerce=int)
    shipment_subdivision = IntegerField(lazy_gettext('Subdivision'), [validators.Optional()])
    shipment_email = StringField(lazy_gettext('E-mail'), [validators.DataRequired(), validators.Email()])
    shipment_phone = StringField(lazy_gettext('Phone'))

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True

    def load(self, type_='shipment', address=None):
        self.shipment_name.data = address.party_name if address else request.form.get('%s_name' % type_)
        self.shipment_street.data = address.street if address else request.form.get('%s_street' % type_)
        self.shipment_postal_code.data = address.postal_code if address else request.form.get('%s_postal_code' % type_)
        self.shipment_city.data = address.city if address else request.form.get('%s_city' % type_)
        self.shipment_email.data = session.get('email') or request.form.get('%s_email' % type_)
        self.shipment_phone.data = request.form.get('%s_phone' % type_)

        if address:
            if address.country:
                self.shipment_country.choices = [(address.country.id, address.country.name)]
                self.shipment_country.data = address.country.id
            if address.subdivision:
                self.shipment_subdivision.label = address.subdivision.name
                self.shipment_subdivision.data = address.subdivision.id
            else:
                self.shipment_subdivision.label = ''
                self.shipment_subdivision.data = 0
        else:
            shipment_country = request.form.get('%s_country' % type_)
            if shipment_country:
                country = Country(shipment_country)
                self.shipment_country.choices = [(country.id, country.name)]
                self.shipment_country.data = country.id

            shipment_subdivision = request.form.get('%s_subdivision' % type_)
            if shipment_subdivision and shipment_subdivision != '0':
                subdivision = Subdivision(shipment_subdivision)
                self.shipment_subdivision.label = subdivision.name
                self.shipment_subdivision.data = shipment_subdivision
            else:
                self.shipment_subdivision.label = ''
                self.shipment_subdivision.data = 0

    def get_address(self):
        # return dict to parameter in esale_create_address
        values = {
            'delivery': True,
            'name': request.form.get('shipment_name'),
            'street': request.form.get('shipment_street'),
            'city': request.form.get('shipment_city'),
            'postal_code': request.form.get('shipment_postal_code'),
            }
        country = request.form.get('shipment_country')
        if country:
            values['country'] = int(country)
        subdivision = request.form.get('shipment_subdivision')
        if subdivision:
            values['subdivision'] = int(subdivision)
        if hasattr(Address, 'phone'):
            values['phone'] = request.form.get('shipment_phone')
        if hasattr(Address, 'email'):
            values['email'] = request.form.get('shipment_email')
        return values


class InvoiceAddressForm(Form):
    "Invoice Address form"
    invoice_id = StringField('ID')
    invoice_name = StringField(lazy_gettext('Name'))
    invoice_street = StringField(lazy_gettext('Street'), [validators.DataRequired()])
    invoice_city = StringField(lazy_gettext('City'), [validators.DataRequired()])
    invoice_postal_code = StringField(lazy_gettext('Postal Code'), [validators.DataRequired()])
    invoice_country = SelectField(lazy_gettext('Country'), [validators.DataRequired(),], coerce=int)
    invoice_subdivision = IntegerField(lazy_gettext('Subdivision'), [validators.Optional()])
    invoice_email = StringField(lazy_gettext('E-mail'), [validators.DataRequired(), validators.Email()])
    invoice_phone = StringField(lazy_gettext('Phone'))

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True

    def load(self, address=None):
        self.invoice_name.data = address.party_name if address else request.form.get('invoice_name')
        self.invoice_street.data = address.street if address else request.form.get('invoice_street')
        self.invoice_postal_code.data = address.postal_code if address else request.form.get('invoice_postal_code')
        self.invoice_city.data = address.city if address else request.form.get('invoice_city')
        self.invoice_email.data = session.get('email') or request.form.get('invoice_email')
        self.invoice_phone.data = request.form.get('invoice_phone')

        if address:
            if address.country:
                self.invoice_country.choices = [(address.country.id, address.country.name)]
                self.invoice_country.data = address.country.id
            if address.subdivision:
                self.invoice_subdivision.label = address.subdivision.name
                self.invoice_subdivision.data = address.subdivision.id
            else:
                self.invoice_subdivision.label = ''
                self.invoice_subdivision.data = 0
        else:
            invoice_country = request.form.get('invoice_country')
            if invoice_country:
                country = Country(invoice_country)
                self.invoice_country.choices = [(country.id, country.name)]
                self.invoice_country.data = country.id

            invoice_subdivision = request.form.get('invoice_subdivision')
            if invoice_subdivision and invoice_subdivision != '0':
                subdivision = Subdivision(invoice_subdivision)
                self.invoice_subdivision.label = subdivision.name
                self.invoice_subdivision.data = invoice_subdivision
            else:
                self.invoice_subdivision.label = ''
                self.invoice_subdivision.data = 0

    def get_address(self, delivery=True):
        # return dict to parameter in esale_create_address
        values = {
            'invoice': True,
            'delivery': delivery,
            'name': request.form.get('invoice_name'),
            'street': request.form.get('invoice_street'),
            'city': request.form.get('invoice_city'),
            'postal_code': request.form.get('invoice_postal_code'),
            }
        country = request.form.get('invoice_country')
        if country:
            values['country'] = int(country)
        subdivision = request.form.get('invoice_subdivision')
        if subdivision:
            values['subdivision'] = int(subdivision)
        if hasattr(Address, 'phone'):
            values['phone'] = request.form.get('invoice_phone')
        if hasattr(Address, 'email'):
            values['email'] = request.form.get('invoice_email')
        return values
