"""Tests for the Stripe webhook endpoint."""

import json
import time
from unittest import mock

import stripe
from django.core import mail
from django.test import Client, override_settings

from main.models import Order, OrderItem
from main.payments import Payments
from main.tests.base import (
    WEBHOOK_URL,
    OWNER_EMAIL,
    stripe_signature,
    OrderTestBase,
)


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

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [OWNER_EMAIL])
        self.assertEqual(message.from_email, "support@pigscanfly.ca")
        self.assertIn(str(self.order.pk), message.subject)

    def test_the_email_carries_everything_needed_to_fulfil(self):
        self.deliver(self.event_body(self.order))

        body = mail.outbox[0].body
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
        self.assertNotIn("WARNING", mail.outbox[0].body)

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
        self.assertEqual(len(mail.outbox), 1)

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
        self.assertEqual(len(mail.outbox), 1)


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

        body = mail.outbox[0].body
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
        self.assertIn("not everything matched up cleanly", mail.outbox[0].body)

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
        with mock.patch("main.payments.stripe.StripeClient"):
            Payments.list_line_items("cs_x")

        self.assertIsNone(stripe.default_http_client)


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

        body = mail.outbox[0].body
        self.assertIn("DO NOT SHIP FROM THE LIST ABOVE", body)
        self.assertIn("Stripe is unreachable", body)

    def test_a_lookup_failure_with_a_matching_subtotal_says_so_quietly(self):
        self.deliver_with_broken_lookup()

        body = mail.outbox[0].body
        self.assertNotIn("DO NOT SHIP", body)
        self.assertIn("could not be re-read from Stripe", body)

    def test_the_owner_is_still_emailed_once(self):
        self.deliver_with_broken_lookup()
        self.assertEqual(len(mail.outbox), 1)

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
        self.assertEqual(len(mail.outbox), 1)

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

    def test_a_redelivery_after_a_failed_email_does_not_retry_the_email(self):
        # Deliberate: the order is recorded either way, and re-mailing on
        # every one of Stripe's retries is exactly the flood being avoided.
        with mock.patch("main.models.send_mail",
                        side_effect=OSError("SMTP is down")):
            with self.assertLogs("main.models", level="ERROR"):
                self.deliver(self.event_body(self.order))

        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)


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
        # assertLogs both proves the failure is not swallowed silently and
        # keeps the deliberate traceback out of the test output.
        with mock.patch("main.models.send_mail", side_effect=exception):
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

    NOT a threaded test. A genuine two-thread version was written and then
    abandoned: against the SQLite test database both threads die with
    "database table is locked" before either reaches the guard, so it measures
    SQLite's shared-cache locking rather than this code, and making it pass
    would need exactly the elaborate synchronisation that produces flaky
    tests. Postgres -- what prod runs, and where select_for_update is a real
    row lock -- is not available in CI.

    What is covered instead is the specific interleaving that matters: two
    deliveries that both observe the order as PENDING before either writes.
    That is precisely what a read-then-write check gets wrong, and it is
    deterministic.
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
