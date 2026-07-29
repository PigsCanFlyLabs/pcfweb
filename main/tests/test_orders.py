"""Tests for order creation and the checkout success page."""

import contextlib
import threading
import traceback
from unittest import mock

import stripe
from django.contrib.auth.models import User
from django.core import mail
from django.db import IntegrityError, connection as django_connection, transaction
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, TransactionTestCase, override_settings

from main.models import Cart, CartProduct, Order, OrderItem, Product
from main.tests.base import (
    ORDER_TEST_SETTINGS, OrderTestBase, OrderTestMixin,
    OWNER_EMAIL, stripe_signature, WEBHOOK_URL,
    assert_never_cache_response)
from main.views import StripeWebhookView


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

    def test_the_success_page_is_marked_uncacheable(self):
        order = self.place_order()
        self.deliver(self.event_body(order))

        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertEqual(response.status_code, 200)
        assert_never_cache_response(self, response)

    def test_the_cancel_page_is_marked_uncacheable(self):
        response = self.client.get("/checkout/cancel")

        self.assertEqual(response.status_code, 200)
        assert_never_cache_response(self, response)

    def test_an_unknown_session_id_just_renders_the_plain_page(self):
        response = self.client.get("/checkout/success?session_id=cs_nope")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Checkout Successful!")
        self.assertNotContains(response, "Order #")


class CheckoutSuccessReconciliationTest(OrderTestBase):
    """The success page asks Stripe when the webhook has not yet run.

    Every test here mocks only stripe.checkout.Session.retrieve and never
    delivers a webhook, so the order's initial state is always what the
    checkout left behind -- PENDING, with no fulfilment markers set.
    """

    def setUp(self):
        super().setUp()
        # The book asset root for download tests.
        from pathlib import Path
        import tempfile, shutil
        from django.test import override_settings
        from main.tests.base import write_book_archive, EBOOK_STEM
        self._asset_root = Path(tempfile.mkdtemp(prefix="pcfweb-books-")).resolve()
        self.addCleanup(shutil.rmtree, self._asset_root, True)
        self._settings_patch = override_settings(
            BOOK_ASSET_ROOT=str(self._asset_root))
        self._settings_patch.enable()
        self.addCleanup(self._settings_patch.disable)
        write_book_archive(self._asset_root, EBOOK_STEM)

    def _mock_stripe_session(self, order, **overrides):
        """Return a patch object for stripe.checkout.Session.retrieve."""
        session = self.session_payload(order, **overrides)
        return mock.patch(
            "stripe.checkout.Session.retrieve",
            return_value=session)

    def test_when_the_webhook_never_runs_the_page_asks_stripe_and_fulfils(self):
        """The common case this feature exists for: a late or missing webhook."""
        order = self.place_order(product_pk=100, quantity=1)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)

        with self._mock_stripe_session(order):
            response = self.client.get(
                f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIsNotNone(order.paid_at)
        self.assertIsNotNone(order.notified_at)
        self.assertEqual(len(self.order_emails()), 1)

    def test_stripe_reports_unpaid_order_stays_pending(self):
        """A session Stripe says is unpaid must not be treated as paid."""
        order = self.place_order(product_pk=100, quantity=1)

        with self._mock_stripe_session(order, payment_status="unpaid"):
            with self.assertLogs("main.views", level="INFO") as log:
                response = self.client.get(
                    f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertIsNone(order.paid_at)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(
            any("reports payment_status 'unpaid'" in msg for msg in log.output))

    def test_no_payment_required_free_order_reaches_fulfilment(self):
        """$0 pay-what-you-want: no_payment_required must be in the set."""
        order = self.place_order(product_pk=106, quantity=1)

        with self._mock_stripe_session(
                order, payment_status="no_payment_required",
                amount_total=0, amount_subtotal=0,
                total_details={"amount_tax": 0, "amount_shipping": 0}):
            response = self.client.get(
                f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIsNotNone(order.notified_at)
        self.assertIsNotNone(order.digital_delivery_sent_at)
        self.assertEqual(len(self.order_emails()), 1)

    def test_already_paid_order_does_not_call_stripe(self):
        """DoS protection: the Stripe call is skipped when already PAID."""
        order = self.place_order(product_pk=100, quantity=1)
        self.deliver(self.event_body(order))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        mail.outbox.clear()

        with mock.patch(
                "stripe.checkout.Session.retrieve") as retrieve:
            response = self.client.get(
                f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertEqual(response.status_code, 200)
        retrieve.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)

    def test_no_session_id_does_not_call_stripe(self):
        """An empty session_id resolves to no order, so Stripe is never hit."""
        self.place_order()

        with mock.patch(
                "stripe.checkout.Session.retrieve") as retrieve:
            response = self.client.get("/checkout/success")

        self.assertEqual(response.status_code, 200)
        retrieve.assert_not_called()

    def test_nonexistent_session_id_does_not_call_stripe(self):
        """Any session_id that resolves to no local order skips Stripe."""
        self.place_order()

        with mock.patch(
                "stripe.checkout.Session.retrieve") as retrieve:
            response = self.client.get(
                "/checkout/success?session_id=cs_nonexistent")

        self.assertEqual(response.status_code, 200)
        retrieve.assert_not_called()

    def test_stripe_timeout_still_renders_the_page(self):
        """The page must render even when Stripe is unreachable."""
        order = self.place_order(product_pk=100, quantity=1)
        mail.outbox.clear()

        with mock.patch(
                "stripe.checkout.Session.retrieve",
                side_effect=stripe.APIConnectionError("Connection timed out")):
            with self.assertLogs("main.views", level="WARNING") as log:
                response = self.client.get(
                    f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(
            any("could not retrieve Stripe session" in msg
                for msg in log.output))

    def test_stripe_error_still_renders_the_page(self):
        """Any Stripe API error leaves the order untouched and the page up."""
        order = self.place_order(product_pk=100, quantity=1)
        mail.outbox.clear()

        with mock.patch(
                "stripe.checkout.Session.retrieve",
                side_effect=stripe.PermissionError("Invalid API key")):
            with self.assertLogs("main.views", level="WARNING") as log:
                response = self.client.get(
                    f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(
            any("could not retrieve Stripe session" in msg
                for msg in log.output))

    def test_reconciliation_only_runs_when_order_is_pending(self):
        """The Stripe call is the third guard: order exists, order is PENDING."""
        order = self.place_order(product_pk=100, quantity=1)
        # Mark it cancelled -- not PENDING and not PAID.
        Order.objects.filter(pk=order.pk).update(status=Order.Status.CANCELLED)

        with mock.patch(
                "stripe.checkout.Session.retrieve") as retrieve:
            response = self.client.get(
                f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertEqual(response.status_code, 200)
        retrieve.assert_not_called()

    def test_cart_still_cleared_after_successful_reconciliation(self):
        """The existing cart-clearing behaviour is preserved."""
        order = self.place_order(product_pk=100, quantity=1)
        self.assertTrue(CartProduct.objects.exists())

        with self._mock_stripe_session(order):
            self.client.get(
                f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertFalse(CartProduct.objects.exists())

    def test_cart_not_cleared_for_unknown_session(self):
        """A stranger cannot clear the cart with a random session_id."""
        self.place_order()
        self.assertTrue(CartProduct.objects.exists())

        with mock.patch(
                "stripe.checkout.Session.retrieve") as retrieve:
            self.client.get("/checkout/success?session_id=cs_unknown")

        self.assertTrue(CartProduct.objects.exists())
        retrieve.assert_not_called()


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



@override_settings(**ORDER_TEST_SETTINGS)
class CheckoutSuccessConcurrencyTest(OrderTestMixin, TransactionTestCase):
    """A webhook delivery and a success-page reconciliation that overlap.

    The window is real: both paths call Stripe, both check payment_status,
    both attempt a conditional PENDING->PAID UPDATE, and both then try to
    fulfil. Only one can win the PAID transition, and only one can claim
    the fulfilment lease. Every side effect must happen exactly once.
    """

    @contextlib.contextmanager
    def as_a_separate_worker_process(self):
        """Run a request in a separate thread with no shared memory."""
        view_class = StripeWebhookView
        saved = {name: value
                 for name, value in vars(view_class).items()
                 if type(value) is set}
        for name in saved:
            setattr(view_class, name, set())
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(view_class, name, value)

    @staticmethod
    def customer_emails():
        return [m for m in mail.outbox if "Your download" in m.subject]

    def test_one_of_each_email_when_webhook_and_page_race(self):
        """Webhook + success page concurrently: exactly one fulfilment."""
        from pathlib import Path
        import tempfile, shutil
        from main.tests.base import write_book_archive, EBOOK_STEM
        asset_root = Path(tempfile.mkdtemp(prefix="pcfweb-conc-")).resolve()
        self.addCleanup(shutil.rmtree, asset_root, True)
        settings_patch = override_settings(BOOK_ASSET_ROOT=str(asset_root))
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        write_book_archive(asset_root, EBOOK_STEM)

        order = self.place_order(product_pk=106, quantity=1)
        body = self.event_body(order)
        signature = stripe_signature(body)

        # Both paths see the same Stripe session.
        session = self.session_payload(order)

        overlapped = []
        failures = []
        real_fulfil = StripeWebhookView.fulfil_order

        def success_page_worker():
            try:
                with self.as_a_separate_worker_process():
                    c = Client()
                    with mock.patch(
                            "stripe.checkout.Session.retrieve",
                            return_value=session):
                        c.get(
                            f"/checkout/success"
                            f"?session_id={order.stripe_session_id}")
            except Exception:
                failures.append(traceback.format_exc())
            finally:
                django_connection.close()

        def fulfil_and_overlap(webhook_view, order_to_fulfil):
            # Hold the first worker *inside* its fulfilment -- the exact
            # window where its markers are still null -- and run the whole
            # of the success page there.
            if not overlapped:
                overlapped.append(True)
                thread = threading.Thread(target=success_page_worker)
                thread.start()
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive(),
                                 "the success page worker never finished")
            return real_fulfil(webhook_view, order_to_fulfil)

        with mock.patch.object(
                __import__('main.views', fromlist=['StripeWebhookView']
                          ).StripeWebhookView,
                'fulfil_order', fulfil_and_overlap):
            self.deliver(body, signature=signature)

        self.assertEqual(failures, [], "the second worker raised")
        self.assertTrue(overlapped, "the workers never actually overlapped")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        # Exactly one owner email.
        self.assertEqual(len(self.order_emails()), 1)
        # Exactly one download email.
        self.assertEqual(len(self.customer_emails()), 1)
        # The claim is handed back.
        self.assertIsNone(order.fulfilment_claimed_at)