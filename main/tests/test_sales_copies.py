"""Copies of the mail a sale sends the buyer.

The owner sees every order through ``Order.notify_owner()``, but that is a
pick/pack list -- it is not the message that landed in the customer's inbox,
and it does not carry the download link or the receipt's wording. A copy is
what makes "the link in my email is broken" answerable without asking the
customer to forward anything.

The load-bearing property tested here is that the copy is its own message.
smtplib raises only when *every* envelope recipient is refused, so a copy
riding along on the buyer's envelope would turn a rejected buyer address into
a silent success -- and the callers stamp ``receipt_sent_at`` /
``digital_delivery_sent_at`` on anything that does not raise, which the
webhook then never retries. Hence ``test_the_buyer_is_alone_on_their_own_
envelope`` and ``test_a_failed_copy_does_not_cost_the_buyer_their_receipt``:
between them they say that nothing configured here can cost a sale.
"""
import os
import unittest

from unittest import mock

from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings

from main.mailing import subscribe
from main.models import MailingListMessage
from main.tests.base import (
    EBOOK_PK, OWNER_EMAIL, SALES_COPY_EMAIL, BookAssetRootMixin, OrderTestBase)
from main.utils import sales_copy_recipients, send_sales_email
from newsletter.models import Newsletter

from pigscanfly.settings import Base


class RecordingMessage:
    """Stands in for main.utils.EmailMessage, one instance per send.

    Patched as the *name* in main.utils rather than as an attribute on
    Django's shared EmailMessage class: replacing `EmailMessage.send` would
    break every other sender in the process for the duration, including
    anything a signal or fixture mails.
    """

    sent: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.fail_silently = None

    def send(self, fail_silently=False):
        self.fail_silently = fail_silently
        type(self).sent.append(self)
        if any("boom" in address for address in self.kwargs.get("to", [])):
            raise OSError("that relay is down")
        return 1


class SalesCopyTest(BookAssetRootMixin, OrderTestBase):
    """The receipt and the download email, copied to SALES_COPY_EMAILS."""

    def messages(self, needle):
        return [m for m in mail.outbox if needle in m.subject]

    def buyer_and_copy(self, needle):
        """The buyer's message and the owner's copy of it, in send order."""
        messages = self.messages(needle)
        self.assertEqual(len(messages), 2,
                         f"expected a {needle!r} message and one copy")
        buyer, copy = messages
        self.assertEqual(buyer.to, ["buyer@example.com"])
        self.assertEqual(copy.to, [SALES_COPY_EMAIL])
        return buyer, copy

    def test_the_receipt_is_copied_to_the_owner(self):
        order = self.place_order()

        self.deliver(self.event_body(order))

        buyer, copy = self.buyer_and_copy("Your receipt")
        # The copy is the real thing, not a summary of it.
        self.assertEqual(copy.subject, buyer.subject)
        self.assertEqual(copy.body, buyer.body)

    def test_the_download_email_is_copied_to_the_owner(self):
        # The one that matters most: this is the message carrying the link,
        # and a link that does not work is what a customer writes in about.
        order = self.place_order(product_pk=EBOOK_PK, quantity=1,
                                 session_id="cs_ebook")

        self.deliver(self.event_body(order))

        buyer, copy = self.buyer_and_copy("Your download")
        self.assertEqual(copy.body, buyer.body)
        # The owner can follow the same link the customer got.
        self.assertIn("/download/", copy.body)

    def test_the_buyer_is_alone_on_their_own_envelope(self):
        # The property the whole design rests on. smtplib raises only when
        # EVERY recipient is refused, so one recipient per envelope is what
        # keeps "the buyer's address was rejected" a recorded failure rather
        # than a send that quietly succeeded for somebody else.
        order = self.place_order(product_pk=EBOOK_PK, quantity=1,
                                 session_id="cs_ebook")

        self.deliver(self.event_body(order))

        for needle in ("Your receipt", "Your download"):
            buyer, _copy = self.buyer_and_copy(needle)
            self.assertEqual(buyer.recipients(), ["buyer@example.com"],
                             f"the {needle!r} envelope carried a second "
                             "recipient, so a refused buyer would not raise")
            self.assertEqual(buyer.bcc, [])
            self.assertEqual(buyer.cc, [])

    def test_the_customer_is_never_shown_who_was_copied(self):
        order = self.place_order()

        self.deliver(self.event_body(order))

        buyer, copy = self.buyer_and_copy("Your receipt")
        # A positive control first: assert the copy exists, so this cannot
        # pass by the feature having been removed.
        self.assertEqual(copy.to, [SALES_COPY_EMAIL])
        self.assertNotIn(SALES_COPY_EMAIL, buyer.message().as_string())

    def test_a_failed_copy_does_not_cost_the_buyer_their_receipt(self):
        # A copy that cannot be sent must not have the order recorded as
        # unsent: the webhook would retry it and mail the customer twice.
        order = self.place_order()
        order.customer_email = "buyer@example.com"
        order.save()
        RecordingMessage.sent = []

        with override_settings(SALES_COPY_EMAILS=["boom@example.com"]):
            with mock.patch("main.utils.EmailMessage", RecordingMessage):
                with self.assertLogs("main.utils", level="ERROR"):
                    self.assertTrue(order.send_receipt())

        buyer, copy = RecordingMessage.sent
        self.assertEqual(buyer.kwargs["to"], ["buyer@example.com"])
        self.assertEqual(copy.kwargs["to"], ["boom@example.com"])
        order.refresh_from_db()
        self.assertIsNotNone(order.receipt_sent_at)
        self.assertEqual(order.receipt_error, "")

    def test_a_failed_buyer_send_is_still_recorded_as_a_failure(self):
        order = self.place_order()
        order.customer_email = "boom@example.com"
        order.save()
        RecordingMessage.sent = []

        with mock.patch("main.utils.EmailMessage", RecordingMessage):
            with self.assertLogs("main.models", level="ERROR"):
                self.assertFalse(order.send_receipt())

        # The copy is never attempted: the buyer's send raises first, and the
        # caller records that rather than carrying on.
        self.assertEqual(len(RecordingMessage.sent), 1)
        order.refresh_from_db()
        self.assertIsNone(order.receipt_sent_at)
        self.assertIn("that relay is down", order.receipt_error)

    def test_both_sends_refuse_to_be_silent_about_a_failure(self):
        # fail_silently=True on either would have the backend swallow SMTP
        # errors and the order stamped as delivered. Nothing else pins it.
        order = self.place_order()
        order.customer_email = "buyer@example.com"
        order.save()
        RecordingMessage.sent = []

        with mock.patch("main.utils.EmailMessage", RecordingMessage):
            order.send_receipt()

        self.assertEqual(len(RecordingMessage.sent), 2)
        for message in RecordingMessage.sent:
            self.assertIs(message.fail_silently, False)

    def test_a_copy_address_that_is_also_the_buyer_does_not_double_up(self):
        with override_settings(SALES_COPY_EMAILS=["Buyer@Example.com"]):
            order = self.place_order()
            self.deliver(self.event_body(order))

        receipt, = self.messages("Your receipt")
        self.assertEqual(receipt.to, ["buyer@example.com"])

    def test_no_copy_address_configured_still_sends_the_buyer_theirs(self):
        with override_settings(SALES_COPY_EMAILS=[]):
            order = self.place_order()
            self.deliver(self.event_body(order))

        receipt, = self.messages("Your receipt")
        self.assertEqual(receipt.to, ["buyer@example.com"])
        order.refresh_from_db()
        self.assertIsNotNone(order.receipt_sent_at)

    def test_the_owner_notification_is_not_copied_to_the_owner_again(self):
        # It is already addressed to them; a copy here is one order arriving
        # twice, which is how a notification stops being read.
        order = self.place_order()

        self.deliver(self.event_body(order))

        notification, = self.order_emails()
        self.assertEqual(notification.to, [OWNER_EMAIL])
        self.assertEqual(notification.bcc, [])

    def test_a_receipt_that_cannot_be_sent_copies_nobody(self):
        # No customer address means no message at all -- not a copy on its
        # own, which would read as a delivery that went out. (place_order()
        # stops before the webhook, where Stripe's customer_details land.)
        order = self.place_order()
        self.assertEqual(order.customer_email, "")

        with self.assertLogs("main.models", level="WARNING"):
            self.assertFalse(order.send_receipt())

        self.assertEqual(mail.outbox, [])


class SalesCopyRecipientsTest(SimpleTestCase):
    """sales_copy_recipients() on its own, where the shapes are clearest."""

    @override_settings(SALES_COPY_EMAILS=["Copies@Example.com"])
    def test_the_address_is_sent_in_the_case_it_was_configured_in(self):
        # The local part is case-sensitive per the RFC; lower-casing
        # somebody's mailbox on the way out is not ours to do.
        self.assertEqual(sales_copy_recipients(), ["Copies@Example.com"])

    @override_settings(SALES_COPY_EMAILS=["Copies@Example.com"])
    def test_exclusion_is_case_insensitive_all_the_same(self):
        # ... but every host anybody uses treats them as one mailbox.
        self.assertEqual(
            sales_copy_recipients(exclude=["copies@EXAMPLE.com"]), [])

    @override_settings(SALES_COPY_EMAILS=["Owner <copies@example.com>"])
    def test_a_display_name_entry_is_still_matched_by_its_mailbox(self):
        # A perfectly good thing to type into an address list, and the relay
        # delivers it -- so the comparison key has to be the mailbox, or the
        # owner buying from their own shop gets two of everything.
        self.assertEqual(
            sales_copy_recipients(exclude=["copies@example.com"]), [])
        self.assertEqual(sales_copy_recipients(),
                         ["Owner <copies@example.com>"])

    @override_settings(SALES_COPY_EMAILS=["one@example.com", "One@example.com",
                                          "two@example.com"])
    def test_a_repeated_address_is_copied_once(self):
        self.assertEqual(sales_copy_recipients(),
                         ["one@example.com", "two@example.com"])

    @override_settings(SALES_COPY_EMAILS=[
        "holden@", "a@b.com; c@d.com", "@example.com", "good@example.com"])
    def test_an_unusable_entry_is_dropped_rather_than_breaking_the_send(self):
        # Django's SMTP backend sanitizes recipients before the send and
        # outside its own error handling, so an entry let through here would
        # raise ValueError instead of simply not being copied.
        with self.assertLogs("main.utils", level="WARNING") as logs:
            self.assertEqual(sales_copy_recipients(), ["good@example.com"])
        self.assertEqual(len(logs.records), 3)

    @override_settings(SALES_COPY_EMAILS="one@example.com,two@example.com")
    def test_a_bare_string_setting_is_not_read_one_letter_at_a_time(self):
        # Documented as a comma-separated string, so a direct assignment in
        # that shape must not become one recipient per character.
        self.assertEqual(sales_copy_recipients(),
                         ["one@example.com", "two@example.com"])

    @override_settings(SALES_COPY_EMAILS=[None, 3, "good@example.com"])
    def test_an_entry_that_is_not_even_a_string_is_survivable(self):
        with self.assertLogs("main.utils", level="WARNING"):
            self.assertEqual(sales_copy_recipients(), ["good@example.com"])

    @override_settings(SALES_COPY_EMAILS=[])
    def test_nothing_configured_copies_nobody(self):
        self.assertEqual(sales_copy_recipients(), [])

    @override_settings(SALES_COPY_EMAILS=["copies@example.com"],
                       DEFAULT_FROM_EMAIL="support@pigscanfly.ca")
    def test_both_messages_come_from_the_site_address(self):
        send_sales_email("Subject", "Body", ["buyer@example.com"])

        buyer, copy = mail.outbox
        self.assertEqual(buyer.from_email, "support@pigscanfly.ca")
        self.assertEqual(copy.from_email, "support@pigscanfly.ca")
        self.assertEqual(copy.to, ["copies@example.com"])

    @unittest.skipIf(os.getenv("SALES_COPY_EMAILS") is not None,
                     "the environment sets its own copy addresses")
    def test_the_shipped_default_copies_the_owner(self):
        # `is not None`, not truthiness: "" is the documented way to turn
        # copies off, and a falsy check would run this and fail instead.
        self.assertEqual(Base.SALES_COPY_EMAILS, ["holden@pigscanfly.ca"])


@override_settings(SALES_COPY_EMAILS=[SALES_COPY_EMAIL])
class MailingListIsNotCopiedTest(TestCase):
    """A mailing is one message per subscriber, so it is not copied.

    Applying the sale rule here would mean a copy of every mailing times
    every recipient -- a mailbox nobody can read, arriving exactly when the
    list is at its largest.
    """

    def setUp(self):
        # No fixture: the seeded lists come from migration 0014, not from
        # initial_products, which holds no newsletter rows.
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
            self.assertNotIn(SALES_COPY_EMAIL, copy.recipients())
