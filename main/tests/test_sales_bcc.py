"""Blind copies of the mail a sale sends the buyer.

The owner sees every order through ``Order.notify_owner()``, but that is a
pick/pack list -- it is not the message that landed in the customer's inbox,
and it does not carry the download link or the receipt's wording. A Bcc on
the buyer-facing sends is what makes "the link in my email is broken"
answerable without asking the customer to forward anything.

What is tested here is the shape of that copy rather than the fact of it:
one message rather than two, in the envelope rather than the headers, and
confined to the two sale emails -- a copy rule that reached the mailing list
would mean a copy of every mailing times every subscriber.
"""
import os
import unittest

from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings

from main.mailing import subscribe
from main.models import MailingListMessage
from main.tests.base import (
    EBOOK_PK, OWNER_EMAIL, SALES_BCC_EMAIL, BookAssetRootMixin, OrderTestBase)
from main.utils import sales_bcc, send_sales_email
from newsletter.models import Newsletter

from pigscanfly.settings import Base


class SalesBccTest(BookAssetRootMixin, OrderTestBase):
    """The receipt and the download email, copied to SALES_BCC_EMAILS."""

    def receipt(self):
        receipts = [m for m in mail.outbox if "Your receipt" in m.subject]
        self.assertEqual(len(receipts), 1, "expected exactly one receipt")
        return receipts[0]

    def download_email(self):
        downloads = [m for m in mail.outbox if "Your download" in m.subject]
        self.assertEqual(len(downloads), 1, "expected exactly one download")
        return downloads[0]

    def test_the_receipt_is_copied_to_the_owner(self):
        order = self.place_order()

        self.deliver(self.event_body(order))

        receipt = self.receipt()
        self.assertEqual(receipt.to, ["buyer@example.com"])
        self.assertEqual(receipt.bcc, [SALES_BCC_EMAIL])

    def test_the_download_email_is_copied_to_the_owner(self):
        # The one that matters most: this is the message carrying the link,
        # and a link that does not work is what a customer writes in about.
        order = self.place_order(product_pk=EBOOK_PK, quantity=1,
                                 session_id="cs_ebook")

        self.deliver(self.event_body(order))

        download = self.download_email()
        self.assertEqual(download.to, ["buyer@example.com"])
        self.assertEqual(download.bcc, [SALES_BCC_EMAIL])
        # The copy is the real thing, not a summary of it: the owner can
        # follow the same link the customer got.
        self.assertIn("/download/", download.body)

    def test_the_copy_is_the_same_message_and_not_a_second_one(self):
        # One message down one connection. Two sends could differ from each
        # other, and a failure of the second would be a failure to record.
        order = self.place_order()

        self.deliver(self.event_body(order))

        receipt = self.receipt()
        self.assertEqual(
            sorted(receipt.recipients()),
            sorted(["buyer@example.com", SALES_BCC_EMAIL]))

    def test_the_customer_is_never_shown_who_was_copied(self):
        # Blind means blind: Django puts Bcc in the envelope only, and this
        # is the assertion that keeps it that way if the send ever moves to
        # a header-building path of its own.
        order = self.place_order()

        self.deliver(self.event_body(order))

        rendered = self.receipt().message().as_string()
        headers, _, _ = rendered.partition("\n\n")
        self.assertNotIn("Bcc", headers)
        self.assertNotIn(SALES_BCC_EMAIL, headers)

    def test_a_copy_address_that_is_also_the_buyer_does_not_double_up(self):
        # How the checkout gets tested: the owner buys from their own shop.
        # Both entries are handed to the relay, so without this they get two
        # copies of their own receipt.
        with override_settings(SALES_BCC_EMAILS=["Buyer@Example.com"]):
            order = self.place_order()
            self.deliver(self.event_body(order))

        receipt = self.receipt()
        self.assertEqual(receipt.bcc, [])
        self.assertEqual(receipt.recipients(), ["buyer@example.com"])

    def test_no_copy_address_configured_still_sends_the_buyer_theirs(self):
        with override_settings(SALES_BCC_EMAILS=[]):
            order = self.place_order()
            self.deliver(self.event_body(order))

        receipt = self.receipt()
        self.assertEqual(receipt.bcc, [])
        self.assertEqual(receipt.to, ["buyer@example.com"])
        order.refresh_from_db()
        self.assertIsNotNone(order.receipt_sent_at)

    def test_the_owner_notification_is_not_copied_to_the_owner_again(self):
        # It is already addressed to them; a Bcc here is one order arriving
        # twice, which is how a notification stops being read.
        order = self.place_order()

        self.deliver(self.event_body(order))

        notification, = self.order_emails()
        self.assertEqual(notification.to, [OWNER_EMAIL])
        self.assertEqual(notification.bcc, [])

    def test_a_receipt_that_cannot_be_sent_copies_nobody(self):
        # No customer address means no message at all -- not an empty one to
        # the copy address, which would read as a delivery that went out.
        # (place_order() stops before the webhook, which is where Stripe's
        # customer_details land, so this order has no address on it yet.)
        order = self.place_order()
        self.assertEqual(order.customer_email, "")

        with self.assertLogs("main.models", level="WARNING"):
            self.assertFalse(order.send_receipt())

        self.assertEqual(mail.outbox, [])


class SalesBccHelperTest(TestCase):
    """sales_bcc() on its own, where the shapes are easiest to state."""

    @override_settings(SALES_BCC_EMAILS=["  copies@example.com  "])
    def test_a_configured_address_is_stripped(self):
        # A value pulled out of a file into a Secret carries a newline.
        self.assertEqual(sales_bcc(), ["copies@example.com"])

    @override_settings(SALES_BCC_EMAILS=["Copies@Example.com"])
    def test_the_address_is_sent_in_the_case_it_was_configured_in(self):
        # The local part is case-sensitive per the RFC; lower-casing
        # somebody's mailbox on the way out is not ours to do.
        self.assertEqual(sales_bcc(), ["Copies@Example.com"])

    @override_settings(SALES_BCC_EMAILS=["Copies@Example.com"])
    def test_exclusion_is_case_insensitive_all_the_same(self):
        # ... but every host anybody uses treats them as one mailbox, so a
        # case variant of the recipient is still the recipient.
        self.assertEqual(sales_bcc(exclude=["copies@EXAMPLE.com"]), [])

    @override_settings(SALES_BCC_EMAILS=["one@example.com", "one@example.com",
                                         "two@example.com"])
    def test_a_repeated_address_is_copied_once(self):
        self.assertEqual(sales_bcc(), ["one@example.com", "two@example.com"])

    @override_settings(SALES_BCC_EMAILS=["", "   "])
    def test_blank_entries_are_not_recipients(self):
        self.assertEqual(sales_bcc(), [])

    @override_settings(SALES_BCC_EMAILS=["copies@example.com"],
                       DEFAULT_FROM_EMAIL="support@pigscanfly.ca")
    def test_a_send_carries_the_from_address_the_rest_of_the_site_uses(self):
        send_sales_email("Subject", "Body", ["buyer@example.com"])

        sent, = mail.outbox
        self.assertEqual(sent.from_email, "support@pigscanfly.ca")
        self.assertEqual(sent.to, ["buyer@example.com"])
        self.assertEqual(sent.bcc, ["copies@example.com"])

    @override_settings(SALES_BCC_EMAILS=["copies@example.com"])
    def test_a_send_failure_is_raised_rather_than_swallowed(self):
        # The callers record it on the order and answer Stripe 2xx anyway;
        # that decision is theirs and must not be pre-empted here.
        with mock.patch("main.utils.EmailMessage.send",
                        side_effect=OSError("SMTP is down")):
            with self.assertRaises(OSError):
                send_sales_email("Subject", "Body", ["buyer@example.com"])

    @unittest.skipIf(os.getenv("SALES_BCC_EMAILS"),
                     "the environment sets its own copy addresses")
    def test_the_shipped_default_copies_the_owner(self):
        # Pinned so a refactor cannot quietly turn the copies off: the
        # setting is only doing anything if it has an address in it.
        self.assertEqual(Base.SALES_BCC_EMAILS, ["holden@pigscanfly.ca"])


@override_settings(SALES_BCC_EMAILS=[SALES_BCC_EMAIL])
class MailingListIsNotCopiedTest(TestCase):
    """A mailing is one message per subscriber, so it is not copied.

    Applying the sale rule here would mean a copy of every mailing times
    every recipient -- a mailbox nobody can read, and one that would arrive
    exactly when the list is at its largest.
    """

    fixtures = ["initial_products"]

    def setUp(self):
        self.general = Newsletter.objects.get(slug="general")
        for address in ("first@example.com", "second@example.com"):
            subscription, _ = subscribe(address, self.general)
            subscription.update("subscribe")
        mail.outbox.clear()

    def test_a_mailing_goes_only_to_its_subscribers(self):
        message = MailingListMessage.objects.create(
            subject="News", body="Some news.")
        message.interests.add(self.general)

        sent, failed = message.send_batch()

        self.assertEqual((sent, failed), (2, 0))
        self.assertEqual(len(mail.outbox), 2)
        for copy in mail.outbox:
            self.assertEqual(copy.bcc, [])
            self.assertNotIn(SALES_BCC_EMAIL, copy.recipients())
