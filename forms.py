from flask_babel import lazy_gettext
from flask_wtf import Form
from wtforms import IntegerField, TextAreaField, TextField, SelectField, \
        RadioField, IntegerField, validators
import stdnum.eu.vat as vat

# VAT Countries
VAT_COUNTRIES = [('', '')]
for country in vat._country_codes:
    VAT_COUNTRIES.append((country, country.upper()))


class SaleForm(Form):
    '''Sale form'''
    payment_type = RadioField(lazy_gettext('Payment Type'))
    carrier = RadioField(lazy_gettext('Carrier'))
    comment = TextAreaField(lazy_gettext('Comment'), [])
    coupon = TextField(lazy_gettext('Coupon'))

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True


class PartyForm(Form):
    "Party form"
    name = TextField(lazy_gettext('Name'), [validators.Required()])
    vat_country = SelectField(lazy_gettext('VAT Country'), choices=VAT_COUNTRIES)
    vat_number = TextField(lazy_gettext('VAT Number'))
    invoice_address = RadioField(lazy_gettext('Invoice Address'))
    shipment_address = RadioField(lazy_gettext('Shipment Address'))
    esale_email = TextField(lazy_gettext('E-mail'), [validators.Required(), validators.Email()])

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True


class ShipmentAddressForm(Form):
    "Shipment Address form"
    shipment_id = TextField('ID')
    shipment_name = TextField(lazy_gettext('Name'), [validators.Required()])
    shipment_street = TextField(lazy_gettext('Street'), [validators.Required()])
    shipment_city = TextField(lazy_gettext('City'), [validators.Required()])
    shipment_zip = TextField(lazy_gettext('Zip'), [validators.Required()])
    shipment_country = SelectField(lazy_gettext('Country'), [validators.Required(),], coerce=int)
    shipment_subdivision = IntegerField(lazy_gettext('Subdivision'))
    shipment_email = TextField(lazy_gettext('E-mail'), [validators.Required(), validators.Email()])
    shipment_phone = TextField(lazy_gettext('Phone'))

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True


class InvoiceAddressForm(Form):
    "Invoice Address form"
    invoice_id = TextField('ID')
    invoice_name = TextField(lazy_gettext('Name'), [validators.Required()])
    invoice_street = TextField(lazy_gettext('Street'), [validators.Required()])
    invoice_city = TextField(lazy_gettext('City'), [validators.Required()])
    invoice_zip = TextField(lazy_gettext('Zip'), [validators.Required()])
    invoice_country = SelectField(lazy_gettext('Country'), [validators.Required(),], coerce=int)
    invoice_subdivision = IntegerField(lazy_gettext('Subdivision'))
    invoice_email = TextField(lazy_gettext('E-mail'), [validators.Required(), validators.Email()])
    invoice_phone = TextField(lazy_gettext('Phone'))

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True
