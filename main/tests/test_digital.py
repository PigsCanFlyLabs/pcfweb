"""Tests for the physical/digital axis and digital fulfilment.

Covers the delivery-type flag itself, what it does to checkout and the
cart page, the distribution-rights interlock, the asset-path hardening
behind the download endpoint, and delivery from the webhook."""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core import mail
from django.test import RequestFactory, TestCase, override_settings

from main import digital
from main.digital import (
    BadSignature, DigitalAssetError, SignatureExpired, make_download_token,
    resolve_asset_path)
from main.models import Cart, CartProduct, Order, OrderItem, Product
from main.payments import Payments
from main.tests.base import (
    assert_never_cache_response,
    EBOOK_PK,
    EBOOK_STEM,
    SHIPPING_NOTICE_TEXT,
    BookAssetRootMixin,
    CartTestBase,
    OrderTestBase,
)


MEDIA_MAIL_RATE = "shr_0MJrL4nkDnSOC1s7cPSy15CO"


class DeliveryTypeTest(TestCase):
    """Part 1: "physical" is now stated, not inferred from mode and category."""

    def _product(self, **kwargs):
        kwargs.setdefault("external_product_id", "prod_preexisting")
        kwargs.setdefault("price", 1000)
        return Product.objects.create(name="Thing", **kwargs)

    def test_products_are_physical_by_default(self):
        product = self._product()
        self.assertEqual(product.delivery_type,
                         Product.DeliveryTypes.PHYSICAL)
        self.assertTrue(product.is_physical_good())

    def test_a_digital_product_is_not_a_physical_good(self):
        # The old expression called this physical -- payment mode, not the
        # services category -- which is what offered media mail on a PDF.
        product = self._product(
            delivery_type=Product.DeliveryTypes.DIGITAL,
            cat=Product.Categories.BOOKS,
            mode=Product.Modes.PAYMENT)
        self.assertFalse(product.is_physical_good())

    def test_a_service_is_not_a_physical_good(self):
        product = self._product(
            delivery_type=Product.DeliveryTypes.SERVICE,
            cat=Product.Categories.SERVICES)
        self.assertFalse(product.is_physical_good())

    def test_being_physical_no_longer_depends_on_mode_or_category(self):
        # A subscription box is posted; a downloadable one-off is not. Neither
        # was expressible before.
        posted_subscription = self._product(
            mode=Product.Modes.SUBSCRIPTION,
            cat=Product.Categories.MERCH,
            delivery_type=Product.DeliveryTypes.PHYSICAL)
        downloadable_payment = self._product(
            mode=Product.Modes.PAYMENT,
            cat=Product.Categories.MERCH,
            delivery_type=Product.DeliveryTypes.DIGITAL)

        self.assertTrue(posted_subscription.is_physical_good())
        self.assertFalse(downloadable_payment.is_physical_good())


class DigitalCheckoutShippingTest(TestCase):
    """Part 1: shipping follows "is anything actually posted", not the mode."""

    def setUp(self):
        self.factory = RequestFactory()

    def _cart(self, *products):
        cart = Cart.objects.create()
        for index, product in enumerate(products):
            cart_product = CartProduct.objects.create(
                cart=cart, product=product, quantity=1,
                price_id=f"price_{product.pk}_{index}")
            cart.products.add(cart_product)
        return cart

    def _product(self, name, **kwargs):
        kwargs.setdefault("price", 1500)
        return Product.objects.create(
            name=name, external_product_id=f"prod_{name}", **kwargs)

    def _digital(self, **kwargs):
        return self._product(
            "ebook", delivery_type=Product.DeliveryTypes.DIGITAL,
            cat=Product.Categories.BOOKS, **kwargs)

    def _physical(self):
        return self._product("print", cat=Product.Categories.BOOKS)

    def _checkout(self, cart):
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_x")
            Payments.checkout(self.factory.get("/checkout"), cart)
        return create.call_args.kwargs

    def test_a_digital_only_cart_asks_for_no_mailing_address(self):
        params = self._checkout(self._cart(self._digital()))

        self.assertEqual(params["mode"], "payment")
        self.assertNotIn("shipping_address_collection", params)
        self.assertNotIn("shipping_options", params)

    def test_a_digital_only_cart_is_not_offered_media_mail(self):
        # The specific absurdity this whole axis exists to prevent.
        params = self._checkout(self._cart(self._digital()))
        self.assertNotIn(MEDIA_MAIL_RATE, json.dumps(params, default=str))

    def test_a_physical_cart_still_collects_an_address_and_offers_rates(self):
        params = self._checkout(self._cart(self._physical()))

        self.assertEqual(
            params["shipping_address_collection"],
            {"allowed_countries": ["US", "CA"]})
        self.assertIn(MEDIA_MAIL_RATE, json.dumps(params, default=str))

    def test_a_mixed_cart_still_collects_an_address(self):
        # One posted item in the cart means the buyer has to be asked where.
        params = self._checkout(self._cart(self._digital(), self._physical()))

        self.assertIn("shipping_address_collection", params)
        self.assertIn("shipping_options", params)

    def test_a_service_only_cart_asks_for_no_address(self):
        service = self._product(
            "consulting", cat=Product.Categories.SERVICES,
            delivery_type=Product.DeliveryTypes.SERVICE)
        params = self._checkout(self._cart(service))

        self.assertNotIn("shipping_address_collection", params)
        self.assertNotIn("shipping_options", params)


class DigitalCartPageTest(CartTestBase):
    """views.py's has_physical follows is_physical_good(), so it should too."""

    def test_a_digital_only_cart_hides_the_shipping_notice(self):
        self.client.post(f"/add-to-cart/{EBOOK_PK}/1")
        response = self.client.get("/cart")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SHIPPING_NOTICE_TEXT)

    def test_adding_a_printed_copy_brings_the_shipping_notice_back(self):
        self.client.get("/cart")
        cart = Cart.objects.get(cart_id=self.client.session["cart_id"])
        ebook = CartProduct.objects.create(
            cart=cart, product=Product.objects.get(pk=EBOOK_PK), quantity=1)
        printed = CartProduct.objects.create(
            cart=cart, product=Product.objects.get(pk=104), quantity=1)
        cart.products.add(ebook, printed)
        response = self.client.get("/cart")

        self.assertContains(response, SHIPPING_NOTICE_TEXT)

    def test_the_ebook_page_hides_the_shipping_notice(self):
        response = self.client.get(f"/product/{EBOOK_PK}")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SHIPPING_NOTICE_TEXT)

    def test_a_pay_what_you_want_cart_says_the_total_is_a_suggestion(self):
        self.client.post(f"/add-to-cart/{EBOOK_PK}/1")
        response = self.client.get("/cart")

        self.assertContains(response, "pay-what-you-want")


class EbookRightsInterlockTest(TestCase):
    """Part 2: sells_ebook is a licence, and it wins over the dropdown."""

    fixtures = ["initial_products"]

    def test_the_oreilly_titles_are_not_ours_to_hand_out(self):
        for pk in (100, 101, 102, 103):
            with self.subTest(pk=pk):
                book = Product.objects.get(pk=pk)
                self.assertFalse(book.sells_ebook)
                self.assertFalse(book.is_digitally_fulfilled())

    def test_sells_ebook_defaults_to_false(self):
        product = Product.objects.create(
            name="New book", external_product_id="prod_x", price=1000)
        self.assertFalse(product.sells_ebook)

    def test_a_digital_product_we_may_not_distribute_is_not_fulfillable(self):
        # The mis-set-dropdown case: somebody marks an O'Reilly title DIGITAL
        # in the admin. Marking it so must not make it sendable.
        # update() rather than save(): fixture rows carry no Stripe product
        # id, and Product.save() would go and mint one for real.
        Product.objects.filter(pk=100).update(
            delivery_type=Product.DeliveryTypes.DIGITAL,
            digital_asset_name="learning_spark_1ed")
        book = Product.objects.get(pk=100)

        self.assertTrue(book.delivery_type == Product.DeliveryTypes.DIGITAL)
        self.assertFalse(book.is_digitally_fulfilled())

    def test_both_halves_are_required(self):
        cases = [
            (Product.DeliveryTypes.DIGITAL, True, True),
            (Product.DeliveryTypes.DIGITAL, False, False),
            (Product.DeliveryTypes.PHYSICAL, True, False),
            (Product.DeliveryTypes.PHYSICAL, False, False),
        ]
        for delivery_type, sells_ebook, expected in cases:
            with self.subTest(delivery_type=delivery_type,
                              sells_ebook=sells_ebook):
                product = Product(
                    delivery_type=delivery_type, sells_ebook=sells_ebook)
                self.assertEqual(product.is_digitally_fulfilled(), expected)


class DigitalAssetPathTest(BookAssetRootMixin, TestCase):
    """Part 4, the security core.

    Product.digital_asset_name is typed into the Django admin, so it is
    hostile input, and whatever it names gets read off disk and handed to a
    customer. A stem that could escape the asset directory is an arbitrary
    file read with an email delivery mechanism attached.
    """

    def test_a_valid_stem_resolves_inside_the_asset_root(self):
        path = resolve_asset_path(self.stem)

        self.assertEqual(path, self.asset_root / f"{self.stem}.zip")
        self.assertEqual(path.parent, self.asset_root)

    def test_traversal_attempts_are_refused(self):
        hostile = [
            "../../etc/passwd",
            "../../../../../../etc/shadow",
            "..",
            "../secrets",
            "/etc/passwd",
            "etc/passwd",
            "books/../../etc/passwd",
            "distributed_computing_4_kids/../../etc/passwd",
            r"..\..\windows\system32\config\sam",
            "\\\\server\\share\\secret",
        ]
        for stem in hostile:
            with self.subTest(stem=stem):
                with self.assertRaises(DigitalAssetError):
                    resolve_asset_path(stem)

    def test_names_that_smuggle_a_path_or_extension_are_refused(self):
        # The filename is built in code; nothing may bring its own suffix,
        # separator, whitespace or null byte along.
        for stem in ["book.zip", "book.pdf", "book name", "book\x00.zip",
                     "book/", "/book", "book\n../etc/passwd", ".hidden",
                     "Book", "BOOK", "book-1", "b", "", "_book", "book_"]:
            with self.subTest(stem=stem):
                with self.assertRaises(DigitalAssetError):
                    resolve_asset_path(stem)

    def test_a_non_string_stem_is_refused(self):
        for stem in [None, 17, b"book", ["book"]]:
            with self.subTest(stem=stem):
                with self.assertRaises(DigitalAssetError):
                    resolve_asset_path(stem)

    def test_a_symlink_out_of_the_asset_root_is_refused(self):
        # The pattern check cannot see this one: the stem is perfectly valid
        # and the escape happens on disk. This is why containment is asserted
        # on the *resolved* path.
        secret = Path(tempfile.mkdtemp(prefix="pcfweb-secret-")).resolve()
        self.addCleanup(shutil.rmtree, secret, True)
        (secret / "loot.zip").write_bytes(b"PK\x03\x04secrets")
        os.symlink(secret / "loot.zip", self.asset_root / "sneaky_book.zip")

        with self.assertRaises(DigitalAssetError):
            resolve_asset_path("sneaky_book")

    def test_opening_a_missing_archive_reports_it_rather_than_crashing(self):
        with self.assertRaises(DigitalAssetError) as raised:
            digital.open_asset("no_such_book")

        self.assertIn("missing", str(raised.exception))

    def test_opening_a_present_archive_yields_the_bytes(self):
        with digital.open_asset(self.stem) as handle:
            self.assertEqual(handle.read(4), b"PK\x03\x04")


class DownloadTokenTest(TestCase):
    """Part 4: the signed link itself."""

    def test_a_token_round_trips(self):
        token = make_download_token(42, EBOOK_PK)
        self.assertEqual(
            digital.parse_download_token(token), (42, EBOOK_PK))

    def test_a_tampered_token_is_refused(self):
        token = make_download_token(42, EBOOK_PK)
        for tampered in [token.replace("42", "43", 1), token[:-1],
                         token + "x", "not-a-token", ""]:
            with self.subTest(token=tampered):
                with self.assertRaises(BadSignature):
                    digital.parse_download_token(tampered)

    def test_a_token_expires(self):
        token = make_download_token(42, EBOOK_PK)
        with override_settings(DIGITAL_DOWNLOAD_MAX_AGE=-1):
            with self.assertRaises(SignatureExpired):
                digital.parse_download_token(token)

    def test_the_default_expiry_is_seven_days(self):
        self.assertEqual(digital.link_lifetime_days(), 7)

    def test_a_correctly_signed_but_unusable_payload_is_refused(self):
        # Signed with our own key, so the signature checks out; the payload
        # is still not something this app minted.
        from django.core.signing import TimestampSigner
        token = TimestampSigner(
            salt=digital.DOWNLOAD_TOKEN_SALT).sign("../../etc/passwd")

        with self.assertRaises(BadSignature):
            digital.parse_download_token(token)


class DigitalDownloadViewTest(BookAssetRootMixin, TestCase):
    """Part 4: who is allowed to fetch a book, and who is not."""

    fixtures = ["initial_products"]

    def setUp(self):
        super().setUp()
        self.ebook = Product.objects.get(pk=EBOOK_PK)
        self.order = self._paid_order(self.ebook)

    def _paid_order(self, product, status=None, session="cs_dl"):
        order = Order.objects.create(
            status=status or Order.Status.PAID,
            customer_email="buyer@example.com",
            stripe_session_id=session)
        OrderItem.objects.create(
            order=order, product=product, product_name=product.name,
            unit_amount=product.price, quantity=1, snapshot_quantity=1)
        return order

    def _get(self, token):
        return self.client.get(f"/download/{token}")

    def _valid_token(self):
        return make_download_token(self.order.pk, self.ebook.pk)

    def test_a_valid_link_streams_the_archive(self):
        response = self._get(self._valid_token())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn(f'filename="{EBOOK_STEM}.zip"',
                      response["Content-Disposition"])
        self.assertEqual(
            b"".join(response.streaming_content), self.archive.read_bytes())

    def test_a_valid_link_is_marked_uncacheable(self):
        response = self._get(self._valid_token())

        self.assertEqual(response.status_code, 200)
        assert_never_cache_response(self, response)
        response.close()

    def test_a_tampered_token_gets_nothing(self):
        token = self._valid_token()
        response = self._get(token[:-3] + "aaa")

        self.assertEqual(response.status_code, 404)

    def test_an_expired_token_says_so_rather_than_404ing(self):
        # A real customer with a real receipt; "not found" would be a lie and
        # would not tell them what to do next.
        with override_settings(DIGITAL_DOWNLOAD_MAX_AGE=-1):
            response = self._get(self._valid_token())

        self.assertEqual(response.status_code, 410)
        self.assertIn(b"expired", response.content)

    def test_a_token_for_a_different_order_gets_nothing(self):
        # Someone else's order, which never bought this book.
        other = Order.objects.create(
            status=Order.Status.PAID, stripe_session_id="cs_other")

        response = self._get(make_download_token(other.pk, self.ebook.pk))

        self.assertEqual(response.status_code, 404)

    def test_a_token_for_an_order_that_was_never_paid_gets_nothing(self):
        pending = self._paid_order(
            self.ebook, status=Order.Status.PENDING, session="cs_pending")

        response = self._get(make_download_token(pending.pk, self.ebook.pk))

        self.assertEqual(response.status_code, 404)

    def test_a_token_for_a_product_not_on_the_order_gets_nothing(self):
        response = self._get(make_download_token(self.order.pk, 100))

        self.assertEqual(response.status_code, 404)

    def test_a_token_for_a_nonexistent_order_gets_nothing(self):
        response = self._get(make_download_token(999999, self.ebook.pk))

        self.assertEqual(response.status_code, 404)

    def test_the_rights_interlock_applies_at_serve_time_too(self):
        # Rights revoked after the sale: old links have to stop working.
        Product.objects.filter(pk=EBOOK_PK).update(sells_ebook=False)

        response = self._get(self._valid_token())

        self.assertEqual(response.status_code, 404)

    def test_a_hostile_stem_in_the_database_reads_no_file(self):
        # The whole traversal scenario, end to end: somebody edits the stem in
        # the admin and waits for a customer to click their link.
        Product.objects.filter(pk=EBOOK_PK).update(
            digital_asset_name="../../../../etc/passwd")

        with self.assertLogs("main.views", level="ERROR"):
            response = self._get(self._valid_token())

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"root:", response.content)

    def test_a_missing_archive_is_a_404_not_a_500(self):
        self.archive.unlink()

        with self.assertLogs("main.views", level="ERROR"):
            response = self._get(self._valid_token())

        self.assertEqual(response.status_code, 404)

    def test_a_fulfilled_order_can_still_download(self):
        Order.objects.filter(pk=self.order.pk).update(
            status=Order.Status.FULFILLED)

        self.assertEqual(self._get(self._valid_token()).status_code, 200)


class DigitalDeliveryTest(BookAssetRootMixin, OrderTestBase):
    """Part 4: delivery fires from the webhook, exactly once, and never
    breaks it."""

    def customer_emails(self):
        return [m for m in mail.outbox
                if "Your download" in m.subject]

    def buy_the_ebook(self, session_id="cs_ebook"):
        return self.place_order(
            product_pk=EBOOK_PK, quantity=1, session_id=session_id)

    def test_paying_for_the_ebook_emails_a_working_download_link(self):
        order = self.buy_the_ebook()

        response = self.deliver(self.event_body(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIsNotNone(order.digital_delivery_sent_at)
        self.assertEqual(order.digital_delivery_error, "")

        email, = self.customer_emails()
        self.assertEqual(email.to, ["buyer@example.com"])
        self.assertIn("7 days", email.body)
        self.assertIn("EPUB", email.body)

        # The link in the email is a link, not an attachment, and it works.
        self.assertEqual(email.attachments, [])
        url, = re.findall(r"https://\S+/download/\S+", email.body)
        response = self.client.get(url.replace("https://www.pigscanfly.ca", ""))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            b"".join(response.streaming_content), self.archive.read_bytes())

    def test_a_physical_order_sends_the_customer_nothing(self):
        order = self.place_order(product_pk=104, quantity=1)

        self.deliver(self.event_body(order))

        order.refresh_from_db()
        self.assertIsNone(order.digital_delivery_sent_at)
        self.assertEqual(order.digital_delivery_error, "")
        self.assertEqual(self.customer_emails(), [])

    def test_a_book_we_may_not_distribute_is_never_sent(self):
        # The interlock, exercised through the whole checkout and webhook:
        # DIGITAL and downloadable-looking, but not ours to give away.
        Product.objects.filter(pk=EBOOK_PK).update(sells_ebook=False)
        order = self.buy_the_ebook()

        response = self.deliver(self.event_body(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIsNone(order.digital_delivery_sent_at)
        self.assertEqual(self.customer_emails(), [])

    def test_a_missing_archive_leaves_the_order_paid_and_records_why(self):
        # Stripe must not be made to retry, and the sale must not be lost,
        # because a file is not on disk.
        self.archive.unlink()
        order = self.buy_the_ebook()

        with self.assertLogs("main.models", level="ERROR"):
            response = self.deliver(self.event_body(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIsNone(order.digital_delivery_sent_at)
        self.assertIn("missing", order.digital_delivery_error)
        self.assertEqual(self.customer_emails(), [])

    def test_the_owner_is_told_loudly_when_a_download_was_not_delivered(self):
        # Nothing else would ever tell them somebody paid and got nothing.
        self.archive.unlink()
        order = self.buy_the_ebook()

        with self.assertLogs("main.models", level="ERROR"):
            self.deliver(self.event_body(order))

        owner_email, = self.order_emails()
        self.assertIn("NOT DELIVERED", owner_email.body)
        self.assertIn("missing", owner_email.body)

    def test_the_owner_is_told_when_a_download_was_delivered(self):
        order = self.buy_the_ebook()

        self.deliver(self.event_body(order))

        owner_email, = self.order_emails()
        self.assertIn("Download links emailed to buyer@example.com",
                      owner_email.body)

    def test_a_bad_stem_leaves_the_order_paid_and_records_why(self):
        Product.objects.filter(pk=EBOOK_PK).update(
            digital_asset_name="../../etc/passwd")
        order = self.buy_the_ebook()

        with self.assertLogs("main.models", level="ERROR"):
            response = self.deliver(self.event_body(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIn("not a usable digital asset name",
                      order.digital_delivery_error)
        self.assertEqual(self.customer_emails(), [])

    def test_an_order_with_no_customer_email_records_the_problem(self):
        order = self.buy_the_ebook()

        response = self.deliver(
            self.event_body(order, customer_details={"email": "", "name": ""}))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIn("no customer email", order.digital_delivery_error)
        self.assertEqual(self.customer_emails(), [])

    def test_a_mail_failure_leaves_the_order_paid_and_records_why(self):
        order = self.buy_the_ebook()

        with mock.patch("main.models.send_sales_email",
                        side_effect=OSError("SMTP is down")):
            with self.assertLogs("main.models", level="ERROR"):
                response = self.deliver(self.event_body(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIsNone(order.digital_delivery_sent_at)
        self.assertIn("SMTP is down", order.digital_delivery_error)

    def test_a_redelivered_webhook_does_not_send_the_book_twice(self):
        # Stripe retries for three days. Delivery sits behind the same
        # conditional UPDATE as the owner notification, so only the delivery
        # that actually moved PENDING -> PAID sends anything.
        order = self.buy_the_ebook()
        body = self.event_body(order)

        self.assertEqual(self.deliver(body).status_code, 200)
        self.assertEqual(self.deliver(body).status_code, 200)

        self.assertEqual(len(self.customer_emails()), 1)
        self.assertEqual(len(self.order_emails()), 1)

    def test_a_mixed_order_ships_the_print_copy_and_emails_the_ebook(self):
        order = self.manual_order(
            (Product.objects.get(pk=EBOOK_PK), 1),
            (Product.objects.get(pk=104), 1),
            session_id="cs_mixed",
        )

        self.deliver(self.event_body(order))

        order.refresh_from_db()
        self.assertIsNotNone(order.digital_delivery_sent_at)
        email, = self.customer_emails()
        # Only the e-book is downloadable; the printed copy still gets posted.
        self.assertEqual(len(re.findall(r"/download/", email.body)), 1)
        owner_email, = self.order_emails()
        self.assertIn("2 Ship Lane", owner_email.body)

    def test_a_zero_total_ebook_order_still_delivers_the_book(self):
        # The two new behaviours meeting: a pay-what-you-want buyer who paid
        # nothing still bought the book, and still gets it.
        order = self.buy_the_ebook()

        self.deliver(self.event_body(
            order, payment_status="no_payment_required",
            amount_total=0, amount_subtotal=0,
            total_details={"amount_tax": 0, "amount_shipping": 0}))

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.amount_total, 0)
        self.assertIsNotNone(order.digital_delivery_sent_at)
        self.assertEqual(len(self.customer_emails()), 1)

    def test_the_owner_email_flags_a_pay_what_you_want_line(self):
        # The snapshot holds the suggested amount, so the line reads 15.00
        # whatever the buyer actually paid.
        order = self.buy_the_ebook()

        self.deliver(self.event_body(
            order, amount_total=0, amount_subtotal=0,
            payment_status="no_payment_required",
            total_details={"amount_tax": 0, "amount_shipping": 0}))

        owner_email, = self.order_emails()
        self.assertIn("pay-what-you-want", owner_email.body)


class WithheldDigitalDeliveryTest(BookAssetRootMixin, OrderTestBase):
    """An interlock that fires silently is worse than no interlock.

    sells_ebook stopping a delivery is the system working as designed. But the
    customer has still paid and still received nothing, so the refusal has to
    be as loud as any other failure -- it must never be indistinguishable from
    "this order simply had no downloads on it".
    """

    def customer_emails(self):
        return [m for m in mail.outbox if "Your download" in m.subject]

    def withhold_the_ebook(self):
        # The mis-set-dropdown scenario: DIGITAL, but no distribution rights.
        Product.objects.filter(pk=EBOOK_PK).update(sells_ebook=False)

    def test_a_withheld_book_sends_nothing_and_is_recorded(self):
        self.withhold_the_ebook()
        order = self.place_order(product_pk=EBOOK_PK, quantity=1)

        with self.assertLogs("main.models", level="ERROR"):
            response = self.deliver(self.event_body(order))

        # The order stays PAID and the webhook still answers 200.
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        # Nothing was sent...
        self.assertIsNone(order.digital_delivery_sent_at)
        self.assertEqual(self.customer_emails(), [])
        # ...and the reason is recorded, naming the product.
        self.assertTrue(order.digital_delivery_error)
        self.assertIn("E-book", order.digital_delivery_error)
        self.assertIn("sells_ebook", order.digital_delivery_error)

    def test_the_owner_is_told_a_book_was_withheld(self):
        # The record is worthless if it never reaches someone who can act.
        self.withhold_the_ebook()
        order = self.place_order(product_pk=EBOOK_PK, quantity=1)

        with self.assertLogs("main.models", level="ERROR"):
            self.deliver(self.event_body(order))

        owner_email, = self.order_emails()
        self.assertIn("NOT DELIVERED", owner_email.body)
        self.assertIn("sells_ebook", owner_email.body)
        # And says what the two ways out are.
        self.assertIn("resend", owner_email.body)
        self.assertIn("refund", owner_email.body)

    def test_an_order_with_no_downloads_on_it_records_nothing(self):
        # The two cases have to stay distinguishable: noise above, silence
        # here. If a plain printed-book order also produced a digital-delivery
        # section, the section would stop meaning anything.
        order = self.place_order(product_pk=104, quantity=1)

        self.deliver(self.event_body(order))

        order.refresh_from_db()
        self.assertEqual(order.digital_delivery_error, "")
        self.assertIsNone(order.digital_delivery_sent_at)
        owner_email, = self.order_emails()
        self.assertNotIn("Digital delivery", owner_email.body)

    def test_a_withheld_line_does_not_cost_the_customer_a_deliverable_one(self):
        # One product we may not distribute must not swallow the one we may.
        Product.objects.filter(pk=100).update(
            delivery_type=Product.DeliveryTypes.DIGITAL, sells_ebook=False)
        order = self.manual_order(
            (Product.objects.get(pk=EBOOK_PK), 1),
            (Product.objects.get(pk=100), 1),
            session_id="cs_partial",
        )

        with self.assertLogs("main.models", level="ERROR"):
            self.deliver(self.event_body(order))

        order.refresh_from_db()
        self.assertIsNotNone(order.digital_delivery_sent_at)
        self.assertIn("Learning Spark", order.digital_delivery_error)
        # Exactly the licensed one was sent.
        email, = self.customer_emails()
        self.assertEqual(len(re.findall(r"/download/", email.body)), 1)
        # And the owner is told this was not the whole order.
        owner_email, = self.order_emails()
        self.assertIn("NOT EVERYTHING ON THIS ORDER WAS DELIVERED",
                      owner_email.body)

    def test_a_withheld_book_is_still_refused_by_the_download_endpoint(self):
        # Belt and braces: even if a link had somehow been issued for it.
        self.withhold_the_ebook()
        order = self.place_order(product_pk=EBOOK_PK, quantity=1)
        with self.assertLogs("main.models", level="ERROR"):
            self.deliver(self.event_body(order))

        response = self.client.get(
            f"/download/{make_download_token(order.pk, EBOOK_PK)}")

        self.assertEqual(response.status_code, 404)

    def test_the_refusal_names_every_withheld_product(self):
        Product.objects.filter(pk__in=[100, EBOOK_PK]).update(
            delivery_type=Product.DeliveryTypes.DIGITAL, sells_ebook=False)
        order = self.manual_order(
            (Product.objects.get(pk=EBOOK_PK), 1),
            (Product.objects.get(pk=100), 1),
            session_id="cs_both",
        )

        with self.assertLogs("main.models", level="ERROR"):
            self.deliver(self.event_body(order))

        order.refresh_from_db()
        self.assertIn("E-book", order.digital_delivery_error)
        self.assertIn("Learning Spark", order.digital_delivery_error)
        self.assertEqual(self.customer_emails(), [])
