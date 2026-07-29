"""Targeted tests for the two mailing-list bugs from the PR #22 review:

(a) Suppression is case-sensitive: a suppressed address in one case
    should exclude a subscriber in any case, but the subquery may not
    lower-case properly.
(b) A surname-first CSV incorrectly classifies the surname column as 'name'
    and silently drops the given name.
"""
import io

from django.core import mail
from django.test import TestCase

from main import mailing
from main.models import (
    MailingListDelivery, MailingListMessage, SuppressedAddress)
from newsletter.models import Newsletter, Subscription


class SuppressionCaseInsensitivityTest(TestCase):
    """Bug (a): the send-time suppression check must be case-insensitive.

    A suppressed address stored as Foo@Example.com must exclude a subscriber
    stored as foo@example.com (and vice versa). If the subquery does not
    lower-case, the subscriber slips through and gets mailed.
    """

    def setUp(self):
        self.general = Newsletter.objects.get(slug="general")

    def _make_subscriber(self, email, newsletter=None):
        newsletter = newsletter or self.general
        sub = Subscription.objects.create(
            newsletter=newsletter, email_field=email,
            subscribed=True, unsubscribed=False)
        return sub

    def _make_suppressed(self, email):
        return SuppressedAddress.objects.create(email=email, reason="test")

    def _recipient_count(self, msg):
        return msg.recipients().count()

    def test_suppressed_upper_excludes_subscriber_lower(self):
        """Suppressed as Foo@Example.com, subscriber is foo@example.com."""
        self._make_suppressed("Foo@Example.com")
        self._make_subscriber("foo@example.com")
        msg = MailingListMessage.objects.create(subject="S", body="B")
        self.assertEqual(
            self._recipient_count(msg), 0,
            "Suppressed Foo@Example.com should exclude subscriber "
            "foo@example.com")
        # Double-check: the suppressed address itself is case-normalised on
        # save, so the DB row *should* be lower-case. If it is, this bug
        # is already fixed. If the row is still Foo@Example.com, the
        # suppression check has to lower-case the right-hand side too.
        suppressed_row = SuppressedAddress.objects.first()
        self.assertEqual(
            suppressed_row.email, "foo@example.com",
            "SuppressedAddress.save() normalises case, so the row is "
            "lower-case. If this fails, save() is not normalising.")

    def test_suppressed_lower_excludes_subscriber_upper(self):
        """Suppressed as foo@example.com, subscriber is Foo@Example.com."""
        self._make_suppressed("foo@example.com")
        self._make_subscriber("Foo@Example.com")
        msg = MailingListMessage.objects.create(subject="S", body="B")
        self.assertEqual(
            self._recipient_count(msg), 0,
            "Suppressed foo@example.com should exclude subscriber "
            "Foo@Example.com")

    def test_suppressed_via_matching_method(self):
        """The matching() classmethod is already case-insensitive."""
        self._make_suppressed("Foo@Example.com")
        matched = SuppressedAddress.matching(["foo@example.com"])
        self.assertIn("foo@example.com", matched)

    def test_suppressed_via_bulk_create_no_save(self):
        """A suppressed address created via bulk_create bypasses save()
        and stays in its original case. The exclusion must still work."""
        SuppressedAddress.objects.bulk_create([
            SuppressedAddress(email="Foo@Example.com", reason="test"),
        ])
        self._make_subscriber("foo@example.com")
        msg = MailingListMessage.objects.create(subject="S", body="B")
        self.assertEqual(
            self._recipient_count(msg), 0,
            "A bulk-created suppressed address in mixed case must still "
            "exclude a lower-case subscriber")


class SurnameFirstHeadingTest(TestCase):
    """Bug (b): when a CSV's surname column comes before the given name,
    the heading parser must classify it as surname, not name.

    Every surname heading ('surname', 'last name', 'family name') contains
    the substring 'name', so the parser must check SURNAME_HEADINGS before
    NAME_HEADINGS.
    """

    def _parse(self, text):
        upload = io.BytesIO(text.encode("utf-8"))
        upload.name = "test.csv"
        return mailing.parse_addresses(upload)

    def test_surname_before_given_name_is_not_dropped(self):
        """CSV: Surname, Given Name, Email Address -> both name parts kept."""
        csv = "Surname,Given Name,Email Address\n"
        csv += "Smith,John,john@example.com\n"
        addresses = self._parse(csv)
        self.assertIn("john@example.com", addresses)
        self.assertEqual(
            addresses["john@example.com"], "John Smith",
            "Given name should not be dropped when surname column comes first")

    def test_last_name_before_first_name(self):
        """CSV: Last Name, First Name, Email -> both name parts kept."""
        csv = "Last Name,First Name,Email\n"
        csv += "Doe,Jane,jane@example.com\n"
        addresses = self._parse(csv)
        self.assertEqual(
            addresses["jane@example.com"], "Jane Doe")

    def test_family_name_before_given_name(self):
        """CSV: Family Name, Given Name, Email Address -> both name parts kept."""
        csv = "Family Name,Given Name,Email Address\n"
        csv += "Brown,Charlie,charlie@example.com\n"
        addresses = self._parse(csv)
        self.assertIn("charlie@example.com", addresses)
        self.assertEqual(
            addresses["charlie@example.com"], "Charlie Brown",
            "Given name should not be dropped when family name comes first")

    def test_given_name_before_surname_still_works(self):
        """Traditional First/Last order is unaffected."""
        csv = "Given Name,Surname,Email Address\n"
        csv += "Alice,Jones,alice@example.com\n"
        addresses = self._parse(csv)
        self.assertEqual(
            addresses["alice@example.com"], "Alice Jones")

    def test_only_surname_no_given_name(self):
        """CSV with only Surname (no Given Name column) just gets the surname."""
        csv = "Surname,Email Address\n"
        csv += "Wilson,wilson@example.com\n"
        addresses = self._parse(csv)
        self.assertIn("wilson@example.com", addresses)
        self.assertIn("Wilson", addresses["wilson@example.com"])
