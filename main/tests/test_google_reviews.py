"""The Google Customer Reviews opt-in on the checkout success page.

Google's module fails silently in the browser when a required field is
missing, so nothing about a broken integration is visible from the site: the
survey simply never arrives, weeks later, for reasons nobody is told. These
pin the fields it needs, and the cases where the module must not render at
all rather than render with a blank.
"""

import json
import re
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from main.models import (
    MAX_HANDLING_DAYS, MAX_TRANSIT_DAYS, Order, OrderItem, Product)


MERCHANT_ID = "686906834"


@override_settings(THUMBNAIL_DEBUG=False,
                   GOOGLE_CUSTOMER_REVIEWS_MERCHANT_ID=MERCHANT_ID)
class CustomerReviewsOptInTest(TestCase):
    def make_product(self, **fields):
        with mock.patch("main.models.Payments") as payments:
            payments.create_product.return_value = "prod_rev"
            defaults = {
                "name": "A Book",
                "description": "Prose.",
                "price": 1999,
                "cat": Product.Categories.BOOKS,
                "external_product_id": "prod_rev",
                "print_isbn": "9781449358624",
            }
            defaults.update(fields)
            return Product.objects.create(**defaults)

    def make_order(self, product=None, **fields):
        defaults = {
            "stripe_session_id": "cs_test_reviews",
            "status": Order.Status.PAID,
            "customer_email": "buyer@example.com",
            "shipping_country": "US",
            "amount_total": 1999,
            "paid_at": timezone.now(),
        }
        defaults.update(fields)
        order = Order.objects.create(**defaults)
        if product is not None:
            OrderItem.objects.create(
                order=order, product=product, product_name=product.name,
                unit_amount=product.price, quantity=1)
        return order

    def success_html(self, order):
        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def opt_in_config(self, html):
        """The object literal handed to gapi.surveyoptin.render()."""
        match = re.search(
            r"surveyoptin\.render\(\s*(\{.*?\})\s*\);", html, re.DOTALL)
        self.assertIsNotNone(match, "no Customer Reviews opt-in on the page")
        assert match is not None  # for mypy
        # Parsed rather than substring-matched: the module is JavaScript, and
        # a trailing comma or an unquoted value renders a page that looks
        # right and runs nothing.
        return json.loads(match.group(1))

    def test_the_module_carries_every_required_field(self):
        order = self.make_order(self.make_product())

        config = self.opt_in_config(self.success_html(order))

        self.assertEqual(config["merchant_id"], int(MERCHANT_ID))
        self.assertEqual(config["order_id"], str(order.pk))
        self.assertEqual(config["email"], "buyer@example.com")
        self.assertEqual(config["delivery_country"], "US")
        self.assertIn("estimated_delivery_date", config)

    def test_the_delivery_estimate_clears_the_published_window(self):
        # A survey that arrives before the parcel asks the customer to rate a
        # delivery that has not happened.
        product = self.make_product()
        order = self.make_order(product)

        config = self.opt_in_config(self.success_html(order))

        expected = (timezone.localtime(order.paid_at).date()
                    + timedelta(days=MAX_HANDLING_DAYS + MAX_TRANSIT_DAYS))
        self.assertEqual(config["estimated_delivery_date"],
                         expected.strftime("%Y-%m-%d"))

    def test_a_digital_order_is_delivered_the_day_it_is_paid(self):
        # Otherwise every e-book buyer is surveyed a month after they read it.
        product = self.make_product(
            delivery_type=Product.DeliveryTypes.DIGITAL)
        order = self.make_order(
            product, shipping_country="", billing_country="US")

        config = self.opt_in_config(self.success_html(order))

        self.assertEqual(
            config["estimated_delivery_date"],
            timezone.localtime(order.paid_at).date().strftime("%Y-%m-%d"))

    def test_the_products_carry_their_gtins(self):
        order = self.make_order(self.make_product())

        config = self.opt_in_config(self.success_html(order))

        self.assertEqual(config["products"], [{"gtin": "9781449358624"}])

    def test_a_product_with_no_gtin_is_left_out_rather_than_sent_blank(self):
        # "products" is a list of identifiers; an empty string is not one.
        order = self.make_order(self.make_product(print_isbn="", upc=""))

        config = self.opt_in_config(self.success_html(order))

        self.assertNotIn("products", config)

    def test_a_digital_order_falls_back_to_the_billing_country(self):
        # A download collects no shipping address, and the country is
        # required.
        product = self.make_product(
            delivery_type=Product.DeliveryTypes.DIGITAL)
        order = self.make_order(
            product, shipping_country="", billing_country="CA")

        self.assertEqual(
            self.opt_in_config(self.success_html(order))["delivery_country"],
            "CA")

    def test_a_pending_order_renders_no_module(self):
        # Stripe reports the email on the paid session, so a PENDING order has
        # none -- and the module needs one.
        order = self.make_order(
            self.make_product(), status=Order.Status.PENDING,
            customer_email="", paid_at=None)

        with mock.patch.object(
                type(order), "estimated_delivery_date", return_value=None):
            html = self.success_html(order)

        self.assertNotIn("surveyoptin", html)

    def test_no_order_renders_no_module(self):
        response = self.client.get("/checkout/success")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("surveyoptin", response.content.decode())

    @override_settings(GOOGLE_CUSTOMER_REVIEWS_MERCHANT_ID="")
    def test_a_blank_merchant_id_switches_the_module_off(self):
        order = self.make_order(self.make_product())

        self.assertNotIn("surveyoptin", self.success_html(order))

    def test_an_order_with_no_country_renders_no_module(self):
        # Rather than sending "" and having Google reject the submission.
        order = self.make_order(
            self.make_product(), shipping_country="", billing_country="")

        self.assertNotIn("surveyoptin", self.success_html(order))
