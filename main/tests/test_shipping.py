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

    def test_the_rates_come_from_settings_not_the_call_site(self):
        # The whole point of the setting: a test-mode and a live deployment
        # need different ids, so they cannot be pinned in payments.py.
        self.assertIsInstance(settings.STRIPE_SHIPPING_RATES, list)
        source = Path(inspect.getfile(Payments)).read_text()
        self.assertNotIn("shr_0M", source)
