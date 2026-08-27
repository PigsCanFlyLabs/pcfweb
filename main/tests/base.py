"""Shared bases, constants and helpers for the ``main`` test package.

Every base class used from more than one test module lives here:
``OrderTestBase`` (test_orders, test_webhook, test_pwyw, test_digital),
``CartTestBase`` (test_cart, test_digital) and ``BookAssetRootMixin``
(test_digital, test_pwyw). This module is deliberately not named
``test_*`` so the test runner does not try to collect it -- which is the
whole point for the TestCase subclasses above, since importing one into
a second test module would otherwise collect its tests a second time."""

import hashlib
import hmac
import itertools
import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings

from main.models import Order, OrderItem, Product
from main.utils import SALE_COPY_HEADER


SHIPPING_NOTICE_TEXT = "shipping times for physical goods are currently long"
WEBHOOK_SECRET = "whsec_test_secret_value"
WEBHOOK_URL = "/stripe/webhook"
OWNER_EMAIL = "owner@example.com"
# The address SALES_COPY_EMAILS is pinned to for order tests, so they assert
# against a known copy recipient rather than whatever the shipped default is.
SALES_COPY_EMAIL = "copies@example.com"
NEVER_CACHE_DIRECTIVES = {
    "max-age=0",
    "no-cache",
    "no-store",
    "must-revalidate",
    "private",
}

# The repository root. Resolved from this file rather than the working
# directory because the packaging tests read Dockerfile/.gitignore/build.sh
# off disk, and the suite is not always run from the checkout root.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

EBOOK_PK = 106
EBOOK_STEM = "distributed_computing_4_kids"


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


def cache_control_directives(response) -> set[str]:
    return {
        directive.strip().lower()
        for directive in response["Cache-Control"].split(",")
        if directive.strip()
    }


def assert_never_cache_response(testcase, response) -> None:
    directives = cache_control_directives(response)
    testcase.assertEqual(directives, NEVER_CACHE_DIRECTIVES)
    testcase.assertNotIn("public", directives)
    testcase.assertFalse(
        any(directive == "s-maxage" or directive.startswith("s-maxage=")
            for directive in directives))
    testcase.assertIn("Expires", response)


def customer_mail(needle: str):
    """Messages a customer received, excluding the owner's copies of them.

    The copy of a sale email carries the same subject as the original --
    being the same message is the point of it -- so a subject filter on its
    own counts two, and every "exactly one receipt went out" assertion in
    this suite would quietly start passing on a duplicate. The copy is told
    apart by the header main.utils puts on it, which is also what an inbox
    rule would filter on.
    """
    return [m for m in mail.outbox
            if needle in m.subject
            and SALE_COPY_HEADER not in (m.extra_headers or {})]


"""The settings every order test needs. Kept as a dict rather than applied
once, because the concurrency tests need the same settings on a
``TransactionTestCase`` -- real threads on real connections cannot see the
uncommitted data a ``TestCase`` leaves in its wrapping transaction."""
ORDER_TEST_SETTINGS = dict(
    STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET,
    ADMINS=[("Owner", OWNER_EMAIL)],
    SALES_COPY_EMAILS=[SALES_COPY_EMAIL],
    DEFAULT_FROM_EMAIL="support@pigscanfly.ca")


class OrderTestMixin:
    """Order and Stripe-webhook helpers, independent of which TestCase base.

    Everything lives here rather than on ``OrderTestBase`` so it can be mixed
    into a ``TransactionTestCase`` too; ``override_settings`` refuses to
    decorate a plain mixin, so concrete classes apply
    ``ORDER_TEST_SETTINGS`` themselves.
    """

    fixtures = ["initial_products"]
    # Supplied by the concrete TestCase this gets mixed into; named here so
    # the helpers below type-check on the mixin on its own.
    client: Client

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
        Product.objects.filter(cat=Product.Categories.BOOKS).update(stock=99)

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

    def manual_order(self, *items, session_id="cs_manual"):
        """Build a pending order directly, with the checkout snapshot shape.

        This keeps fulfilment tests honest when the thing under test is what
        happens *after* an order exists, not whether today's checkout flow can
        create one. That matters here because Product.is_pwyw is a live admin
        toggle and fulfilment reads Product flags live too: an order that was
        ordinary when it was placed can become a mixed PWYW/fixed order later.
        """
        order = Order.objects.create(
            status=Order.Status.PENDING,
            currency="usd",
            amount_total=sum(product.price * quantity
                             for product, quantity in items),
            stripe_session_id=session_id,
        )
        OrderItem.objects.bulk_create([
            OrderItem(
                order=order,
                product=product,
                product_name=product.name,
                unit_amount=product.price,
                currency="usd",
                quantity=quantity,
                snapshot_quantity=quantity,
                price_id=f"price_manual_{index}",
            )
            for index, (product, quantity) in enumerate(items, start=1)
        ])
        return order

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


@override_settings(**ORDER_TEST_SETTINGS)
class OrderTestBase(OrderTestMixin, TestCase):
    """Orders and the Stripe webhook. Stripe itself is stubbed; the signature
    verification is not."""


def write_book_archive(directory, stem=EBOOK_STEM) -> Path:
    """A real (tiny) ZIP holding an EPUB and a PDF, as the contract requires."""
    path = Path(directory) / f"{stem}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{stem}.epub", "epub bytes")
        archive.writestr(f"{stem}.pdf", "%PDF-1.4 pdf bytes")
    return path


class BookAssetRootMixin:
    """Point BOOK_ASSET_ROOT at a throwaway directory holding one book."""

    stem = EBOOK_STEM

    def setUp(self):
        super().setUp()
        # resolve() because resolve_asset_path() compares resolved paths, and
        # a temp directory is a symlink on some platforms.
        self.asset_root = Path(tempfile.mkdtemp(prefix="pcfweb-books-")).resolve()
        self.addCleanup(shutil.rmtree, self.asset_root, True)
        settings_patch = override_settings(BOOK_ASSET_ROOT=str(self.asset_root))
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        self.archive = write_book_archive(self.asset_root, self.stem)


class CartTestBase(TestCase):
    """Cart tests: stubs Stripe out, since every CartProduct save hits it."""

    fixtures = ["initial_products"]

    def setUp(self):
        patcher = mock.patch("main.models.Payments")
        payments = patcher.start()
        self.addCleanup(patcher.stop)
        payments.create_product.return_value = "prod_test"
        payments.create_price.return_value = "price_test"
        Product.objects.filter(cat=Product.Categories.BOOKS).update(stock=99)

    def make_user(self, email="buyer@example.com", username="buyer"):
        return User.objects.create_user(
            username=username, email=email, password="hunter2hunter2")
