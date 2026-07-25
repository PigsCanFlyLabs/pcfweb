"""Shared bases, constants and helpers for the ``main`` test package.

``OrderTestBase`` and the webhook constants are used from both
test_orders and test_webhook, so they live here rather than in
either one. This module is deliberately not named ``test_*`` so the
test runner does not try to collect it."""

import hashlib
import hmac
import itertools
import json
import time
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings

from main.models import Order


SHIPPING_NOTICE_TEXT = "shipping times for physical goods are currently long"
WEBHOOK_SECRET = "whsec_test_secret_value"
WEBHOOK_URL = "/stripe/webhook"
OWNER_EMAIL = "owner@example.com"


def stripe_signature(payload: str, secret: str = WEBHOOK_SECRET,
                     timestamp=None) -> str:
    """Build a real Stripe-Signature header.

    Deliberately the genuine HMAC construction rather than a mocked-out
    construct_event: a suite that never runs the signature check cannot tell
    a working verification from a missing one, which is the one bug in this
    feature that actually matters.
    """
    timestamp = int(time.time()) if timestamp is None else timestamp
    signature = hmac.new(
        secret.encode(), f"{timestamp}.{payload}".encode(),
        hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


@override_settings(
    STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET,
    ADMINS=[("Owner", OWNER_EMAIL)],
    DEFAULT_FROM_EMAIL="support@pigscanfly.ca")
class OrderTestBase(TestCase):
    """Orders and the Stripe webhook. Stripe itself is stubbed; the signature
    verification is not."""

    fixtures = ["initial_products"]

    def setUp(self):
        patcher = mock.patch("main.models.Payments")
        payments = patcher.start()
        self.addCleanup(patcher.stop)
        self.payments = payments
        payments.create_product.return_value = "prod_test"
        # A distinct Stripe Price per cart row, as the real code does -- the
        # reconciliation join is on price id, so a shared one would be
        # unrepresentative.
        prices = itertools.count(1)
        payments.create_price.side_effect = (
            lambda *a, **kw: f"price_test_{next(prices)}")

        # By default Stripe reports exactly what was snapshotted, i.e. the
        # customer changed nothing. Tests override these to model an
        # adjustment or a failed lookup.
        self.billed_quantities: dict = {}
        self.extra_line_items: list = []
        self.line_items_error = None
        self.line_items_has_more = False
        payments.list_line_items.side_effect = self.fake_line_items

    def fake_line_items(self, session_id, limit=100):
        """Stand in for Stripe's line-item listing for a session.

        billed_quantities is keyed by product pk; None means the customer
        removed that line at checkout, so Stripe does not report it at all.
        """
        if self.line_items_error is not None:
            raise self.line_items_error
        order = Order.objects.filter(stripe_session_id=session_id).first()
        data = []
        if order is not None:
            for item in order.items.all():
                quantity = self.billed_quantities.get(
                    item.product_id, item.snapshot_quantity)
                if quantity is None:
                    continue
                data.append(
                    {"price": {"id": item.price_id}, "quantity": quantity})
        return {"data": data + self.extra_line_items,
                "has_more": self.line_items_has_more}

    def place_order(self, product_pk=100, quantity=2,
                    session_id="cs_test_session", client=None):
        """Run the real checkout path, with only Stripe's HTTP call stubbed."""
        client = client or self.client
        client.post(f"/add-to-cart/{product_pk}/{quantity}")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/session", id=session_id)
            response = client.post("/checkout")
        self.create_call = create
        self.checkout_response = response
        return Order.objects.get(stripe_session_id=session_id)

    def session_payload(self, order, **overrides):
        """A checkout.session object shaped like Stripe's."""
        session = {
            "id": order.stripe_session_id or "cs_test_session",
            "object": "checkout.session",
            "client_reference_id": str(order.pk),
            "metadata": {"order_id": str(order.pk)},
            "payment_status": "paid",
            "currency": "usd",
            # Subtotal agrees with the snapshot; the mismatch case has its
            # own test.
            "amount_subtotal": order.snapshot_subtotal(),
            "amount_total": order.snapshot_subtotal() + 700,
            "total_details": {"amount_tax": 200, "amount_shipping": 500},
            "customer_details": {
                "email": "buyer@example.com",
                "name": "Buyer Person",
                "address": {
                    "line1": "1 Billing Way", "line2": "",
                    "city": "San Francisco", "state": "CA",
                    "postal_code": "94110", "country": "US",
                },
            },
            "shipping_details": {
                "name": "Buyer Person",
                "address": {
                    "line1": "2 Ship Lane", "line2": "Apt 3",
                    "city": "Oakland", "state": "CA",
                    "postal_code": "94607", "country": "US",
                },
            },
        }
        session.update(overrides)
        return session

    def event_body(self, order, event_type="checkout.session.completed",
                   **overrides) -> str:
        return json.dumps({
            "id": "evt_test_1",
            "object": "event",
            "type": event_type,
            "data": {"object": self.session_payload(order, **overrides)},
        })

    @staticmethod
    def order_emails():
        """Just the fulfilment notifications.

        Configuring ADMINS also enables Django's own 500 mail, so a test that
        provokes an error would otherwise see it in the outbox too.
        """
        return [m for m in mail.outbox if "New paid order" in m.subject]

    def deliver(self, body: str, signature=None, secret=WEBHOOK_SECRET):
        """POST a webhook body, signed for real unless told otherwise."""
        if signature is None:
            signature = stripe_signature(body, secret=secret)
        extra = {} if signature is False else {
            "HTTP_STRIPE_SIGNATURE": signature}
        return self.client.post(
            WEBHOOK_URL, data=body, content_type="application/json", **extra)
