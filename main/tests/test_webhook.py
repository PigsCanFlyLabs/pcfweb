"""Tests for the Stripe webhook endpoint."""

import contextlib
import json
import threading
import time
import traceback
from datetime import timedelta
from unittest import mock

import stripe
from django.conf import settings
from django.core import mail
from django.db import connection as django_connection
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from main import models as main_models
from main.models import Order, OrderItem
from main.payments import Payments
from main.tests.base import (
    customer_mail,
    WEBHOOK_URL,
    OWNER_EMAIL,
    stripe_signature,
    ORDER_TEST_SETTINGS,
    OrderTestBase,
    OrderTestMixin,
)
from main.views import StripeWebhookView


class WebhookSignatureTest(OrderTestBase):
    """Requirement 3, and the security boundary of the whole feature: an
    unverified payload must never be processed."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order()
        self.body = self.event_body(self.order)

    def assertNothingHappened(self, response):
        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertIsNone(self.order.paid_at)
        self.assertIsNone(self.order.notified_at)
        self.assertEqual(self.order.customer_email, "")
        self.assertEqual(len(mail.outbox), 0)

    def test_a_missing_signature_header_is_rejected(self):
        self.assertNothingHappened(self.deliver(self.body, signature=False))

    def test_an_empty_signature_header_is_rejected(self):
        self.assertNothingHappened(self.deliver(self.body, signature=""))

    def test_a_garbage_signature_header_is_rejected(self):
        self.assertNothingHappened(
            self.deliver(self.body, signature="t=1,v1=deadbeef"))

    def test_a_signature_made_with_the_wrong_secret_is_rejected(self):
        self.assertNothingHappened(
            self.deliver(self.body, secret="whsec_not_the_real_secret"))

    def test_a_body_swapped_after_signing_is_rejected(self):
        # The forgery that matters: a real signature for a payload the
        # attacker then replaces with their own.
        signature = stripe_signature(self.body)
        forged = self.event_body(self.order, payment_status="paid",
                                 amount_total=1)
        self.assertNotEqual(forged, self.body)

        self.assertNothingHappened(self.deliver(forged, signature=signature))

    def test_a_stale_signature_outside_the_tolerance_is_rejected(self):
        old = stripe_signature(self.body, timestamp=int(time.time()) - 3600)
        self.assertNothingHappened(self.deliver(self.body, signature=old))

    def test_a_malformed_body_with_a_valid_signature_is_rejected(self):
        self.assertNothingHappened(self.deliver("this is not json{{"))

    @override_settings(STRIPE_WEBHOOK_SECRET="")
    def test_an_unconfigured_secret_rejects_everything(self):
        # Failing closed: with no secret nothing can be verified, so a
        # correctly signed-looking delivery must still be refused.
        self.assertNothingHappened(self.deliver(self.body))

    def test_the_webhook_refuses_get(self):
        response = self.client.get(WEBHOOK_URL)
        self.assertEqual(response.status_code, 405)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_the_webhook_does_not_require_a_csrf_token(self):
        # Stripe has no token to send; the signature is the authentication.
        strict = Client(enforce_csrf_checks=True)
        body = self.event_body(self.order)
        response = strict.post(
            WEBHOOK_URL, data=body, content_type="application/json",
            HTTP_STRIPE_SIGNATURE=stripe_signature(body))
        self.assertEqual(response.status_code, 200)


class WebhookRejectionDiagnosticsTest(OrderTestBase):
    """A rejected delivery has to say which of its three causes it was.

    The tests above prove a bad delivery is refused. These prove the refusal
    is legible afterwards, which is a different property and the one that
    matters at 2am: all three causes mean the same thing operationally --
    no order will ever be marked paid -- and they have completely different
    fixes. A single "bad signature" line at WARNING was the one log entry
    that could not answer the question being asked of it.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order()
        self.body = self.event_body(self.order)

    def test_a_wrong_secret_names_the_signature_mismatch(self):
        # STRIPE_WEBHOOK_SECRET does not belong to the endpoint that is
        # sending: a rotation, or a test/live mix-up.
        with self.assertLogs("main.views", level="ERROR") as log:
            response = self.deliver(self.body, secret="whsec_not_the_one")

        self.assertEqual(response.status_code, 400)
        self.assertTrue(any("No signatures found matching" in m
                            for m in log.output))

    def test_clock_drift_names_the_tolerance_rather_than_the_secret(self):
        # A perfectly good secret, rejected because the pod clock has drifted
        # past Stripe's five-minute tolerance. Indistinguishable from the
        # case above until the reason is logged.
        stale = stripe_signature(
            self.body, timestamp=int(time.time()) - 3600)

        with self.assertLogs("main.views", level="ERROR") as log:
            response = self.deliver(self.body, signature=stale)

        self.assertEqual(response.status_code, 400)
        self.assertTrue(any("Timestamp outside the tolerance zone" in m
                            for m in log.output))

    def test_a_mangled_header_names_the_header(self):
        # Something between Stripe and here is rewriting Stripe-Signature.
        with self.assertLogs("main.views", level="ERROR") as log:
            response = self.deliver(self.body, signature="not-a-signature")

        self.assertEqual(response.status_code, 400)
        self.assertTrue(any("Unable to extract timestamp" in m
                            for m in log.output))

    def test_every_rejection_says_orders_will_not_be_paid(self):
        # The consequence, spelled out, because the reason on its own does
        # not say that the store has quietly stopped recording sales.
        with self.assertLogs("main.views", level="ERROR") as log:
            self.deliver(self.body, secret="whsec_not_the_one")

        self.assertTrue(any("No order will be marked paid" in m
                            for m in log.output))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)


class WebhookPaymentTest(OrderTestBase):
    """Requirement 2: a validly-signed completed session pays the order and
    tells the owner, exactly once."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=100, quantity=2)

    def test_a_valid_completed_event_marks_the_order_paid(self):
        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNotNone(self.order.paid_at)
        self.assertEqual(self.order.customer_email, "buyer@example.com")
        self.assertEqual(self.order.customer_name, "Buyer Person")
        self.assertEqual(self.order.amount_total,
                         self.order.snapshot_subtotal() + 700)
        self.assertEqual(self.order.amount_tax, 200)
        self.assertEqual(self.order.currency, "usd")

    def test_the_reported_addresses_are_recorded(self):
        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.assertEqual(self.order.shipping_line1, "2 Ship Lane")
        self.assertEqual(self.order.shipping_line2, "Apt 3")
        self.assertEqual(self.order.shipping_city, "Oakland")
        self.assertEqual(self.order.shipping_state, "CA")
        self.assertEqual(self.order.shipping_postal_code, "94607")
        self.assertEqual(self.order.shipping_country, "US")
        self.assertEqual(self.order.billing_line1, "1 Billing Way")
        self.assertEqual(self.order.billing_city, "San Francisco")

    def test_shipping_reported_under_collected_information_is_recorded(self):
        # Newer Stripe API versions moved it; both shapes must work.
        body = self.event_body(
            self.order,
            shipping_details=None,
            collected_information={"shipping_details": {
                "name": "Newer Shape",
                "address": {"line1": "9 New Way", "city": "Berkeley",
                            "state": "CA", "postal_code": "94704",
                            "country": "US"},
            }})
        self.deliver(body)

        self.order.refresh_from_db()
        self.assertEqual(self.order.shipping_name, "Newer Shape")
        self.assertEqual(self.order.shipping_line1, "9 New Way")

    def test_exactly_one_email_goes_to_the_owner(self):
        self.deliver(self.event_body(self.order))

        owner_emails = self.order_emails()
        self.assertEqual(len(owner_emails), 1,
                         "the owner must receive exactly one notification")
        message = owner_emails[0]
        self.assertEqual(message.to, [OWNER_EMAIL])
        self.assertEqual(message.from_email, "support@pigscanfly.ca")
        self.assertIn(str(self.order.pk), message.subject)

    def test_the_email_carries_everything_needed_to_fulfil(self):
        self.deliver(self.event_body(self.order))

        body = self.order_emails()[0].body
        self.assertIn(f"Order #{self.order.pk}", body)
        self.assertIn("2 x Learning Spark", body)
        self.assertIn("buyer@example.com", body)
        for line in ["2 Ship Lane", "Apt 3", "Oakland, CA", "94607"]:
            self.assertIn(line, body)
        self.order.refresh_from_db()
        self.assertIn(self.order.total_display_price(), body)

    def test_a_successful_notification_is_recorded_on_the_order(self):
        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.notified_at)
        self.assertEqual(self.order.notification_error, "")

    def test_the_billed_quantities_are_read_back_from_stripe(self):
        # Checkout enables adjustable_quantity, so the snapshot alone is not
        # a safe pick list. WebhookLineItemReconciliationTest covers the
        # cases where it actually differs.
        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.assertTrue(self.order.quantities_are_authoritative())
        self.assertNotIn("WARNING", self.order_emails()[0].body)

    def test_a_completed_but_unpaid_session_does_not_pay_the_order(self):
        # e.g. an ACH debit that has not settled yet.
        response = self.deliver(
            self.event_body(self.order, payment_status="unpaid"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_later_async_success_pays_that_same_order(self):
        self.deliver(self.event_body(self.order, payment_status="unpaid"))
        response = self.deliver(self.event_body(
            self.order, event_type="checkout.session.async_payment_succeeded"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertGreaterEqual(len(mail.outbox), 1)
        self.assertEqual(len(self.order_emails()), 1)

    def test_an_async_failure_cancels_the_pending_order(self):
        response = self.deliver(self.event_body(
            self.order, event_type="checkout.session.async_payment_failed"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_expired_session_cancels_the_pending_order(self):
        response = self.deliver(self.event_body(
            self.order, event_type="checkout.session.expired"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)

    def test_a_cancellation_event_cannot_undo_a_payment(self):
        self.deliver(self.event_body(self.order))
        self.deliver(self.event_body(
            self.order, event_type="checkout.session.expired"))

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_an_event_for_an_unknown_order_is_accepted_without_side_effects(self):
        body = self.event_body(
            self.order, id="cs_never_seen", client_reference_id="999999",
            metadata={})

        response = self.deliver(body)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_unbound_order_is_found_by_its_client_reference(self):
        # Belt and braces: if the session id never made it onto the order,
        # the reference Stripe echoes back has to be enough to bind it.
        Order.objects.filter(pk=self.order.pk).update(stripe_session_id=None)

        response = self.deliver(self.event_body(self.order, id="cs_other_id"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(self.order.stripe_session_id, "cs_other_id")


class WebhookSessionBindingTest(OrderTestBase):
    """The stored stripe_session_id *is* the binding between an order and the
    payment for it, so it gets verified rather than overwritten.

    Without this, a signed event naming an order in client_reference_id could
    re-point an order already bound to another session and mark it paid,
    silently rewriting the local binding.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order(session_id="cs_session_a")

    def assertOrderUntouched(self, response):
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(self.order.stripe_session_id, "cs_session_a")
        self.assertIsNone(self.order.paid_at)
        self.assertEqual(self.order.customer_email, "")
        self.assertEqual(len(mail.outbox), 0)

    def test_a_different_session_cannot_re_bind_and_pay_an_order(self):
        body = self.event_body(self.order, id="cs_session_b")

        with self.assertLogs("main.views", level="ERROR") as logs:
            response = self.deliver(body)

        self.assertOrderUntouched(response)
        self.assertIn("already bound", "\n".join(logs.output))

    def test_a_different_session_reaching_it_via_metadata_is_also_refused(self):
        body = self.event_body(
            self.order, id="cs_session_b", client_reference_id=None,
            metadata={"order_id": str(self.order.pk)})

        with self.assertLogs("main.views", level="ERROR"):
            response = self.deliver(body)

        self.assertOrderUntouched(response)

    def test_a_cancellation_from_a_different_session_is_also_refused(self):
        body = self.event_body(
            self.order, id="cs_session_b",
            event_type="checkout.session.expired")

        with self.assertLogs("main.views", level="ERROR"):
            response = self.deliver(body)

        self.assertOrderUntouched(response)

    def test_the_orders_own_session_still_works(self):
        # The guard must not break the ordinary path it is protecting.
        response = self.deliver(self.event_body(self.order, id="cs_session_a"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertGreaterEqual(len(mail.outbox), 1)
        self.assertEqual(len(self.order_emails()), 1)


class WebhookLineItemReconciliationTest(OrderTestBase):
    """Checkout enables adjustable_quantity, so the cart snapshot is not
    necessarily what was bought -- and the owner's email *is* the pick list.

    The webhook therefore re-reads the billed quantities from Stripe. That
    extra call is strictly best-effort: a paid order must never be lost
    because a secondary lookup failed.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=100, quantity=2)
        self.item = self.order.items.get()

    def test_the_default_case_records_that_nothing_changed(self):
        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertIsNotNone(self.order.reconciled_at)
        self.assertEqual(self.order.reconciliation_error, "")
        self.assertEqual(self.item.quantity, 2)
        self.assertFalse(self.item.quantity_adjusted())

    def test_a_quantity_increased_at_checkout_is_persisted(self):
        self.billed_quantities = {100: 5}

        self.deliver(self.event_body(self.order))

        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 5)
        self.assertEqual(self.item.snapshot_quantity, 2)
        self.assertTrue(self.item.quantity_adjusted())

    def test_a_quantity_decreased_at_checkout_is_persisted(self):
        self.billed_quantities = {100: 1}

        self.deliver(self.event_body(self.order))

        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)
        self.assertEqual(self.item.snapshot_quantity, 2)

    def test_the_original_quantity_stays_recoverable(self):
        self.billed_quantities = {100: 7}

        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        unit = self.item.unit_amount
        self.assertEqual(self.item.snapshot_quantity, 2)
        self.assertEqual(self.order.original_subtotal(), unit * 2)
        self.assertEqual(self.order.snapshot_subtotal(), unit * 7)
        self.assertEqual(
            [i.pk for i in self.order.adjusted_items()], [self.item.pk])

    def test_a_line_removed_at_checkout_becomes_zero_and_is_recorded(self):
        # Stripe simply omits a line the customer zeroed out; shipping it
        # anyway is exactly the failure this reconciliation exists to stop.
        self.billed_quantities = {100: None}

        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 0)
        self.assertEqual(self.item.snapshot_quantity, 2)
        self.assertIn("did not bill", self.order.reconciliation_error)

    def test_the_email_shows_the_billed_quantity_and_the_adjustment(self):
        self.billed_quantities = {100: 5}

        self.deliver(self.event_body(self.order))

        body = self.order_emails()[0].body
        self.assertIn("5 x Learning Spark", body)
        self.assertNotIn("2 x Learning Spark", body)
        self.assertIn("adjusted at checkout, was 2", body)
        # Reconciled, so no "do not trust this list" warning.
        self.assertNotIn("DO NOT SHIP", body)

    def test_multiple_lines_are_each_matched_on_their_own_price(self):
        self.client.post("/add-to-cart/101/3")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_multi")
            self.client.post("/checkout")
        order = Order.objects.get(stripe_session_id="cs_multi")
        self.billed_quantities = {100: 1, 101: 9}

        self.deliver(self.event_body(order))

        order.refresh_from_db()
        self.assertEqual(
            sorted((i.product_id, i.quantity, i.snapshot_quantity)
                   for i in order.items.all()),
            [(100, 1, 2), (101, 9, 3)])
        self.assertEqual(order.reconciliation_error, "")

    def test_a_billed_line_matching_nothing_is_recorded_not_crashed(self):
        self.extra_line_items = [
            {"price": {"id": "price_from_nowhere"}, "quantity": 3}]

        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNotNone(self.order.reconciled_at)
        self.assertIn("price_from_nowhere", self.order.reconciliation_error)
        self.assertIn("matches no line", self.order.reconciliation_error)
        # And the owner is told the match was not clean.
        self.assertIn("not everything matched up cleanly",
                      self.order_emails()[0].body)

    def test_an_order_line_with_no_price_id_is_recorded_not_crashed(self):
        OrderItem.objects.filter(pk=self.item.pk).update(price_id="")

        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertIn("no Stripe price id", self.order.reconciliation_error)

    def test_more_line_items_than_one_page_changes_nothing_at_all(self):
        # This does not page, so page one alone would zero every line that
        # lives on page two -- silently dropping real items from the pick
        # list. Refuse before writing anything rather than commit a partial
        # truth and mention it in a caveat.
        self.client.post("/add-to-cart/101/3")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_big")
            self.client.post("/checkout")
        order = Order.objects.get(stripe_session_id="cs_big")
        self.line_items_has_more = True
        # Stripe's page one omits one of the two lines, as a real second page
        # would; nothing here may act on that.
        self.billed_quantities = {101: None}

        response = self.deliver(self.event_body(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(
            sorted((i.product_id, i.quantity, i.snapshot_quantity)
                   for i in order.items.all()),
            [(100, 2, 2), (101, 3, 3)])
        self.assertIsNone(order.reconciled_at)
        self.assertFalse(order.quantities_are_authoritative())
        self.assertIn("more than 100", order.reconciliation_error)
        # And the owner is told the list is unverified.
        self.assertIn("could not be re-read from Stripe", mail.outbox[-1].body)

    def test_reconciliation_runs_only_for_the_delivery_that_won(self):
        body = self.event_body(self.order)

        self.deliver(body)
        self.deliver(body)
        self.deliver(body)

        self.assertEqual(self.payments.list_line_items.call_count, 1)

    def test_exactly_one_lookup_of_one_page_is_made(self):
        # One extra call, no pagination loop: this is on the webhook's
        # response path.
        self.deliver(self.event_body(self.order))

        self.payments.list_line_items.assert_called_once_with(
            "cs_test_session", limit=100)

    def test_the_stripe_call_is_bounded_by_a_timeout_and_no_retries(self):
        # Checked against the real wrapper, since the class above stubs it.
        # A hung connection on the SDK's ~80s default would pin the worker
        # long after Stripe had given up and queued a re-delivery.
        with mock.patch("main.payments.stripe.StripeClient") as client_class:
            Payments.list_line_items("cs_x", limit=100)

        kwargs = client_class.call_args.kwargs
        self.assertEqual(kwargs["max_network_retries"], 0)
        # The timeout lives on the HTTP client, not on the request, so a
        # per-call argument would have been silently sent to Stripe as a
        # query parameter and bounded nothing.
        self.assertEqual(kwargs["http_client"]._timeout, 5)
        self.assertEqual(Payments.LINE_ITEM_TIMEOUT, 5)
        client_class.return_value.checkout.sessions.line_items.list \
            .assert_called_once_with("cs_x", {"limit": 100})

    def test_the_bounded_client_is_not_the_one_checkout_uses(self):
        # Bounding this call must not drag Session.create down to 5 seconds.
        # The module-level client that checkout uses is bounded too, but on a
        # far more generous budget -- checkout legitimately wants longer.
        with mock.patch("main.payments.stripe.StripeClient"):
            Payments.list_line_items("cs_x")

        self.assertIsNotNone(stripe.default_http_client)
        self.assertNotEqual(
            stripe.default_http_client._timeout, Payments.LINE_ITEM_TIMEOUT)
        self.assertEqual(
            stripe.default_http_client._timeout, settings.STRIPE_TIMEOUT)

    def test_the_default_client_is_bounded_well_under_the_sdk_default(self):
        # The SDK ships an 80s timeout and retries twice, so an unbounded
        # Session.create could hold a request open for minutes -- long past
        # the point gunicorn kills the worker out from under it.
        self.assertLessEqual(settings.STRIPE_TIMEOUT, 30)


class WebhookReconciliationFailureTest(OrderTestBase):
    """A failed line-item lookup degrades to the snapshot; it never costs a
    paid order."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=100, quantity=2)

    def deliver_with_broken_lookup(self, error=None, **overrides):
        self.line_items_error = error or OSError("Stripe is unreachable")
        with self.assertLogs("main.models", level="ERROR"):
            return self.deliver(self.event_body(self.order, **overrides))

    def test_a_lookup_failure_still_returns_200(self):
        self.assertEqual(self.deliver_with_broken_lookup().status_code, 200)

    def test_a_lookup_failure_still_records_the_paid_order(self):
        self.deliver_with_broken_lookup()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNotNone(self.order.paid_at)
        self.assertEqual(self.order.customer_email, "buyer@example.com")
        self.assertEqual(self.order.shipping_line1, "2 Ship Lane")

    def test_a_lookup_failure_keeps_the_snapshot_quantities(self):
        self.deliver_with_broken_lookup()

        item = self.order.items.get()
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.snapshot_quantity, 2)

    def test_a_lookup_failure_is_visible_on_the_order(self):
        self.deliver_with_broken_lookup()

        self.order.refresh_from_db()
        self.assertIsNone(self.order.reconciled_at)
        self.assertFalse(self.order.quantities_are_authoritative())
        self.assertIn("Stripe is unreachable", self.order.reconciliation_error)

    def test_a_lookup_failure_with_a_mismatched_subtotal_shouts(self):
        # This is the dangerous combination: the totals say the customer
        # changed something and we could not find out what.
        self.deliver_with_broken_lookup(
            amount_subtotal=self.order.snapshot_subtotal() + 4321)

        body = self.order_emails()[0].body
        self.assertIn("DO NOT SHIP FROM THE LIST ABOVE", body)
        self.assertIn("Stripe is unreachable", body)

    def test_a_lookup_failure_with_a_matching_subtotal_says_so_quietly(self):
        self.deliver_with_broken_lookup()

        body = self.order_emails()[0].body
        self.assertNotIn("DO NOT SHIP", body)
        self.assertIn("could not be re-read from Stripe", body)

    def test_the_owner_is_still_emailed_once(self):
        self.deliver_with_broken_lookup()
        self.assertEqual(len(self.order_emails()), 1)

    def test_a_crash_before_the_marker_lands_rolls_the_quantities_back(self):
        # The quantities and the "these came from Stripe" marker are one
        # fact. Landing the first without the second would leave the admin
        # showing Stripe's numbers while reconciled_at says they were never
        # checked -- and the email would call them unverified.
        self.billed_quantities = {100: 5}
        # timezone.now() is evaluated for reconciled_at, i.e. after the item
        # quantities have been written and before the marker is.
        with mock.patch("main.models.timezone.now",
                        side_effect=RuntimeError("boom, between the writes")):
            with self.assertLogs("main.models", level="ERROR"):
                reconciled = self.order.reconcile_line_items()

        self.assertFalse(reconciled)
        item = self.order.items.get()
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.snapshot_quantity, 2)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.reconciled_at)
        self.assertIn("boom, between the writes",
                      self.order.reconciliation_error)

    def test_a_late_bound_order_is_still_reconciled(self):
        Order.objects.filter(pk=self.order.pk).update(stripe_session_id=None)

        self.deliver(self.event_body(self.order, id="cs_late_binding"))

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        # It got bound before reconciliation ran, so the lookup did happen.
        self.assertEqual(self.order.stripe_session_id, "cs_late_binding")
        self.assertIsNotNone(self.order.reconciled_at)


class WebhookIdempotencyTest(OrderTestBase):
    """Requirement 4: Stripe retries on any non-2xx and can duplicate
    deliveries outright."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order()

    def test_redelivering_the_same_event_pays_and_mails_once(self):
        body = self.event_body(self.order)

        first = self.deliver(body)
        second = self.deliver(body)
        third = self.deliver(body)

        self.assertEqual(
            [first.status_code, second.status_code, third.status_code],
            [200, 200, 200])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.assertEqual(len(self.order_emails()), 1,
                         "owner must receive exactly one notification")

    def test_the_paid_transition_only_ever_runs_from_pending(self):
        # The guard is a conditional UPDATE, not a read-then-write, so it is
        # the database that refuses the second transition.
        self.deliver(self.event_body(self.order))
        self.order.refresh_from_db()
        first_paid_at = self.order.paid_at

        self.deliver(self.event_body(self.order, amount_total=999999))

        self.order.refresh_from_db()
        self.assertEqual(self.order.paid_at, first_paid_at)
        self.assertNotEqual(self.order.amount_total, 999999)

    def test_a_redelivery_after_fulfilment_does_not_re_notify(self):
        self.deliver(self.event_body(self.order))
        # The owner ships it and marks it done in the admin.
        Order.objects.filter(pk=self.order.pk).update(
            status=Order.Status.FULFILLED)
        mail.outbox.clear()

        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.FULFILLED)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_redelivery_after_a_failed_email_retries_the_email(self):
        with mock.patch("main.models.send_mail",
                        side_effect=OSError("SMTP is down")):
            with self.assertLogs("main.models", level="ERROR"):
                self.deliver(self.event_body(self.order))

        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(mail.outbox), 1)
        self.assertEqual(len(self.order_emails()), 1,
                         "owner was notified exactly once on retry")
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.notified_at)

    def test_redelivery_resumes_after_a_crash_just_after_marking_paid(self):
        body = self.event_body(self.order)

        with mock.patch.object(
                StripeWebhookView, "fulfil_order",
                side_effect=SystemExit("simulated worker crash")):
            with self.assertRaisesRegex(SystemExit, "simulated worker crash"):
                self.deliver(body)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNone(self.order.reconciled_at)
        self.assertIsNone(self.order.notified_at)
        self.assertEqual(len(mail.outbox), 0)

        response = self.deliver(body)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.reconciled_at)
        self.assertIsNotNone(self.order.notified_at)
        self.assertEqual(len(self.order_emails()), 1)


class WebhookEmailFailureTest(OrderTestBase):
    """Requirement 5: a failing send_mail must not fail the webhook.

    A 500 here makes Stripe retry for up to three days, so an SMTP outage
    would turn into an order that looks unrecorded plus a mail flood later.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order()

    def deliver_with_broken_mail(self, exception=None):
        exception = exception or OSError("SMTP server is unreachable")
        # Both senders: notify_owner() is on send_mail, while the receipt and
        # the download email go through send_sales_email. An outage that broke
        # only one of them would not be the outage this class is named for.
        # assertLogs both proves the failure is not swallowed silently and
        # keeps the deliberate traceback out of the test output.
        with mock.patch("main.models.send_mail", side_effect=exception), \
                mock.patch("main.models.send_sales_email",
                           side_effect=exception):
            with self.assertLogs("main.models", level="ERROR"):
                return self.deliver(self.event_body(self.order))

    def test_a_send_failure_still_returns_200(self):
        self.assertEqual(self.deliver_with_broken_mail().status_code, 200)

    def test_a_send_failure_still_records_the_paid_order(self):
        self.deliver_with_broken_mail()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNotNone(self.order.paid_at)
        self.assertEqual(self.order.customer_email, "buyer@example.com")
        self.assertEqual(self.order.shipping_line1, "2 Ship Lane")

    def test_a_send_failure_is_visible_on_the_order(self):
        self.deliver_with_broken_mail()

        self.order.refresh_from_db()
        self.assertIsNone(self.order.notified_at)
        self.assertIn("SMTP server is unreachable", self.order.notification_error)

    @override_settings(ADMINS=[])
    def test_no_configured_admins_is_recorded_rather_than_raised(self):
        with self.assertLogs("main.models", level="ERROR"):
            response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNone(self.order.notified_at)
        self.assertIn("ADMINS", self.order.notification_error)


class WebhookUnhandledEventTest(OrderTestBase):
    """Requirement 6: anything this endpoint has no opinion about gets a 200
    so Stripe stops retrying it."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order()

    def test_an_unrelated_event_type_is_accepted_and_ignored(self):
        for event_type in ["payment_intent.succeeded", "invoice.paid",
                           "customer.created", "charge.refunded"]:
            with self.subTest(event_type=event_type):
                response = self.deliver(
                    self.event_body(self.order, event_type=event_type))

                self.assertEqual(response.status_code, 200)
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, Order.Status.PENDING)
                self.assertIsNone(self.order.paid_at)
                self.assertEqual(len(mail.outbox), 0)

    def test_an_event_with_no_recognisable_object_is_accepted(self):
        body = json.dumps({
            "id": "evt_weird", "object": "event",
            "type": "some.new.event.type", "data": {},
        })
        response = self.deliver(body)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)


class WebhookInterleavedDeliveryTest(OrderTestBase):
    """The race the idempotency guard exists to close.

    NOT a threaded test, and it does not need to be. What is covered here is
    the specific interleaving that matters for the PENDING -> PAID transition:
    two deliveries that both observe the order as PENDING before either
    writes. That is precisely what a read-then-write check gets wrong, and it
    is deterministic.

    An earlier note here recorded that a threaded version had been abandoned
    because both threads died with "database table is locked" on SQLite. That
    applies to overlapping the paid transition itself, which holds a write
    lock for its whole duration. It is not true of the *fulfilment* overlap,
    which happens after that transaction has committed --
    ``WebhookConcurrentWorkerTest`` below does run two real connections, and
    has to, because that race is invisible to a single-process test.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order()

    def test_only_one_of_two_deliveries_that_both_saw_pending_can_pay(self):
        # Both readers hold a PENDING view of the row, as two concurrent
        # webhook workers would.
        first_view = Order.objects.get(pk=self.order.pk)
        second_view = Order.objects.get(pk=self.order.pk)
        self.assertEqual(first_view.status, Order.Status.PENDING)
        self.assertEqual(second_view.status, Order.Status.PENDING)

        # The transition is a conditional UPDATE, so the database decides it,
        # not either reader's already-stale copy.
        first = Order.objects.filter(
            pk=first_view.pk, status=Order.Status.PENDING).update(
                status=Order.Status.PAID)
        second = Order.objects.filter(
            pk=second_view.pk, status=Order.Status.PENDING).update(
                status=Order.Status.PAID)

        self.assertEqual((first, second), (1, 0))

    def test_a_delivery_arriving_mid_flight_does_not_double_notify(self):
        # Re-enter the webhook from inside the first delivery's post-commit
        # work, i.e. while the first one is still running.
        body = self.event_body(self.order)
        real_notify = Order.notify_owner
        reentered = []

        def notify_and_reenter(order):
            if not reentered:
                reentered.append(True)
                self.deliver(body)
            return real_notify(order)

        with mock.patch.object(Order, "notify_owner", notify_and_reenter):
            self.deliver(body)

        self.assertTrue(reentered)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(len(self.order_emails()), 1)



@override_settings(**ORDER_TEST_SETTINGS)
class WebhookConcurrentWorkerTest(OrderTestMixin, TransactionTestCase):
    """Two Gunicorn workers fulfilling the same order at the same time.

    ``scripts/start-server.sh`` runs gunicorn with ``--workers 4``, so the
    duplicate deliveries Stripe makes of one event land in processes that
    share no memory. Nothing in the request path serialises them: the
    ``select_for_update`` in ``handle_paid`` is released with its
    transaction, well before fulfilment starts, and every completion marker
    is written only *after* the side effect it records. So the window between
    "is ``notified_at`` still null?" and "``notified_at`` is now set" spans an
    entire SMTP send, and a second worker looking in that window sees null and
    sends the same email again.

    ``TransactionTestCase`` rather than ``TestCase``: the second worker runs
    on its own database connection, and can only see rows that are actually
    committed. (The abandoned threaded test described on
    ``WebhookInterleavedDeliveryTest`` overlapped the PENDING -> PAID
    transaction itself, which is what deadlocked SQLite. Overlapping the
    e-mail send instead touches no open write transaction, so it neither
    locks nor needs synchronisation beyond the handoff below.)
    """

    @contextlib.contextmanager
    def as_a_separate_worker_process(self):
        """Run the enclosed request as a worker that shares no memory.

        A second gunicorn worker has its own copy of every class attribute,
        so any *process-local* bookkeeping on the view is empty there however
        busy this process is. Every mutable set on the class is therefore
        swapped for a fresh one for the duration; the view's genuinely
        constant sets are frozensets and are deliberately left alone.

        Against a database-backed claim this does nothing whatsoever, which
        is precisely the point. The guarantee under test has to survive being
        run in a process that shares nothing with the one already fulfilling,
        so a guard that lives in memory must not be what makes this pass.
        """
        saved = {name: value for name, value in vars(StripeWebhookView).items()
                 if type(value) is set}
        for name in saved:
            setattr(StripeWebhookView, name, set())
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(StripeWebhookView, name, value)

    def test_a_second_worker_mid_send_does_not_repeat_the_owner_email(self):
        order = self.place_order()
        body = self.event_body(order)
        overlapped = []
        failures = []
        real_send = main_models.send_mail

        def second_worker():
            """The losing delivery, on its own connection and its own memory."""
            try:
                with self.as_a_separate_worker_process():
                    Client().post(
                        WEBHOOK_URL, data=body,
                        content_type="application/json",
                        HTTP_STRIPE_SIGNATURE=stripe_signature(body))
            except Exception:                      # pragma: no cover - reported
                failures.append(traceback.format_exc())
            finally:
                # A thread that opened a connection has to close it, or the
                # test database cannot be torn down.
                django_connection.close()

        def send_and_overlap(*args, **kwargs):
            # Hold the first worker *inside* its send -- the exact window
            # where its notified_at is still null -- and run the whole of the
            # second worker's delivery there. Deterministic: no sleeps, and
            # the second worker is complete before the first one's mail goes.
            if not overlapped:
                overlapped.append(True)
                thread = threading.Thread(target=second_worker)
                thread.start()
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive(),
                                 "the second worker never finished")
            return real_send(*args, **kwargs)

        with mock.patch("main.models.send_mail", send_and_overlap):
            self.deliver(body)

        self.assertEqual(failures, [], "the second worker raised")
        self.assertTrue(overlapped, "the deliveries never actually overlapped")
        # The whole point: the owner is told once, not once per worker.
        self.assertEqual(len(self.order_emails()), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIsNotNone(order.notified_at)
        # And the claim is handed back, so a later delivery can still repair
        # anything this one left undone.
        self.assertIsNone(order.fulfilment_claimed_at)


class WebhookFulfilmentClaimTest(OrderTestBase):
    """The claim is a lease, so a worker that dies cannot wedge an order."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order()
        # A paid order with everything still to do, i.e. what a worker that
        # died just after the PAID commit leaves behind.
        Order.objects.filter(pk=self.order.pk).update(
            status=Order.Status.PAID, reconciled_at=None, notified_at=None)

    def test_a_live_claim_holds_fulfilment_off(self):
        Order.objects.filter(pk=self.order.pk).update(
            fulfilment_claimed_at=timezone.now())

        with self.assertLogs("main.views", level="INFO"):
            response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.notified_at)
        self.assertEqual(len(self.order_emails()), 0)

    def test_a_claim_left_behind_by_a_dead_worker_is_reclaimed(self):
        # No process holds this any more; the worker that took it is gone.
        stale = timezone.now() - StripeWebhookView.FULFILMENT_LEASE
        Order.objects.filter(pk=self.order.pk).update(
            fulfilment_claimed_at=stale - timedelta(seconds=1))

        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.notified_at)
        self.assertIsNone(self.order.fulfilment_claimed_at)
        self.assertEqual(len(self.order_emails()), 1)


class WebhookReconciliationRetryNotificationTest(OrderTestBase):
    """The owner's e-mail *is* the pick list, so it cannot outlive its facts.

    When the first delivery cannot reach Stripe, the owner is told the
    quantities are the cart's and unverified. The retry path added for crash
    recovery then re-runs reconciliation on the next delivery -- and if that
    one succeeds it can replace those quantities, because a customer can
    change them on Stripe's hosted page. Without this, the notification marker
    is already set, no new e-mail goes out, and the only instruction the owner
    ever received tells them to ship numbers the database now knows are wrong.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=100, quantity=2)

    def test_a_retry_that_finally_reconciles_reissues_the_notification(self):
        body = self.event_body(self.order)

        # First delivery: Stripe's line-item lookup is unreachable.
        self.line_items_error = RuntimeError("Stripe is unreachable")
        with self.assertLogs("main.models", level="ERROR"):
            self.deliver(body)

        self.order.refresh_from_db()
        self.assertIsNone(self.order.reconciled_at)
        self.assertIsNotNone(self.order.notified_at)
        first = self.order_emails()
        self.assertEqual(len(first), 1)
        self.assertIn("2 x", first[0].body)
        self.assertIn("could not be re-read from Stripe", first[0].body)

        # Second delivery: Stripe answers this time, and reports that the
        # customer actually bought five, not the two in the cart snapshot.
        self.line_items_error = None
        self.billed_quantities = {100: 5}

        response = self.deliver(body)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.reconciled_at)
        self.assertEqual(self.order.items.first().quantity, 5)

        emails = self.order_emails()
        self.assertEqual(len(emails), 2)
        corrected = emails[1]
        self.assertIn("5 x", corrected.body)
        self.assertIn("[adjusted at checkout, was 2]", corrected.body)
        # The caveat is gone, because these numbers really did come from
        # Stripe this time.
        self.assertNotIn("could not be re-read from Stripe", corrected.body)

    def test_a_retry_that_still_cannot_reconcile_does_not_re_notify(self):
        # The owner has already been told, and nothing new has been learned,
        # so a redelivery must not turn Stripe's retries into an e-mail flood.
        body = self.event_body(self.order)
        self.line_items_error = RuntimeError("Stripe is unreachable")

        with self.assertLogs("main.models", level="ERROR"):
            self.deliver(body)
        with self.assertLogs("main.models", level="ERROR"):
            self.deliver(body)

        self.assertEqual(len(self.order_emails()), 1)


@override_settings(**ORDER_TEST_SETTINGS)
class WebhookBuyerReceiptTest(OrderTestBase):
    """The buyer receives a receipt email after Stripe reports the order PAID.

    The receipt is best-effort: a failure is recorded on the row but must
    never block digital delivery or the owner notification."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order()

    def _receipts(self):
        """Every receipt a customer got, not counting the owner's copies."""
        return customer_mail("Your receipt")

    def _receipt(self):
        receipts = self._receipts()
        self.assertTrue(receipts, "no receipt email found in the outbox")
        return receipts[0]

    # ---- requirement 1: the receipt is sent at all ----

    def test_a_paid_order_sends_the_buyer_a_receipt(self):
        self.deliver(self.event_body(self.order))

        receipt = self._receipt()
        self.assertEqual(receipt.to, ["buyer@example.com"])
        self.assertIn("Order #", receipt.body)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.receipt_sent_at)
        self.assertEqual(self.order.receipt_error, "")

    # ---- requirement 2: idempotency ----

    def test_redelivering_the_same_event_sends_the_receipt_once(self):
        self.deliver(self.event_body(self.order))
        mail.outbox.clear()

        self.deliver(self.event_body(self.order))

        self.assertEqual(len(self._receipts()), 0)

    def test_redelivery_after_failed_receipt_retries_it(self):
        # send_sales_email, not send_mail: the buyer's copies go out through
        # the Bcc-carrying helper, and the owner notification is the only
        # thing still on send_mail.
        with mock.patch("main.models.send_sales_email",
                        side_effect=OSError("SMTP is down")):
            with self.assertLogs("main.models", level="ERROR"):
                self.deliver(self.event_body(self.order))

        self.assertEqual(len(self._receipts()), 0)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.receipt_sent_at)
        self.assertNotEqual(self.order.receipt_error, "")

        mail.outbox.clear()

        self.deliver(self.event_body(self.order))

        self.assertEqual(len(self._receipts()), 1)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.receipt_sent_at)
        self.assertEqual(self.order.receipt_error, "")

    # ---- requirement 3: failure isolation ----

    def test_receipt_failure_never_blocks_the_owner_notification(self):
        # Only the buyer-facing send is broken here, which is the point: the
        # owner's notification goes out over send_mail and must still arrive.
        with mock.patch("main.models.send_sales_email",
                        side_effect=OSError("SMTP is down")):
            with self.assertLogs("main.models", level="ERROR"):
                self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNone(self.order.receipt_sent_at)
        self.assertNotEqual(self.order.receipt_error, "")
        self.assertIsNotNone(self.order.notified_at,
                             "owner was not notified")

    def test_receipt_failure_never_blocks_the_order_from_completing(self):
        # The worst case: every send raises -- the owner notification on
        # send_mail and the buyer's copies on send_sales_email alike -- yet
        # the order still transitions to PAID and the webhook returns 200.
        with mock.patch("main.models.send_mail",
                        side_effect=OSError("SMTP is down")), \
                mock.patch("main.models.send_sales_email",
                           side_effect=OSError("SMTP is down")):
            with self.assertLogs("main.models", level="ERROR"):
                response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    # ---- requirement 4: PWYW amounts ----

    def test_pwyw_receipt_shows_the_chosen_amount_not_the_suggestion(self):
        product = main_models.Product.objects.get(pk=106)
        order = self.manual_order((product, 1))
        # $5.00 paid against a $12.99 suggestion
        item = order.items.get()
        item.unit_amount = 500
        item.save()
        order.amount_total = 700   # $5 + $2 tax
        order.amount_subtotal = 500
        order.amount_tax = 200
        order.customer_email = "buyer@example.com"
        order.save()

        order.send_receipt()

        body = self._receipt().body
        self.assertIn("5.00", body)
        self.assertNotIn("12.99", body)
        self.assertIn("(pay-what-you-want", body)

    def test_zero_dollar_receipt_is_still_sent_and_reads_sensibly(self):
        product = main_models.Product.objects.get(pk=104)
        order = self.manual_order((product, 1))
        item = order.items.get()
        item.unit_amount = 0
        item.save()
        order.amount_total = 0
        order.amount_subtotal = 0
        order.amount_tax = 0
        order.customer_email = "buyer@example.com"
        order.save()

        order.send_receipt()

        body = self._receipt().body
        self.assertIn("0.00", body)
        self.assertIn("Total:", body)
        self.assertNotIn("unpaid", body.lower())
        self.assertNotIn("invoice", body.lower())

    # ---- requirement 5: no internal leaks ----

    def test_receipt_body_leaks_no_internal_keys(self):
        self.deliver(self.event_body(self.order))

        body = self._receipt().body
        self.assertNotIn("cs_test", body)
        self.assertNotIn("download_token", body)
        self.assertNotIn("?token=", body)

    # ---- requirement 6: mutation ----

    def test_reverting_the_send_makes_the_test_fail(self):
        # Guard: if send_receipt is removed, this test must fail.
        self.deliver(self.event_body(self.order))
        self.assertTrue(self._receipts(),
                        "no receipt was sent — send_receipt is not wired")

    # ---- edge cases ----

    def test_no_receipt_without_customer_email(self):
        # send_receipt itself refuses, recording the failure on the row.
        self.order.customer_email = ""
        self.order.save()
        self.order.send_receipt()

        self.assertEqual(len(self._receipts()), 0)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.receipt_sent_at)
        self.assertNotEqual(self.order.receipt_error, "")


@override_settings(**ORDER_TEST_SETTINGS)
class WebhookBuyerReceiptConcurrentTest(OrderTestMixin, TransactionTestCase):
    """Two workers fulfilling the same order send exactly one receipt.

    TransactionTestCase rather than TestCase so the second worker sees
    committed data on its own connection."""

    @contextlib.contextmanager
    def as_a_separate_worker_process(self):
        saved = {name: value
                 for name, value in vars(StripeWebhookView).items()
                 if type(value) is set}
        for name in saved:
            setattr(StripeWebhookView, name, set())
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(StripeWebhookView, name, value)

    def test_concurrent_workers_send_the_receipt_exactly_once(self):
        order = self.place_order()
        body = self.event_body(order)
        overlapped = []
        failures = []
        real_send = main_models.send_sales_email

        def second_worker():
            try:
                with self.as_a_separate_worker_process():
                    Client().post(
                        WEBHOOK_URL, data=body,
                        content_type="application/json",
                        HTTP_STRIPE_SIGNATURE=stripe_signature(body))
            except Exception:
                failures.append(traceback.format_exc())
            finally:
                django_connection.close()

        def send_and_overlap(*args, **kwargs):
            if not overlapped:
                overlapped.append(True)
                thread = threading.Thread(target=second_worker)
                thread.start()
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive(),
                                 "the second worker never finished")
            return real_send(*args, **kwargs)

        # Patched on send_sales_email so the overlap lands in the window the
        # receipt is actually sent in, which is the send this test is about.
        with mock.patch("main.models.send_sales_email", send_and_overlap):
            self.deliver(body)

        self.assertEqual(failures, [], "the second worker raised")
        # Its siblings assert this and this one did not: without it, a send
        # that moves off the patched name (which is exactly what happened
        # when the receipt left send_mail) leaves the second worker never
        # started and the race silently uncovered.
        self.assertTrue(overlapped, "the deliveries never actually overlapped")
        receipts = customer_mail("Your receipt")
        self.assertEqual(len(receipts), 1,
                         f"expected 1 receipt; got {len(receipts)}")
        order.refresh_from_db()
        self.assertIsNotNone(order.receipt_sent_at)
