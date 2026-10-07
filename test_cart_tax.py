"""Isolated cart view regressions; run with python3 -m unittest test_cart_tax."""
import copy
import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch


def load_cart():
    # Import the real views without initializing Flask or a Tryton database.
    dependencies = {
        name: MagicMock() for name in (
            'flask', 'app_extensions', 'galatea', 'galatea.utils',
            'galatea.helpers', 'flask_babel', 'flask_login', 'trytond',
            'trytond.transaction', 'trytond.exceptions', 'trytond.modules',
            'trytond.modules.sale_stock_quantity',
            'trytond.modules.sale_stock_quantity.exceptions', 'werkzeug',
            'werkzeug.utils', '_cart_test.forms', 'emailvalid', 'stdnum',
            'stdnum.eu', 'stdnum.eu.vat', 'openpyxl')}
    identity = lambda *args, **kwargs: lambda function: function
    dependencies['flask'].Blueprint.return_value.route.side_effect = identity
    dependencies['app_extensions'].tryton.transaction.side_effect = identity
    dependencies['galatea.helpers'].login_required.side_effect = lambda f: f
    dependencies['galatea.helpers'].customer_required.side_effect = lambda f: f
    spec = importlib.util.spec_from_file_location(
        '_cart_test.cart', Path(__file__).with_name('cart.py'))
    module = importlib.util.module_from_spec(spec)
    with patch.dict('sys.modules', dependencies):
        spec.loader.exec_module(module)
    return module


class Line:
    sale = None
    id = 1
    product = SimpleNamespace(id=1)

    def __init__(self, quantity, rate):
        self.quantity = quantity
        self.rate = rate
        self.amount = Decimal('10') * quantity

    @property
    def amount_w_tax(self):
        # Match the requirement introduced by sale_w_tax: no sale, no amount.
        if self.sale is not None and self.quantity:
            return self.amount * (1 + self.rate)


class Sale:
    def __init__(self, party):
        self.party = party
        self.on_change_lines = Mock()
        self.on_change_shipment_party = Mock()
        self.lines = []

    @property
    def lines(self):
        return self._lines

    @lines.setter
    def lines(self, lines):
        # Relational fields may return different instances from search().
        self._lines = [copy.copy(line) for line in lines]


class CartTaxTest(unittest.TestCase):
    def test_cart_carrier_totals_and_rendered_sale(self):
        for logged_in, stockable, quantity, rate in (
                (False, True, 2, Decimal('.21')),
                (True, True, 2, Decimal('.21')),
                (True, True, 2, Decimal(0)),
                (True, True, 0, Decimal('.21')),
                (True, False, 2, Decimal('.21')),
                (False, False, None, Decimal(0))):
            with self.subTest(logged_in=logged_in, stockable=stockable,
                    quantity=quantity, rate=rate):
                cart = load_cart()
                original = [Line(quantity, rate)] if quantity is not None else []
                address = SimpleNamespace(id=5, full_address='Address',
                    postal_code='08001', country=1, invoice=True, delivery=True)
                party = SimpleNamespace(addresses=[address])
                user = SimpleNamespace(display_invoice_address=True,
                    display_shipment_address=True, invoice_address=address,
                    shipment_address=address)
                payment = SimpleNamespace(id=3, name='Transfer')
                carrier = SimpleNamespace(id=4, rec_name='Carrier')
                shop = SimpleNamespace(esale_countrys=[],
                    esale_country=SimpleNamespace(id=1, code='es'),
                    get_esale_payments=Mock(return_value=([payment], payment)))
                sale_model = Mock()
                sale_model.get_esale_carriers.return_value = [{'carrier': carrier}]
                carrier_model = Mock()
                carrier_model.get_products_stockable.return_value = stockable
                line_model = Mock()
                line_model.search.return_value = original
                models = {
                    'galatea.website': Mock(search=Mock(return_value=[object()])),
                    'sale.shop': Mock(return_value=shop),
                    'galatea.user': Mock(return_value=user),
                    'party.party': lambda identifier: party,
                    'sale.sale': sale_model, 'sale.line': line_model,
                    'carrier': carrier_model,
                    }
                cart.tryton.pool.get.side_effect = lambda name: models.get(name, Mock())
                form = Mock()
                form.get_sale.side_effect = lambda party, step: Sale(party)
                extension = Mock()
                extension.sale_form.return_value = form
                cart.current_app = SimpleNamespace(
                    config={'TRYTON_SALE_SHOP': 1, 'TRYTON_GALATEA_SITE': 1,
                        'TRYTON_CART_CROSSSELLS': False},
                    extensions={'Cart': extension})
                cart.session = {'sid': 'test'}
                if logged_in:
                    cart.session.update(user=1, customer=2)
                cart.g = SimpleNamespace(language='es')
                cart._ = lambda text: text
                cart.render_template = Mock()

                cart.cart_list('es')

                rendered = cart.render_template.call_args.kwargs
                sale = rendered['sale']
                self.assertEqual(len(sale.lines), len(original))
                self.assertIs(sale.party, party if logged_in else None)
                self.assertIs(sale.invoice_address, address if logged_in else None)
                self.assertIs(sale.shipment_address, address if logged_in else None)
                self.assertIs(sale.payment_type, payment)
                self.assertIs(sale.carrier, carrier if stockable else None)
                self.assertEqual(form.carrier.data, 4 if stockable else None)
                for line in sale.lines:
                    self.assertIs(line.sale, sale)
                for line in original:
                    self.assertIsNone(line.sale)
                if stockable:
                    totals = sale_model.get_esale_carriers.call_args.kwargs
                    untaxed = Decimal('10') * quantity
                    self.assertEqual(totals['untaxed'], untaxed)
                    self.assertEqual(totals['tax'], untaxed * rate)
                    self.assertEqual(totals['total'], untaxed * (1 + rate))
                    self.assertIs(totals['party'], party if logged_in else None)
                    self.assertIs(totals['address_id'], address if logged_in else None)
                else:
                    sale_model.get_esale_carriers.assert_not_called()
                line_model.save.assert_not_called()
                line_model.write.assert_not_called()
                sale_model.save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
