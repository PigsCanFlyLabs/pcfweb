"""Stripe Checkout shipping rates.

Shipping rate ids are livemode-scoped: a shr_... minted in test mode does not
exist under a live key. Stripe rejects the whole session rather than skipping
the unknown rate, so a mismatched id breaks every physical checkout and
nothing else -- which is exactly the sort of failure that reads as a Stripe
outage until someone checks.
"""

import inspect
from pathlib import Path
from unittest import mock

import stripe
from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings

from main.models import Cart, CartProduct, Product
from main.payments import SHIPPING_RATE_ERROR, Payments
from pigscanfly.settings import parse_shipping_rates


class ShippingRateConfigTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _checkout(self):
        product = Product.objects.create(
            name="Physical thing",
            description="Ships in a box.",
            external_product_id="prod_ship",
            price=2500,
            mode=Product.Modes.PAYMENT,
        )
        cart = Cart.objects.create()
        cart_product = CartProduct.objects.create(
            cart=cart, product=product, quantity=1, price_id="price_ship")
        cart.products.add(cart_product)
        return Payments.checkout(self.factory.get("/checkout"), cart)

    @override_settings(STRIPE_SHIPPING_RATES=["shr_one", "shr_two"])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_the_configured_rates_are_the_ones_offered(self, create_session):
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout()

        self.assertEqual(
            create_session.call_args.kwargs["shipping_options"],
            [{"shipping_rate": "shr_one"}, {"shipping_rate": "shr_two"}])

    @override_settings(STRIPE_SHIPPING_RATES=[])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_no_configured_rates_means_no_shipping_options(self, create_session):
        # A session with no shipping_options is valid; one with an empty list
        # is not.
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout()

        self.assertNotIn("shipping_options", create_session.call_args.kwargs)

    @override_settings(STRIPE_SHIPPING_RATES=["shr_from_the_other_mode"])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_an_unknown_rate_id_is_reported_as_a_configuration_problem(
            self, create_session):
        create_session.side_effect = stripe.InvalidRequestError(
            "No such shipping rate: 'shr_from_the_other_mode'",
            "shipping_options[0][shipping_rate]",
            code="resource_missing")

        with self.assertLogs("main.payments", level="ERROR"):
            with self.assertRaises(stripe.InvalidRequestError) as caught:
                self._checkout()

        self.assertIn(SHIPPING_RATE_ERROR, str(caught.exception))
        # Not mistaken for a bad coupon, which would silently retry without
        # the discount and fail the same way a second time.
        self.assertEqual(create_session.call_count, 1)

    def test_ids_are_stripped_not_merely_tested_for_blankness(self):
        # "a, b" is how anyone would write a comma-separated list, and a
        # Secret created from a file arrives with a trailing newline. Stripe
        # matches the id exactly, so " shr_two" is not the rate -- it is a
        # resource_missing that fails every physical checkout.
        self.assertEqual(
            parse_shipping_rates("shr_one, shr_two ,\tshr_three\n"),
            ["shr_one", "shr_two", "shr_three"])

    def test_blank_entries_are_dropped(self):
        # Trailing commas and an all-whitespace value must not become empty
        # ids; Stripe rejects the session rather than ignoring them.
        self.assertEqual(parse_shipping_rates("shr_one,,  ,shr_two,"),
                         ["shr_one", "shr_two"])
        self.assertEqual(parse_shipping_rates("   \n "), [])
        self.assertEqual(parse_shipping_rates(""), [])

    def test_the_shipped_defaults_carry_no_stray_whitespace(self):
        for rate in settings.STRIPE_SHIPPING_RATES:
            with self.subTest(rate=rate):
                self.assertEqual(rate, rate.strip())
                self.assertTrue(rate.startswith("shr_"))

    def test_the_rates_come_from_settings_not_the_call_site(self):
        # The whole point of the setting: a test-mode and a live deployment
        # need different ids, so they cannot be pinned in payments.py.
        self.assertIsInstance(settings.STRIPE_SHIPPING_RATES, list)
        source = Path(inspect.getfile(Payments)).read_text()
        self.assertNotIn("shr_0M", source)


@override_settings(FREE_SHIPPING_ENABLED=True, FREE_SHIPPING_THRESHOLD=4000)
class FreeShippingTest(TestCase):
    """Free shipping once the order total reaches the threshold.

    The rate is built inline (shipping_rate_data) rather than being a fifth
    configured shr_... id, so there is nothing to create in the Dashboard and
    nothing to keep in step between test and live mode. What these pin is that
    the offer the cart page advertises is the offer the session carries: the
    page and Payments.checkout read one method, and a cart that disagrees with
    its own checkout is a promise broken at the till.
    """

    def setUp(self):
        self.factory = RequestFactory()

    def _cart(self, price, quantity=1, **product_kwargs):
        kwargs = {
            "name": "Physical thing",
            "description": "Ships in a box.",
            "external_product_id": "prod_ship",
            "price": price,
            "mode": Product.Modes.PAYMENT,
        }
        kwargs.update(product_kwargs)
        product = Product.objects.create(**kwargs)
        cart = Cart.objects.create()
        cart_product = CartProduct.objects.create(
            cart=cart, product=product, quantity=quantity,
            price_id="price_ship")
        cart.products.add(cart_product)
        return cart

    def _checkout(self, cart):
        return Payments.checkout(self.factory.get("/checkout"), cart)

    @override_settings(STRIPE_SHIPPING_RATES=["shr_one"])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_a_qualifying_cart_is_offered_a_free_rate(self, create_session):
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout(self._cart(4000))

        options = create_session.call_args.kwargs["shipping_options"]
        rate = options[0]["shipping_rate_data"]
        self.assertEqual(rate["fixed_amount"], {"amount": 0, "currency": "usd"})
        # First, because Stripe preselects the first option: a buyer who has
        # earned it should not have to go looking.
        self.assertEqual(options[1], {"shipping_rate": "shr_one"})

    @override_settings(STRIPE_SHIPPING_RATES=["shr_one"])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_the_paid_rates_survive_alongside_it(self, create_session):
        # Free shipping is an extra option, not a replacement: somebody who
        # wants the faster service can still pay for it.
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout(self._cart(9999))

        options = create_session.call_args.kwargs["shipping_options"]
        self.assertEqual(len(options), 2)
        self.assertIn({"shipping_rate": "shr_one"}, options)

    @override_settings(STRIPE_SHIPPING_RATES=["shr_one"])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_a_cart_below_the_threshold_gets_no_free_rate(self, create_session):
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout(self._cart(3999))

        self.assertEqual(
            create_session.call_args.kwargs["shipping_options"],
            [{"shipping_rate": "shr_one"}])

    @override_settings(STRIPE_SHIPPING_RATES=["shr_one"])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_the_threshold_is_met_by_the_whole_cart_not_one_line(
            self, create_session):
        # Two $25 books is a $50 order. Reading the unit price instead of the
        # line total would refuse the offer to exactly the larger orders it
        # exists to encourage.
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout(self._cart(2500, quantity=2))

        options = create_session.call_args.kwargs["shipping_options"]
        self.assertIn("shipping_rate_data", options[0])

    @override_settings(STRIPE_SHIPPING_RATES=["shr_one"])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_an_unbuyable_line_does_not_buy_free_shipping(self, create_session):
        # A noorder line is never billed, so it cannot carry a cart over the
        # threshold -- otherwise a $0 order ships free on the strength of a
        # product we do not sell.
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout(self._cart(9999, noorder=True))

        self.assertNotIn(
            "shipping_rate_data",
            create_session.call_args.kwargs["shipping_options"][0])

    @override_settings(STRIPE_SHIPPING_RATES=["shr_one"],
                       FREE_SHIPPING_ENABLED=False)
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_the_offer_can_be_switched_off(self, create_session):
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout(self._cart(999999))

        self.assertEqual(
            create_session.call_args.kwargs["shipping_options"],
            [{"shipping_rate": "shr_one"}])

    @override_settings(STRIPE_SHIPPING_RATES=["shr_one"])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_the_free_rate_carries_a_tax_code(self, create_session):
        # Automatic tax is on, and a session that computes tax rejects an
        # inline rate with no tax behavior -- which would fail every
        # qualifying checkout while leaving small orders working.
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout(self._cart(4000))

        rate = create_session.call_args.kwargs[
            "shipping_options"][0]["shipping_rate_data"]
        self.assertEqual(rate["tax_code"], "txcd_92010001")
        self.assertIn(rate["tax_behavior"],
                      {"exclusive", "inclusive", "unspecified"})

    @override_settings(STRIPE_SHIPPING_RATES=[])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_free_shipping_alone_is_still_a_shipping_option(
            self, create_session):
        # With no configured paid rates, a qualifying cart still gets the free
        # one -- the "no rates means no shipping_options" rule is about an
        # empty list being invalid, not about suppressing the offer.
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout(self._cart(4000))

        options = create_session.call_args.kwargs["shipping_options"]
        self.assertEqual(len(options), 1)
        self.assertIn("shipping_rate_data", options[0])

    @override_settings(STRIPE_SHIPPING_RATES=["shr_one"])
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_a_digital_cart_is_offered_no_shipping_at_all(self, create_session):
        # Nothing to post, so nothing to make free. Guards against the offer
        # reintroducing the shipping-on-a-PDF bug is_physical_good() fixed.
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout(self._cart(
            4000, delivery_type=Product.DeliveryTypes.DIGITAL))

        self.assertNotIn("shipping_options", create_session.call_args.kwargs)
