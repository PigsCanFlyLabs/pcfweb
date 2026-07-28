"""Tests for order creation and the checkout success page."""

from unittest import mock

import stripe
from django.contrib.auth.models import User
from django.core import mail
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.test import RequestFactory

from main.models import Cart, CartProduct, Order, OrderItem, Product
from main.tests.base import OrderTestBase


class CheckoutCreatesOrderTest(OrderTestBase):
    """Requirement 1: checkout writes the snapshot the webhook will need."""

    def test_checkout_records_a_pending_order_with_a_line_item_snapshot(self):
        order = self.place_order(product_pk=100, quantity=2)
        product = Product.objects.get(pk=100)

        self.assertEqual(self.checkout_response.status_code, 302)
        self.assertEqual(
            self.checkout_response["Location"],
            "https://checkout.example/session")
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.stripe_session_id, "cs_test_session")
        self.assertEqual(order.currency, "usd")
        self.assertEqual(order.amount_total, product.price * 2)
        self.assertIsNone(order.paid_at)
        self.assertIsNone(order.notified_at)

        item = order.items.get()
        self.assertEqual(item.product_id, 100)
        self.assertEqual(item.product_name, product.name)
        self.assertEqual(item.unit_amount, product.price)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.snapshot_quantity, 2)
        self.assertEqual(item.currency, "usd")
        self.assertEqual(item.price_id, CartProduct.objects.get().price_id)
        self.assertTrue(item.price_id)

    def test_the_order_id_is_sent_to_stripe_as_the_client_reference(self):
        # Without this there is nothing at all tying a Stripe session to
        # anything local.
        order = self.place_order()
        kwargs = self.create_call.call_args.kwargs
        self.assertEqual(kwargs["client_reference_id"], str(order.pk))
        self.assertEqual(kwargs["metadata"], {"order_id": str(order.pk)})

    def test_the_invalid_coupon_retry_differs_only_by_the_discount(self):
        # The retry is a copy of the single parameter dict minus "discounts",
        # so the order id rides along by construction. Pinned because a
        # version that rebuilt the retry parameters separately would silently
        # drop it -- and would also let the two paths drift apart on tax,
        # which is what CheckoutTaxTest guards from the other side.
        self.client.post("/add-to-cart/100/1")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.side_effect = [
                stripe.InvalidRequestError(
                    "No such coupon", "discounts[0][coupon]"),
                mock.Mock(url="https://checkout.example/session",
                          id="cs_after_retry"),
            ]
            self.client.post("/checkout", {"coupon": "coupon_bad"})

        order = Order.objects.get()
        first, retry = [call.kwargs for call in create.call_args_list]
        self.assertEqual(first["discounts"], [{"coupon": "coupon_bad"}])
        self.assertNotIn("discounts", retry)
        self.assertEqual(
            {k: v for k, v in first.items() if k != "discounts"}, retry)
        for params in (first, retry):
            self.assertEqual(params["client_reference_id"], str(order.pk))
            self.assertEqual(params["metadata"], {"order_id": str(order.pk)})
        self.assertEqual(order.stripe_session_id, "cs_after_retry")

    def test_an_empty_cart_checkout_lands_on_an_explanation(self):
        response = self.client.post("/checkout", follow=True)

        self.assertRedirects(response, "/cart")
        self.assertContains(response, "Your cart is empty.")
        self.assertFalse(Order.objects.exists())

    def test_a_logged_in_buyers_order_is_attached_to_them(self):
        user = User.objects.create_user(
            username="buyer", email="buyer@example.com",
            password="hunter2hunter2")
        self.client.force_login(user)

        order = self.place_order()

        self.assertEqual(order.user_id, user.pk)

    def test_an_anonymous_buyers_order_has_no_user(self):
        self.assertIsNone(self.place_order().user_id)

    def test_the_snapshot_outlives_the_product_and_its_price(self):
        # Product.price is edited in place and Stripe Price objects are minted
        # per cart row, so neither can be trusted for a historical order.
        order = self.place_order(product_pk=100, quantity=2)
        product = Product.objects.get(pk=100)
        original_price = product.price

        Product.objects.filter(pk=100).update(price=original_price + 5000)
        Product.objects.filter(pk=100).delete()

        item = order.items.get()
        self.assertIsNone(item.product_id)
        self.assertEqual(item.unit_amount, original_price)
        self.assertEqual(item.product_name, "Learning Spark (1st edition)")
        self.assertEqual(item.total_amount(), original_price * 2)

    def test_checking_out_an_empty_cart_records_nothing(self):
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            response = self.client.post("/checkout")

        self.assertRedirects(response, "/cart")
        create.assert_not_called()
        self.assertFalse(Order.objects.exists())

    def test_a_pwyw_mixed_cart_becomes_one_order_with_two_lines(self):
        # Was refused before an order could be created, because Stripe allows
        # only one line item beside a custom_unit_amount price. It is an
        # ordinary fixed price now, so the bundle is a single checkout.
        self.client.get("/cart")
        cart = Cart.objects.get(cart_id=self.client.session["cart_id"])
        fixed = CartProduct.objects.create(
            cart=cart, product=Product.objects.get(pk=104), quantity=1)
        pwyw = CartProduct.objects.create(
            cart=cart, product=Product.objects.get(pk=106), quantity=1)
        cart.products.add(fixed, pwyw)
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.stripe.com/c/pay/cs_bundle",
                id="cs_bundle")
            response = self.client.post("/checkout")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(create.call_args.kwargs["line_items"]), 2)
        order = Order.objects.get(stripe_session_id="cs_bundle")
        self.assertEqual(order.items.count(), 2)

    def test_a_pwyw_coupon_reaches_stripe_instead_of_being_stripped(self):
        # Was: the coupon was dropped and the buyer told why, because Stripe
        # refuses discounts on a custom_unit_amount price. Verified against
        # the live test API that a fixed price accepts one.
        self.client.post("/add-to-cart/106/1")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.stripe.com/c/pay/cs_coupon",
                id="cs_coupon")
            response = self.client.post("/checkout", {"coupon": "coupon_sale"})

        self.assertEqual(response.status_code, 302)
        params = create.call_args.kwargs
        self.assertEqual(params["discounts"], [{"coupon": "coupon_sale"}])
        order = Order.objects.get(stripe_session_id="cs_coupon")
        success = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")
        self.assertNotContains(
            success,
            "code was removed and checkout will continue without it")

    def test_the_checkout_pages_still_render_a_queued_message(self):
        """A message queued before the redirect to Stripe is shown on return.

        The pay-what-you-want coupon warning was the only thing that queued a
        message during checkout, so removing it would otherwise take the
        rendering added for it out of the suite along with it. The rendering
        is still wanted, so it is asserted against the templates directly
        rather than through the path that no longer produces one.
        """
        notice = "Something worth saying at checkout."
        request = RequestFactory().get("/checkout/cancel")

        for template in ("checkout_cancel.html", "checkout_success.html"):
            with self.subTest(template=template):
                html = render_to_string(
                    template, {"messages": [notice]}, request=request)

                self.assertIn(notice, html)
                self.assertIn('class="alert alert-warning"', html)

    def test_the_checkout_pages_render_nothing_when_there_is_no_message(self):
        # Control: the alert block is conditional, not always on.
        request = RequestFactory().get("/checkout/cancel")

        for template in ("checkout_cancel.html", "checkout_success.html"):
            with self.subTest(template=template):
                html = render_to_string(
                    template, {"messages": []}, request=request)

                self.assertNotIn('class="alert alert-warning"', html)

    def test_a_failed_stripe_checkout_does_not_leave_a_pending_order(self):
        # No session was created, so nothing will ever arrive for this order
        # -- not even checkout.session.expired. It must not sit in the admin
        # as PENDING forever.
        self.client.post("/add-to-cart/100/1")
        with mock.patch("main.payments.stripe.checkout.Session.create",
                        side_effect=RuntimeError("Stripe is down")):
            with self.assertLogs("main.views", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    self.client.post("/checkout")

        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertIsNone(order.stripe_session_id)
        # The snapshot is kept, so it is still auditable.
        self.assertEqual(order.items.count(), 1)

    def test_a_cancelled_checkout_order_cannot_later_be_paid(self):
        self.client.post("/add-to-cart/100/1")
        with mock.patch("main.payments.stripe.checkout.Session.create",
                        side_effect=RuntimeError("Stripe is down")):
            with self.assertLogs("main.views", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    self.client.post("/checkout")
        order = Order.objects.get()

        response = self.deliver(self.event_body(order, id="cs_ghost"))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        # Not assertEqual(outbox, 0): configuring ADMINS also turns on
        # Django's own 500 mail, and the failed checkout above sent one.
        self.assertEqual(self.order_emails(), [])

    def test_multiple_cart_lines_are_all_snapshotted(self):
        self.client.post("/add-to-cart/100/1")
        self.client.post("/add-to-cart/101/3")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/session", id="cs_multi")
            self.client.post("/checkout")

        order = Order.objects.get()
        self.assertEqual(
            sorted((i.product_id, i.quantity) for i in order.items.all()),
            [(100, 1), (101, 3)])
        self.assertEqual(order.amount_total, order.snapshot_subtotal())


class CheckoutSuccessPageTest(OrderTestBase):
    """The success page keeps clearing the cart, but never decides payment."""

    def test_the_cart_is_cleared_on_the_real_redirect_back_from_stripe(self):
        order = self.place_order()
        self.assertTrue(CartProduct.objects.exists())

        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(CartProduct.objects.exists())

    def test_a_bare_get_does_not_clear_the_cart(self):
        # Stripe always substitutes the session id into success_url, so a
        # request without one did not come from the redirect. It could be a
        # cross-site <img> or a prefetch, and emptying a stranger's cart on
        # that is a side effect no unauthenticated GET should have.
        self.place_order()
        self.assertTrue(CartProduct.objects.exists())

        response = self.client.get("/checkout/success")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(CartProduct.objects.exists())

    def test_an_unknown_session_id_does_not_clear_the_cart(self):
        self.place_order()

        response = self.client.get("/checkout/success?session_id=cs_not_ours")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(CartProduct.objects.exists())

    def test_loading_the_success_page_does_not_pay_the_order(self):
        order = self.place_order()

        self.client.get(f"/checkout/success?session_id={order.stripe_session_id}")

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_known_session_id_shows_the_recorded_order(self):
        order = self.place_order()
        self.deliver(self.event_body(order))

        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertContains(response, f"Order #{order.pk}")
        self.assertContains(response, "Learning Spark")
        self.assertContains(response, "Paid")

    def test_an_unknown_session_id_just_renders_the_plain_page(self):
        response = self.client.get("/checkout/success?session_id=cs_nope")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Checkout Successful!")
        self.assertNotContains(response, "Order #")


class OrderModelTest(OrderTestBase):
    def test_the_snapshot_subtotal_sums_the_lines(self):
        order = Order.objects.create()
        OrderItem.objects.create(
            order=order, product_name="A", unit_amount=1000, quantity=2)
        OrderItem.objects.create(
            order=order, product_name="B", unit_amount=250, quantity=3)

        self.assertEqual(order.snapshot_subtotal(), 2750)

    def test_quantities_match_when_stripe_reported_no_subtotal(self):
        order = Order.objects.create()
        self.assertIsNone(order.amount_subtotal)
        self.assertTrue(order.quantities_match())

    def test_display_prices_are_dollars(self):
        order = Order.objects.create(amount_total=123456)
        self.assertEqual(order.total_display_price(), "1234.56")

    def test_an_order_with_no_shipping_address_says_so(self):
        order = Order.objects.create(customer_email="x@example.com")
        self.assertEqual(order.shipping_address_lines(), [])
        self.assertIn("no shipping address", order.notification_body())

    def test_two_orders_cannot_share_a_stripe_session(self):
        Order.objects.create(stripe_session_id="cs_dupe")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(stripe_session_id="cs_dupe")

    def test_pending_orders_may_all_have_no_session_id_yet(self):
        Order.objects.create()
        Order.objects.create()
        self.assertEqual(
            Order.objects.filter(stripe_session_id__isnull=True).count(), 2)
