"""The mailing list.

Subscribers, double opt-in and unsubscribing are django-newsletter's: one
Newsletter is one interest area. What is ours is the CSRF-exempt signup
endpoint that forms on other sites post to, and the send layer that mails one
message across several lists without anybody getting two copies. These tests
cover our half, plus the wiring into theirs -- and, because that endpoint is
deliberately CSRF exempt, several assertions about what that does *not* open
up: an open redirect, a way to be mailed without confirming, a way to knock
somebody off the list.
"""

import io
import json
import re
from unittest import mock
from urllib.parse import urlparse

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import resolve

from newsletter.models import Newsletter, Subscription

from main import mailing
from main.models import (
    MailingListDelivery, MailingListMessage, SuppressedAddress)
from main.views import MailingListSubscribeView


SUBSCRIBE_URL = "/mailing-list/subscribe"


class MailingListTestBase(TestCase):
    def setUp(self):
        # The signup rate limiter counts in the process-local cache, which
        # outlives a test case. Without this, tests poison each other in
        # whatever order they happen to run in.
        cache.clear()
        # The seeded lists (see migration 0014), not invented ones: these
        # tests should break if a slug an embedded form depends on changes.
        self.general = Newsletter.objects.get(slug="general")
        self.dc4k = Newsletter.objects.get(slug="dc4k")
        self.everything = Newsletter.objects.get(slug="all")

    def confirm(self, subscription):
        """Follow the activation link, the way a subscriber does."""
        response = self.client.post(
            subscription.subscribe_activate_url(),
            {"user_activation_code": subscription.activation_code})
        subscription.refresh_from_db()
        return response

    def subscriber(self, email, newsletter=None):
        """An address that has confirmed, so mailings may go to it."""
        subscription, _ = mailing.subscribe(email, newsletter or self.general)
        subscription.update("subscribe")
        return subscription

    def signup(self, email="kid@example.com", **extra):
        data = {"email": email}
        data.update(extra)
        return self.client.post(SUBSCRIBE_URL, data)

    def message(self, *interests, subject="Hello", body="Some news."):
        message = MailingListMessage.objects.create(subject=subject, body=body)
        for interest in interests:
            message.interests.add(interest)
        return message

    def login_as_staff(self):
        self.staff = User.objects.create_user(
            "staff", "staff@example.com", "hunter2hunter2",
            is_staff=True, is_superuser=True)
        self.client.force_login(self.staff)
        return self.staff

    def upload(self, text, name="export.csv"):
        upload = io.BytesIO(text.encode("utf-8"))
        upload.name = name
        return upload


class SignupTest(MailingListTestBase):
    def test_signup_records_an_unconfirmed_subscription_and_asks_to_confirm(self):
        response = self.client.post(SUBSCRIBE_URL, {"email": "A@Example.com"})

        self.assertEqual(response.status_code, 200)
        subscription = Subscription.objects.get()
        # Normalised, so a second signup as a@example.com is the same person.
        self.assertEqual(subscription.email, "a@example.com")
        self.assertFalse(subscription.subscribed)
        self.assertEqual(subscription.newsletter, self.general)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(subscription.activation_code, mail.outbox[0].body)

    def test_signup_can_name_a_list(self):
        self.client.post(SUBSCRIBE_URL,
                         {"email": "kid@example.com", "interest": "dc4k"})

        self.assertEqual(Subscription.objects.get().newsletter, self.dc4k)

    def test_an_unknown_list_falls_back_rather_than_losing_the_signup(self):
        # An embedded form on another site carries a hard-coded slug. If we
        # rename or retire the list, that form must not start throwing away
        # addresses.
        with self.assertLogs("main.mailing", level="INFO"):
            self.client.post(SUBSCRIBE_URL,
                             {"email": "kid@example.com", "interest": "gone"})

        self.assertEqual(Subscription.objects.get().newsletter, self.general)

    def test_a_hidden_list_is_not_accepted_for_new_signups(self):
        self.dc4k.visible = False
        self.dc4k.save()

        with self.assertLogs("main.mailing", level="INFO"):
            self.client.post(SUBSCRIBE_URL,
                             {"email": "kid@example.com", "interest": "dc4k"})

        self.assertEqual(Subscription.objects.get().newsletter, self.general)

    def test_the_endpoint_does_not_require_a_csrf_token(self):
        # The entire point: a plain <form> pasted onto another site has no
        # token to send. enforce_csrf_checks is what the real middleware does.
        client = Client(enforce_csrf_checks=True)

        response = client.post(SUBSCRIBE_URL, {"email": "far@example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Subscription.objects.filter(
                email_field="far@example.com").exists())

    def test_signing_up_twice_does_not_make_a_second_subscription(self):
        self.client.post(SUBSCRIBE_URL, {"email": "twice@example.com"})
        self.client.post(SUBSCRIBE_URL, {"email": "twice@example.com"})

        self.assertEqual(Subscription.objects.count(), 1)

    def test_the_same_address_can_be_on_two_lists(self):
        self.client.post(SUBSCRIBE_URL, {"email": "both@example.com"})
        self.client.post(SUBSCRIBE_URL,
                         {"email": "both@example.com", "interest": "dc4k"})

        self.assertEqual(Subscription.objects.count(), 2)

    def test_resubscribing_an_active_subscriber_cannot_reset_them(self):
        subscription = self.subscriber("member@example.com")
        mail.outbox.clear()

        self.client.post(SUBSCRIBE_URL, {"email": "member@example.com"})

        subscription.refresh_from_db()
        self.assertTrue(subscription.subscribed)
        self.assertFalse(subscription.unsubscribed)
        # No confirmation mail either: it would be unsolicited mail to
        # somebody an attacker only had to know the address of.
        self.assertEqual(mail.outbox, [])

    def test_a_bad_address_is_rejected(self):
        response = self.client.post(SUBSCRIBE_URL, {"email": "not-an-email"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Subscription.objects.exists())

    def test_the_honeypot_field_silently_drops_the_signup(self):
        with self.assertLogs("main.views", level="INFO"):
            response = self.client.post(
                SUBSCRIBE_URL,
                {"email": "bot@example.com", "website": "http://spam.example"})

        # Looks exactly like a success, so whatever filled it in learns
        # nothing, but nothing was recorded and nothing was mailed.
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Subscription.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_a_broken_mail_server_does_not_break_the_signup(self):
        # The signup often happens on somebody else's site. Our SMTP being
        # down must not show up there as an error.
        with mock.patch.object(Subscription, "send_activation_email",
                               side_effect=OSError("connection refused")):
            with self.assertLogs("main.mailing", level="ERROR"):
                response = self.client.post(
                    SUBSCRIBE_URL, {"email": "held@example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Subscription.objects.get().subscribed)

    def test_a_forged_x_forwarded_for_does_not_break_the_insert(self):
        response = self.client.post(
            SUBSCRIBE_URL, {"email": "spoof@example.com"},
            HTTP_X_FORWARDED_FOR="not-an-ip")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(Subscription.objects.get().ip)

    def test_json_is_returned_when_asked_for(self):
        response = self.client.post(
            SUBSCRIBE_URL, {"email": "js@example.com", "format": "json"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        # Readable by a script on another origin; there are no credentials
        # involved for a wildcard to expose.
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")

    def test_a_json_body_is_accepted(self):
        # What a fetch() against a JSON-answering endpoint looks like; it
        # leaves request.POST empty, so the fields have to come out of the
        # body instead.
        response = self.client.post(
            SUBSCRIBE_URL,
            data='{"email": "fetch@example.com", "interest": "dc4k"}',
            content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(Subscription.objects.get().newsletter, self.dc4k)

    def test_a_malformed_json_body_is_a_400_not_a_500(self):
        response = self.client.post(
            SUBSCRIBE_URL, data="{not json", content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_preflight_is_answered(self):
        response = self.client.options(SUBSCRIBE_URL)

        self.assertEqual(response.status_code, 204)
        self.assertIn("POST", response["Access-Control-Allow-Methods"])

    @override_settings(MAILING_LIST_SIGNUP_RATE_LIMIT=2)
    def test_a_flood_of_signups_stops_getting_confirmation_emails(self):
        # Anybody can post here, so without a ceiling this endpoint is a way
        # to have us mail an address somebody else picked, over and over.
        for i in range(2):
            self.client.post(SUBSCRIBE_URL, {"email": f"flood{i}@example.com"})
        with self.assertLogs("main.views", level="WARNING"):
            for i in range(2, 4):
                self.client.post(SUBSCRIBE_URL,
                                 {"email": f"flood{i}@example.com"})

        self.assertEqual(len(mail.outbox), 2)
        # Nothing is written either: the check runs before the row is created,
        # so a flood cannot fill the table on the way to being refused.
        self.assertEqual(Subscription.objects.count(), 2)


class EverythingCheckboxTest(MailingListTestBase):
    """The "send me all updates" tick on the signup form.

    Signing up for a topic never puts somebody on the All list by itself --
    the subscribe page promises they will only hear about what they picked --
    so this is an opt-in they can see and untick.
    """

    def rows(self):
        return Subscription.objects.filter(email_field="kid@example.com")

    def test_ticking_it_puts_them_on_all_instead_of_the_topic(self):
        # One row, because All already receives every mailing addressed to any
        # public list -- so a second row on the topic would add nothing but a
        # second thing to confirm.
        self.signup(interest="dc4k", all_updates="1")

        self.assertEqual([row.newsletter.slug for row in self.rows()], ["all"])

    def test_it_costs_one_confirmation_email_naming_the_list_it_confirms(self):
        self.signup(interest="dc4k", all_updates="1")

        self.assertEqual(len(mail.outbox), 1)
        # The email has to be about the list it actually subscribes them to:
        # a click can only honestly confirm what the mail it came from says.
        self.assertIn("All", mail.outbox[0].subject)

    def test_nothing_is_live_until_the_link_is_clicked(self):
        self.signup(interest="dc4k", all_updates="1")

        self.assertFalse(any(row.subscribed for row in self.rows()))

    def test_confirming_it_subscribes_them_to_all(self):
        self.signup(interest="dc4k", all_updates="1")
        row = self.rows().get()

        self.confirm(row)

        self.assertTrue(row.subscribed)
        self.assertEqual(row.newsletter, self.everything)

    def test_leaving_it_unticked_adds_only_the_topic(self):
        self.signup(interest="dc4k")

        self.assertEqual(
            [row.newsletter.slug for row in self.rows()], ["dc4k"])

    def test_picking_all_and_ticking_it_is_still_one_subscription(self):
        self.signup(interest="all", all_updates="1")

        self.assertEqual([row.newsletter.slug for row in self.rows()], ["all"])

    def test_nobody_can_put_a_stranger_on_all_using_their_pending_signup(self):
        # The hole the previous design had: the second row was created carrying
        # the first row's activation code, copied server-side, so posting
        # somebody's address with the box ticked meant their own confirmation
        # click also put them on All. Nothing may link two rows like that.
        self.signup("victim@example.com", interest="dc4k")
        victim = Subscription.objects.get(email_field="victim@example.com")

        self.signup("victim@example.com", interest="dc4k", all_updates="1")
        self.confirm(victim)

        on_all = Subscription.objects.filter(
            email_field="victim@example.com", newsletter=self.everything)
        self.assertFalse(any(row.subscribed for row in on_all))

    def test_somebody_who_left_all_has_to_confirm_again_to_come_back(self):
        # Ticking a box on a form anybody could have submitted does not undo an
        # unsubscribe. It asks again -- with a code that has not been in an
        # email before, so the dead link stays dead -- and nothing changes
        # until that new link is clicked. Repeat asking is what the per-address
        # rate limit bounds.
        left = self.subscriber("kid@example.com", self.everything)
        left.update("unsubscribe")
        dead_code = left.activation_code
        mail.outbox.clear()

        self.signup(interest="dc4k", all_updates="1")

        left.refresh_from_db()
        self.assertFalse(left.subscribed)
        self.assertTrue(left.unsubscribed)
        self.assertNotEqual(left.activation_code, dead_code)
        self.assertEqual(len(mail.outbox), 1)

        self.client.post(left.subscribe_activate_url(),
                         {"user_activation_code": dead_code})
        left.refresh_from_db()
        self.assertFalse(left.subscribed)

    def test_a_topic_subscriber_ticking_it_is_asked_to_confirm_all(self):
        # Their topic subscription stands; the new one still needs a click.
        self.subscriber("kid@example.com", self.dc4k)
        mail.outbox.clear()

        self.signup(interest="dc4k", all_updates="1")

        on_all = Subscription.objects.get(newsletter=self.everything)
        self.assertFalse(on_all.subscribed)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(on_all.activation_code, mail.outbox[0].body)

    def test_somebody_already_on_all_ticking_it_gets_no_mail(self):
        self.subscriber("kid@example.com", self.everything)
        mail.outbox.clear()

        self.signup(interest="dc4k", all_updates="1")

        self.assertEqual(mail.outbox, [])


class NoRedirectBackTest(MailingListTestBase):
    """The endpoint never redirects anywhere a submission asked it to.

    It is CSRF exempt and open to the internet, so honouring a `next` would
    make it an open redirect with our domain on it -- and doing that safely
    means a per-site allowlist, which is exactly the site-by-site mapping this
    feature does without. Embedded forms that need the visitor to stay put use
    the iframe instead.
    """

    def test_a_next_field_is_ignored_but_the_signup_still_happens(self):
        response = self.client.post(SUBSCRIBE_URL, {
            "email": "kid@example.com",
            "next": "https://evil.example/landing"})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "mailing_list_result.html")
        self.assertTrue(Subscription.objects.exists())


class ConfirmAndUnsubscribeTest(MailingListTestBase):
    """django-newsletter owns these pages; this is the wiring into them."""

    def setUp(self):
        super().setUp()
        self.client.post(SUBSCRIBE_URL,
                         {"email": "person@example.com", "interest": "dc4k"})
        self.subscription = Subscription.objects.get()

    def test_the_link_in_the_email_subscribes_them(self):
        self.confirm(self.subscription)

        self.assertTrue(self.subscription.subscribed)
        self.assertFalse(self.subscription.unsubscribed)

    def test_a_wrong_activation_code_does_not_subscribe_anybody(self):
        self.client.post(self.subscription.subscribe_activate_url(),
                         {"user_activation_code": "nonsense"})

        self.subscription.refresh_from_db()
        self.assertFalse(self.subscription.subscribed)

    def test_unsubscribing_works(self):
        self.confirm(self.subscription)

        self.client.post(
            self.subscription.unsubscribe_activate_url(),
            {"user_activation_code": self.subscription.activation_code})

        self.subscription.refresh_from_db()
        self.assertTrue(self.subscription.unsubscribed)

    def test_an_old_confirmation_link_cannot_undo_an_unsubscribe(self):
        # django-newsletter reuses one activation code for both actions and
        # does not change it on unsubscribe, so the original "confirm your
        # subscription" email keeps a working link. A forwarded copy of it --
        # or a mail scanner reaching it late -- must not put somebody back on
        # a list they left. main.mailing rotates the code to close that off.
        self.confirm(self.subscription)
        old_url = self.subscription.subscribe_activate_url()
        old_code = self.subscription.activation_code
        self.subscription.update("unsubscribe")

        self.client.post(old_url, {"user_activation_code": old_code})

        self.subscription.refresh_from_db()
        self.assertTrue(self.subscription.unsubscribed)
        self.assertNotEqual(self.subscription.activation_code, old_code)

    def test_signing_up_again_after_unsubscribing_needs_a_new_confirmation(self):
        self.confirm(self.subscription)
        self.subscription.update("unsubscribe")
        old_code = self.subscription.activation_code
        mail.outbox.clear()

        self.client.post(SUBSCRIBE_URL,
                         {"email": "person@example.com", "interest": "dc4k"})

        self.subscription.refresh_from_db()
        self.assertFalse(self.subscription.subscribed)
        self.assertNotEqual(self.subscription.activation_code, old_code)
        self.assertEqual(len(mail.outbox), 1)


class HeadingDetectionTest(TestCase):
    """Which column is which, for files other people's tools produced."""

    def parse(self, text):
        upload = io.BytesIO(text.encode("utf-8"))
        upload.name = "export.csv"
        return mailing.parse_addresses(upload)

    def test_a_surname_column_before_the_given_name_keeps_both(self):
        # Every surname heading contains the substring "name", so the surname
        # used to claim the name slot and the given name was dropped -- the
        # person ended up recorded as their surname alone. Mailchimp's
        # First/Last ordering hides this; a hand-made export need not use it.
        self.assertEqual(
            self.parse("Email,Surname,Given Name\n"
                       "ada@example.com,Lovelace,Ada\n"),
            {"ada@example.com": "Ada Lovelace"})

    def test_mailchimps_own_ordering_still_works(self):
        self.assertEqual(
            self.parse("Email Address,First Name,Last Name\n"
                       "ada@example.com,Ada,Lovelace\n"),
            {"ada@example.com": "Ada Lovelace"})

    def test_a_lone_name_column_is_the_name(self):
        self.assertEqual(
            self.parse("Email,Name\nada@example.com,Ada\n"),
            {"ada@example.com": "Ada"})


class SuppressionAtSignupTest(MailingListTestBase):
    """The never-email list has to hold on the open endpoint too.

    It is not only about who is on a list: a suppressed address is one that
    bounced, complained, or asked us to stop, and *mailing it at all* is the
    thing that gets a domain blocked. The endpoint is open to the internet, so
    without this anyone could have us mail such an address on demand.
    """

    def setUp(self):
        super().setUp()
        SuppressedAddress.objects.create(
            email="stop@example.com", reason="asked us to stop")

    def test_a_suppressed_address_is_not_even_sent_a_confirmation(self):
        with self.assertLogs("main.mailing", level="INFO"):
            self.signup("stop@example.com")

        self.assertEqual(mail.outbox, [])
        self.assertFalse(Subscription.objects.exists())

    def test_the_answer_is_the_same_as_for_any_other_address(self):
        # Otherwise the endpoint says who is suppressed.
        with self.assertLogs("main.mailing", level="INFO"):
            refused = self.signup("stop@example.com")
        accepted = self.signup("anyone@example.com")

        self.assertEqual(refused.content, accepted.content)

    def test_matching_ignores_case(self):
        with self.assertLogs("main.mailing", level="INFO"):
            self.signup("STOP@Example.com")

        self.assertFalse(Subscription.objects.exists())

    def test_a_suppressed_row_written_outside_save_is_still_excluded(self):
        # bulk_create, loaddata and raw SQL skip the save() that lower-cases,
        # so the stored value can be mixed case. The audience query has to fold
        # case on both sides or it mails somebody it was told not to.
        Subscription(newsletter=self.general, email_field="bulk@example.com",
                     subscribed=True).save()
        SuppressedAddress.objects.bulk_create(
            [SuppressedAddress(email="Bulk@Example.COM")])

        self.assertEqual(list(self.message(self.general).recipients()), [])

    def test_a_suppressed_address_is_excluded_from_a_mailing(self):
        # Belt and braces: suppressing takes people off their lists, but a row
        # that slipped past that must still never be mailed.
        live = self.subscriber("later@example.com", self.general)
        SuppressedAddress.objects.create(email="later@example.com")

        self.assertNotIn("later@example.com",
                         [r.email for r in self.message().recipients()])
        self.assertTrue(live.subscribed)  # not unsubscribed, just not mailed


class OneAddressOneCopyTest(MailingListTestBase):
    """Case is not identity for a mailbox.

    django-newsletter's own signup page is mounted at /newsletter/ and does not
    normalise, and neither does its admin, so Bob@Example.COM and
    bob@example.com can both exist. If the send layer treats them as two
    people, one person gets two copies of every mailing.
    """

    def rows_for(self, *addresses):
        for address in addresses:
            Subscription(newsletter=self.general, email_field=address,
                         subscribed=True).save()

    def test_two_case_variants_get_one_copy(self):
        self.rows_for("Bob@Example.COM", "bob@example.com")
        message = self.message(self.general)

        sent, failed = message.send_batch()

        self.assertEqual((sent, failed), (1, 0))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(message.pending_count(), 0)

    def test_a_case_variant_does_not_become_a_second_subscription(self):
        self.subscriber("bob@example.com", self.general)
        mail.outbox.clear()

        self.signup("BOB@example.com")

        self.assertEqual(Subscription.objects.filter(
            newsletter=self.general).count(), 1)
        self.assertEqual(mail.outbox, [])

    def test_suppressing_reaches_a_case_variant_and_a_user_row(self):
        user = User.objects.create_user(
            "somebody", "Somebody@Example.com", "hunter2hunter2")
        Subscription(newsletter=self.general,
                     user=user, subscribed=True).save()
        self.rows_for("Somebody@Example.com")

        mailing.suppress_addresses({"somebody@example.com": ""})

        self.assertEqual(
            [s.unsubscribed for s in Subscription.objects.all()], [True, True])
        self.assertEqual(list(self.message().recipients()), [])


class SendLayerEdgeCaseTest(MailingListTestBase):
    """The ways a send used to get stuck, mail twice, or lie about itself."""

    def setUp(self):
        super().setUp()
        self.first = self.subscriber("first@example.com", self.general)
        self.second = self.subscriber("second@example.com", self.general)
        mail.outbox.clear()

    def test_an_empty_audience_does_not_mark_a_mailing_sent(self):
        # It used to, which made the mailing permanently unsendable: a sent
        # message is never reopened and status is read-only in the admin.
        message = self.message(Newsletter.objects.get(slug="liberatedbread"))

        self.assertEqual(message.send_batch(), (0, 0))
        self.assertEqual(message.status, MailingListMessage.Status.DRAFT)

        self.subscriber("someone@example.com",
                        Newsletter.objects.get(slug="liberatedbread"))
        self.assertEqual(message.send_batch()[0], 1)

    def test_deleting_a_subscriber_keeps_the_record_of_what_was_sent(self):
        message = self.message(self.general)
        message.send_batch(limit=1)
        delivered_to = mail.outbox[0].to[0]

        Subscription.objects.filter(email_field=delivered_to).delete()

        self.assertEqual(message.deliveries.count(), 1)
        self.assertEqual(message.deliveries.get().email, delivered_to)
        self.assertNotIn(delivered_to,
                         [r.email for r in message.pending_recipients()])

    def test_a_mixed_case_delivery_row_still_dedups_by_address(self):
        # The address dedup folds case on both sides, so a delivery row whose
        # email was not normalised -- a future bulk_create or data migration --
        # does not slip past and mail its recipient a second time. The delivery
        # has no subscription (the SET_NULL case, e.g. the original subscriber
        # was deleted and re-signed up), so only the address comparison stands
        # between them and a duplicate.
        self.subscriber("mixed@example.com", self.general)
        message = self.message(self.general)
        MailingListDelivery.objects.bulk_create([
            MailingListDelivery(message=message, subscription=None,
                                email="MIXED@Example.COM")])

        self.assertNotIn(
            "mixed@example.com",
            [r.email for r in message.pending_recipients()])

    def test_an_address_changing_mid_send_does_not_pin_it_open(self):
        # The claimed row used to come back as pending under its new address,
        # be refused by the per-subscription constraint, and leave the send
        # showing "1 still to go" forever.
        message = self.message(self.general)
        message.send_batch(limit=1)
        claimed = message.deliveries.get().subscription
        claimed.email_field = "renamed@example.com"
        claimed.save()

        message.send_batch()

        self.assertEqual(message.pending_count(), 0)
        self.assertEqual(message.status, MailingListMessage.Status.SENT)

    def test_unsubscribing_during_a_batch_stops_your_copy(self):
        # A batch is materialised up front and then mailed one at a time over
        # one connection, so somebody who clicks unsubscribe while it is
        # running used to get their copy anyway. The claim re-checks.
        message = self.message(self.general)
        real_send = mail.EmailMessage.send
        second = self.second

        def unsubscribe_then_send(self_, *args, **kwargs):
            second.update("unsubscribe")
            return real_send(self_, *args, **kwargs)

        with mock.patch.object(mail.EmailMessage, "send",
                               unsubscribe_then_send):
            with self.assertLogs("main.models", level="INFO"):
                sent, failed = message.send_batch()

        self.assertEqual((sent, failed), (1, 0))
        self.assertEqual([m.to[0] for m in mail.outbox], ["first@example.com"])

    def test_widening_a_sent_mailing_does_not_reopen_it(self):
        message = self.message(self.general)
        message.send_batch()
        self.subscriber("kid@example.com", self.dc4k)

        message.interests.add(self.dc4k)

        self.assertEqual(message.pending_count(), 0)

    def test_a_test_send_refuses_an_address_that_never_confirmed(self):
        # The open signup endpoint can create an unconfirmed row for any
        # address, so without this staff could be walked into mailing one.
        mailing.subscribe("never@example.com", self.general)

        with self.assertRaises(ValueError):
            self.message(self.general).send_test("never@example.com")

    def test_a_test_send_prefers_a_subscription_to_this_mailings_own_list(self):
        left = self.subscriber("both@example.com", self.dc4k)
        left.update("unsubscribe")
        self.subscriber("both@example.com", self.general)

        self.message(self.general).send_test("both@example.com")

        self.assertIn(str(self.general), mail.outbox[0].body)


class SignupLimitTest(MailingListTestBase):
    """The ceiling that stops the open endpoint mailbombing one address."""

    @override_settings(MAILING_LIST_SIGNUP_RATE_LIMIT=100)
    def test_one_address_cannot_be_buried_from_many_sources(self):
        # The per-source count is not enough on its own: X-Forwarded-For is
        # client-supplied and nginx appends to it, so the source is whatever
        # the caller says it is.
        for i in range(8):
            with self.settings():
                self.client.post(
                    SUBSCRIBE_URL, {"email": "victim@example.com"},
                    HTTP_X_FORWARDED_FOR=f"203.0.113.{i}")

        self.assertLessEqual(
            len(mail.outbox),
            MailingListSubscribeView.PER_ADDRESS_LIMIT)

    @override_settings(MAILING_LIST_SIGNUP_RATE_LIMIT=2)
    def test_junk_in_the_forwarded_header_is_not_a_way_out(self):
        # An unparseable header used to skip the check entirely.
        for i in range(6):
            self.client.post(SUBSCRIBE_URL,
                             {"email": f"flood{i}@example.com"},
                             HTTP_X_FORWARDED_FOR="not-an-ip")

        self.assertEqual(len(mail.outbox), 2)


class HoneypotTest(MailingListTestBase):
    def test_a_falsy_value_is_not_treated_as_a_bot(self):
        # A JSON caller serialising every field sends 0 or false for an
        # unticked checkbox; CharField stringifies both into something truthy,
        # which used to drop every such signup while reporting success.
        # A different address each time: the per-address ceiling is low, and
        # reusing one would refuse the last of these for the wrong reason.
        for index, falsy in enumerate([0, False, "", None]):
            with self.subTest(website=falsy):
                email = f"js{index}@example.com"
                response = self.client.post(
                    SUBSCRIBE_URL,
                    data=json.dumps({"email": email, "website": falsy}),
                    content_type="application/json")

                self.assertTrue(response.json()["ok"])
                self.assertTrue(
                    Subscription.objects.filter(email_field=email).exists())

    def test_the_answer_does_not_reveal_the_trap(self):
        self.subscriber("known@example.com", self.general)

        plain = self.signup("known@example.com")
        with self.assertLogs("main.views", level="INFO"):
            trapped = self.signup("known@example.com",
                                  website="http://spam.example")

        self.assertEqual(plain.content, trapped.content)


class NameInConfirmationEmailTest(MailingListTestBase):
    def test_a_name_cannot_smuggle_prose_into_our_confirmation_email(self):
        # The name is rendered into django-newsletter's activation email
        # ("Dear {{ name }},"), and anyone can post any address here.
        self.signup("target@example.com",
                    name="friend,\n\nYOUR ORDER IS HELD. Pay at evil.example")

        body = mail.outbox[0].body
        self.assertNotIn("YOUR ORDER IS HELD.\n", body)
        self.assertIn("friend, YOUR ORDER IS HELD. Pay at evil.example", body)


class SeededListsTest(MailingListTestBase):
    """The initial lists. Their slugs end up in markup on other sites, so they
    are part of the interface, not just data."""

    EXPECTED = ["all", "general", "books", "dc4k", "high-performance-spark",
                "liberatedbread", "fight-health-insurance"]

    def test_every_list_is_seeded(self):
        self.assertEqual(
            sorted(Newsletter.objects.values_list("slug", flat=True)),
            sorted(self.EXPECTED))

    def test_every_list_is_attached_to_the_site(self):
        # django-newsletter's own confirm and unsubscribe pages filter on
        # site, so a list with none attached is one nobody can confirm.
        for newsletter in Newsletter.objects.all():
            with self.subTest(slug=newsletter.slug):
                self.assertTrue(newsletter.site.exists())

    def test_the_default_list_survives_being_deleted(self):
        Newsletter.objects.filter(slug="general").delete()

        self.assertEqual(mailing.default_newsletter().slug, "general")


class SubscribePageTest(MailingListTestBase):
    def action_from(self, response):
        markup = response.content.decode("utf-8")
        match = re.search(r'<form[^>]+action="([^"]+)"', markup)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_the_form_action_posts_to_a_live_signup_route(self):
        response = self.client.get("/subscribe")
        action = self.action_from(response)
        path = urlparse(action).path or action

        self.assertEqual(resolve(path).view_name, "mailing-list-subscribe-all")
        posted = self.client.post(path, {"email": "all@example.com",
                                         "interest": "all"})
        self.assertNotEqual(posted.status_code, 404)

    def test_the_subscribe_page_signs_people_up_for_all(self):
        response = self.client.get("/subscribe")
        action = self.action_from(response)
        path = urlparse(action).path or action

        self.assertContains(response, 'type="hidden" name="interest" value="all"')
        self.assertNotContains(response, 'name="all_updates"')

        self.client.post(path, {"email": "all@example.com", "interest": "all"})

        self.assertEqual(Subscription.objects.get().newsletter, self.everything)

    def test_the_subscribe_page_receiver_forces_all_despite_tampering(self):
        response = self.client.get("/subscribe")
        path = urlparse(self.action_from(response)).path

        tampered_posts = [
            {"email": "stripped@example.com"},
            {"email": "blank@example.com", "interest": ""},
            {"email": "changed@example.com", "interest": "dc4k"},
        ]
        for payload in tampered_posts:
            with self.subTest(payload=payload):
                Subscription.objects.all().delete()

                self.client.post(path, payload)

                self.assertEqual(
                    Subscription.objects.get().newsletter, self.everything)

    def test_a_hidden_list_is_not_offered(self):
        self.dc4k.visible = False
        self.dc4k.save()

        response = self.client.get("/subscribe")

        self.assertNotContains(response, 'value="dc4k"')

    def test_the_other_pages_keep_the_general_signup_form(self):
        for i, path in enumerate(["/", "/services", "/about", "/family"]):
            with self.subTest(path=path):
                response = self.client.get(path)
                form_path = urlparse(self.action_from(response)).path

                self.assertEqual(
                    resolve(form_path).view_name, "mailing-list-subscribe")
                self.assertNotContains(
                    response, 'type="hidden" name="interest" value="all"')
                self.assertContains(response, 'name="all_updates"')

                Subscription.objects.all().delete()
                self.client.post(form_path, {"email": f"general{i}@example.com"})

                self.assertEqual(
                    Subscription.objects.get().newsletter, self.general)


class StaticSnippetTest(TestCase):
    def test_the_snippet_ships_and_opts_into_everything_visibly(self):
        # The form pasted onto other sites. The checkbox has to be in it and
        # ticked: "everything" is an opt-in somebody can see and untick, not
        # something we add on their behalf.
        from django.contrib.staticfiles import finders

        path = finders.find("mailing-list/signup-form.html")
        self.assertIsNotNone(path)
        with open(path) as snippet:
            markup = snippet.read()
        self.assertIn('name="all_updates" value="1" checked', markup)
        self.assertIn("/mailing-list/subscribe", markup)


class SendingTest(MailingListTestBase):
    def setUp(self):
        super().setUp()
        self.confirmed = self.subscriber("in@example.com", self.general)
        self.unconfirmed, _ = mailing.subscribe(
            "maybe@example.com", self.general)
        self.kid = self.subscriber("kid@example.com", self.dc4k)
        mail.outbox.clear()

    def message(self, *interests):
        message = MailingListMessage.objects.create(
            subject="Hello", body="Some news.")
        for interest in interests:
            message.interests.add(interest)
        return message

    def test_a_message_with_no_lists_goes_to_everyone_confirmed(self):
        sent, failed = self.message().send_batch()

        self.assertEqual((sent, failed), (2, 0))
        self.assertEqual(sorted(m.to[0] for m in mail.outbox),
                         ["in@example.com", "kid@example.com"])

    def test_a_message_can_be_limited_to_one_list(self):
        self.message(self.dc4k).send_batch()

        self.assertEqual([m.to[0] for m in mail.outbox], ["kid@example.com"])

    def test_an_unsubscribed_address_is_never_mailed(self):
        self.confirmed.update("unsubscribe")

        self.message().send_batch()

        self.assertNotIn("in@example.com", [m.to[0] for m in mail.outbox])

    def test_sending_again_does_not_send_a_second_copy(self):
        message = self.message()
        message.send_batch()
        mail.outbox.clear()

        sent, failed = message.send_batch()

        self.assertEqual((sent, failed), (0, 0))
        self.assertEqual(mail.outbox, [])

    def test_a_batch_leaves_the_rest_pending_and_a_second_batch_finishes(self):
        message = self.message()

        message.send_batch(limit=1)

        self.assertEqual(message.pending_count(), 1)
        self.assertEqual(message.status, MailingListMessage.Status.SENDING)

        message.send_batch(limit=1)

        message.refresh_from_db()
        self.assertEqual(message.pending_count(), 0)
        self.assertEqual(message.status, MailingListMessage.Status.SENT)
        self.assertIsNotNone(message.sent_at)

    def test_a_mailing_to_one_list_also_reaches_everyone_on_all(self):
        # They asked for everything, so a mailing about a topic they never
        # picked is exactly what they signed up for.
        everyone = self.subscriber("everything@example.com", self.everything)

        self.message(self.dc4k).send_batch()

        self.assertEqual(sorted(m.to[0] for m in mail.outbox),
                         [everyone.email, "kid@example.com"])

    def test_a_list_added_later_still_reaches_everyone_on_all(self):
        # The question this behaviour exists to answer: an interest created
        # long after somebody subscribed to All must not silently miss them.
        self.subscriber("everything@example.com", self.everything)
        later = Newsletter.objects.create(
            slug="pocket-lab", title="Pocket Lab",
            email="support@pigscanfly.ca", sender="PCF")
        self.subscriber("gadget@example.com", later)

        self.message(later).send_batch()

        self.assertEqual(sorted(m.to[0] for m in mail.outbox),
                         ["everything@example.com", "gadget@example.com"])

    def test_being_on_all_and_the_named_list_is_still_one_copy(self):
        self.subscriber("everything@example.com", self.everything)
        self.subscriber("everything@example.com", self.dc4k)
        message = self.message(self.dc4k)

        message.send_batch()

        self.assertEqual(
            [m.to[0] for m in mail.outbox].count("everything@example.com"), 1)
        self.assertEqual(message.pending_count(), 0)

    def test_an_unrelated_list_is_still_not_dragged_in(self):
        # All is the only list that gets everything; general does not.
        self.message(self.dc4k).send_batch()

        self.assertEqual([m.to[0] for m in mail.outbox], ["kid@example.com"])

    def test_somebody_on_two_selected_lists_only_gets_one_copy(self):
        # The whole reason this send layer exists rather than submitting the
        # message to each newsletter in django-newsletter's own admin.
        self.subscriber("in@example.com", self.dc4k)
        message = self.message(self.general, self.dc4k)

        message.send_batch()

        self.assertEqual(
            [m.to[0] for m in mail.outbox].count("in@example.com"), 1)
        # ...and the duplicate drains, so the send can finish.
        self.assertEqual(message.pending_count(), 0)

    def test_two_subscriptions_for_one_address_cannot_both_be_claimed(self):
        # Two concurrent senders can hold different subscription rows for the
        # same person, so the claim has to be exclusive on the address.
        other = self.subscriber("in@example.com", self.dc4k)
        message = self.message()

        first = message._claim(self.confirmed)
        with self.assertLogs("main.models", level="INFO"):
            second = message._claim(other)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_somebody_who_subscribes_mid_send_is_not_added_to_it(self):
        message = self.message()
        message.send_batch(limit=1)

        self.subscriber("late@example.com", self.general)

        self.assertNotIn(
            "late@example.com",
            [s.email for s in message.pending_recipients()])

    def test_a_finished_mailing_does_not_reopen_when_somebody_subscribes(self):
        message = self.message()
        message.send_batch()
        self.assertEqual(message.status, MailingListMessage.Status.SENT)
        mail.outbox.clear()

        self.subscriber("late@example.com", self.general)

        self.assertEqual(message.pending_count(), 0)
        self.assertEqual(message.send_batch(), (0, 0))
        self.assertEqual(mail.outbox, [])

    def test_one_undeliverable_address_does_not_stop_the_mailing(self):
        message = self.message()
        real_send = mail.EmailMessage.send

        def explode(self_, *args, **kwargs):
            if self_.to == ["in@example.com"]:
                raise OSError("550 no such user")
            return real_send(self_, *args, **kwargs)

        with mock.patch.object(mail.EmailMessage, "send", explode):
            with self.assertLogs("main.models", level="ERROR"):
                sent, failed = message.send_batch()

        self.assertEqual((sent, failed), (1, 1))
        self.assertEqual(
            message.deliveries.filter(
                status=MailingListDelivery.Status.FAILED).count(), 1)

    def test_a_failed_address_is_not_retried_by_the_next_batch(self):
        message = self.message()
        with mock.patch.object(mail.EmailMessage, "send",
                               side_effect=OSError("nope")):
            with self.assertLogs("main.models", level="ERROR"):
                message.send_batch()
        mail.outbox.clear()

        self.assertEqual(message.send_batch(), (0, 0))

    def test_every_message_carries_an_unsubscribe_link_and_header(self):
        self.message().send_batch()

        sent = next(m for m in mail.outbox if "in@example.com" in m.to[0])
        self.assertIn(self.confirmed.activation_code, sent.body)
        self.assertIn(self.confirmed.activation_code,
                      sent.extra_headers["List-Unsubscribe"])

    def test_the_body_can_address_the_recipient(self):
        self.confirmed.name_field = "Ada"
        self.confirmed.save()
        message = MailingListMessage.objects.create(
            subject="Hi", body="Hello {{ name }}, news follows.")

        message.send_batch()

        body = next(m for m in mail.outbox
                    if "in@example.com" in m.to[0]).body
        self.assertIn("Hello Ada", body)

    def test_a_body_that_cannot_render_is_rejected_before_it_is_saved(self):
        # Otherwise it is discovered at send time, as every delivery failing.
        message = MailingListMessage(subject="Broken", body="Hello {% oops %}")

        with self.assertRaises(ValidationError):
            message.full_clean()

    def test_a_test_send_is_not_recorded_as_a_delivery(self):
        message = self.message()

        message.send_test("in@example.com")

        self.assertEqual(mail.outbox[0].to, ["in@example.com"])
        self.assertTrue(mail.outbox[0].subject.startswith("[test]"))
        self.assertEqual(message.deliveries.count(), 0)
        self.assertEqual(message.pending_count(), 2)

    def test_a_test_to_an_unknown_address_says_so_rather_than_guessing(self):
        # There would be no unsubscribe link to render, so the test would not
        # be a faithful preview of the real thing.
        with self.assertRaises(ValueError):
            self.message().send_test("stranger@example.com")


class TestGroupFixtureTest(MailingListTestBase):
    """The list you can send a real mailing to without reaching anybody else."""

    fixtures = ["mailing_list_test_group"]

    def test_the_owner_is_its_only_subscriber(self):
        test_list = Newsletter.objects.get(slug="test")

        self.assertEqual(
            [s.email for s in Subscription.objects.filter(
                newsletter=test_list, subscribed=True)],
            ["holden@pigscanfly.ca"])

    def test_it_is_not_offered_on_the_public_signup_form(self):
        response = self.client.get("/subscribe")

        self.assertNotContains(response, 'value="test"')

    def test_posting_its_slug_at_the_open_endpoint_does_not_join_it(self):
        # Nobody outside should be able to end up on it, however they ask.
        with self.assertLogs("main.mailing", level="INFO"):
            self.signup("outsider@example.com", interest="test")

        self.assertEqual(
            Subscription.objects.get(
                email_field="outsider@example.com").newsletter.slug, "general")

    def test_a_real_mailing_to_it_reaches_only_the_owner(self):
        # With somebody on the All list, which is the case in production --
        # every signup form ticks it by default. A mailing to a *public* list
        # reaches them; this one must not, or "send it for real, it cannot
        # reach anybody else" is false and the fixture is a trap.
        self.subscriber("realperson@example.com", self.everything)
        message = self.message(Newsletter.objects.get(slug="test"),
                               subject="Deliverability check")
        mail.outbox.clear()

        sent, failed = message.send_batch()

        self.assertEqual((sent, failed), (1, 0))
        # get_recipient() formats the display name into the header.
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("holden@pigscanfly.ca", mail.outbox[0].to[0])

    def test_a_mailing_to_a_public_list_does_reach_the_all_subscribers(self):
        # The other half of the rule above, so the exemption cannot silently
        # widen to every list.
        self.subscriber("realperson@example.com", self.everything)
        message = self.message(self.dc4k)
        mail.outbox.clear()

        message.send_batch()

        self.assertEqual([m.to for m in mail.outbox],
                         [["realperson@example.com"]])

    def test_a_subscriber_with_no_subscribe_date_survives_a_resumed_send(self):
        # loaddata saves raw, so a fixture -- or anything else that bypasses
        # Subscription.save() -- can leave subscribe_date null. Such a
        # subscriber must not silently drop out once the first batch has
        # frozen the audience.
        Subscription.objects.filter(
            email_field="holden@pigscanfly.ca").update(subscribe_date=None)
        self.subscriber("other@example.com", self.general)
        message = MailingListMessage.objects.create(
            subject="Two batches", body="News.")

        message.send_batch(limit=1)
        message.send_batch(limit=1)

        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(any("holden@pigscanfly.ca" in m.to[0]
                            for m in mail.outbox))
        self.assertEqual(message.pending_count(), 0)


class ImportTestBase(MailingListTestBase):
    """Shared by the two halves of the import page."""

    IMPORT_URL = "/timbit/admin/mailing-list/import"

    MAILCHIMP = ("Email Address,First Name,Last Name,MEMBER_RATING\n"
                 "ada@example.com,Ada,Lovelace,4\n"
                 "grace@example.com,Grace,Hopper,3\n")

    def setUp(self):
        super().setUp()
        self.login_as_staff()


class ImportTest(ImportTestBase):
    """Uploading a list of addresses somebody else's tool produced."""

    GOOGLE_FORMS = ("Timestamp,Email Address,Name,Anything else?\n"
                    "2026/07/26 9:00,kid@example.com,New Kid,hi\n")

    def do_import(self, text=None, **extra):
        data = {"mode": "subscribe", "newsletter": self.dc4k.pk,
                "address_file": self.upload(text or self.MAILCHIMP)}
        data.update(extra)
        return self.client.post(self.IMPORT_URL, data, follow=True)

    def subscribers(self, newsletter=None):
        return sorted(s.email for s in Subscription.objects.filter(
            newsletter=newsletter or self.dc4k))

    def test_a_mailchimp_export_imports_with_names(self):
        self.do_import()

        self.assertEqual(self.subscribers(),
                         ["ada@example.com", "grace@example.com"])
        self.assertEqual(
            Subscription.objects.get(email_field="ada@example.com").name,
            "Ada Lovelace")

    def test_a_google_forms_export_imports(self):
        # A timestamp column and no surname, which is what that export is.
        self.do_import(self.GOOGLE_FORMS)

        self.assertEqual(self.subscribers(), ["kid@example.com"])

    def test_a_bare_column_of_addresses_imports(self):
        self.do_import("solo@example.com\nother@example.com\n")

        self.assertEqual(self.subscribers(),
                         ["other@example.com", "solo@example.com"])

    def test_imported_addresses_are_subscribed_without_double_opt_in(self):
        # An import is the owner asserting they already have consent; mailing
        # everybody a confirmation request would be the surprise.
        self.do_import(notify="")

        self.assertTrue(all(
            s.subscribed for s in Subscription.objects.filter(
                newsletter=self.dc4k)))
        self.assertEqual(mail.outbox, [])

    def test_the_notice_tells_them_where_they_are_and_how_to_leave(self):
        self.do_import(notify="on")

        self.assertEqual(len(mail.outbox), 2)
        subscription = Subscription.objects.get(email_field="ada@example.com")
        notice = next(m for m in mail.outbox
                      if "ada@example.com" in m.to[0])
        self.assertIn("updated our mailing list", notice.subject.lower())
        self.assertIn(subscription.activation_code, notice.body)
        self.assertIn(subscription.activation_code,
                      notice.extra_headers["List-Unsubscribe"])
        # Addressed and sent using the list's own Sender/E-mail fields, which
        # is what those admin fields are for.
        self.assertEqual(notice.to, [subscription.get_recipient()])
        self.assertEqual(notice.from_email,
                         subscription.newsletter.get_sender())

    def test_the_notice_link_actually_unsubscribes(self):
        # A notice whose escape hatch does not work is worse than no notice.
        self.do_import(notify="on")
        subscription = Subscription.objects.get(email_field="ada@example.com")

        self.client.post(
            subscription.unsubscribe_activate_url(),
            {"user_activation_code": subscription.activation_code})

        subscription.refresh_from_db()
        self.assertTrue(subscription.unsubscribed)

    @override_settings(MAILING_LIST_IMPORT_NOTICE_MAX=1)
    def test_too_many_to_notify_imports_anyway_and_says_so(self):
        with self.assertLogs("main.mailing", level="WARNING"):
            response = self.do_import(notify="on")

        self.assertEqual(len(self.subscribers()), 2)
        self.assertEqual(mail.outbox, [])
        self.assertContains(response, "too many addresses to email")

    def test_a_suppressed_address_is_never_added(self):
        SuppressedAddress.objects.create(
            email="grace@example.com", reason="asked to be left alone")

        with self.assertLogs("main.mailing", level="INFO"):
            response = self.do_import()

        self.assertEqual(self.subscribers(), ["ada@example.com"])
        self.assertContains(response, "grace@example.com")

    def test_suppression_matching_ignores_case_in_the_stored_row(self):
        # bulk_create bypasses save(), so the stored value is NOT normalised.
        # A suppressed address the matcher misses is one an import adds.
        SuppressedAddress.objects.bulk_create(
            [SuppressedAddress(email="Grace@Example.COM")])

        with self.assertLogs("main.mailing", level="INFO"):
            self.do_import()

        self.assertEqual(self.subscribers(), ["ada@example.com"])

    def test_an_address_already_on_the_list_is_left_alone(self):
        self.subscriber("ada@example.com", self.dc4k)

        response = self.do_import(notify="")

        self.assertEqual(Subscription.objects.filter(
            newsletter=self.dc4k, email_field="ada@example.com").count(), 1)
        self.assertContains(response, "1 already on that list")

    def test_an_import_cannot_resurrect_somebody_who_unsubscribed(self):
        left = self.subscriber("ada@example.com", self.dc4k)
        left.update("unsubscribe")

        self.do_import(notify="")

        left.refresh_from_db()
        self.assertTrue(left.unsubscribed)

    def test_a_file_with_no_addresses_is_an_error_not_a_500(self):
        response = self.do_import("Timestamp,Notes\n2026/07/26,nothing\n")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No email addresses found")

    def test_a_subscriber_import_has_to_say_which_list(self):
        # Importing into the wrong list is the mistake to be afraid of here.
        response = self.client.post(self.IMPORT_URL, {
            "mode": "subscribe",
            "address_file": self.upload(self.MAILCHIMP)})

        self.assertContains(response, "Pick which list")
        self.assertFalse(Subscription.objects.exists())

    def test_the_page_needs_staff(self):
        self.client.logout()

        response = self.client.post(self.IMPORT_URL, {
            "mode": "subscribe", "newsletter": self.dc4k.pk,
            "address_file": self.upload(self.MAILCHIMP)})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Subscription.objects.exists())


class SuppressionTest(ImportTestBase):
    """The never-email list, and loading Mailchimp's unsubscribes into it."""

    def suppress(self, text, **extra):
        data = {"mode": "suppress", "address_file": self.upload(text)}
        data.update(extra)
        return self.client.post(self.IMPORT_URL, data, follow=True)

    def test_an_export_of_unsubscribes_fills_the_list(self):
        self.suppress(self.MAILCHIMP, reason="Mailchimp unsubscribes")

        self.assertEqual(
            sorted(SuppressedAddress.objects.values_list("email", flat=True)),
            ["ada@example.com", "grace@example.com"])
        self.assertEqual(
            SuppressedAddress.objects.first().reason,
            "Mailchimp unsubscribes")

    def test_suppressing_takes_them_off_the_lists_they_are_on(self):
        # Recording "stop" while leaving a live subscription behind would mean
        # the next mailing goes out to them anyway.
        live = self.subscriber("ada@example.com", self.general)

        self.suppress("Email Address\nada@example.com\n")

        live.refresh_from_db()
        self.assertTrue(live.unsubscribed)

    def test_it_suppresses_addresses_that_are_already_subscribed(self):
        # The reason this does not use django-newsletter's parser: that one
        # drops addresses that already have a subscription, which is exactly
        # the set that needs suppressing.
        self.subscriber("ada@example.com", self.general)

        self.suppress("Email Address\nada@example.com\n")

        self.assertTrue(
            SuppressedAddress.objects.filter(email="ada@example.com").exists())

    def test_suppressing_twice_does_not_duplicate_a_row(self):
        self.suppress("Email Address\nada@example.com\n")
        self.suppress("Email Address\nada@example.com\n")

        self.assertEqual(SuppressedAddress.objects.count(), 1)

    def test_a_suppression_import_does_not_need_a_list(self):
        response = self.suppress(self.MAILCHIMP)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SuppressedAddress.objects.count(), 2)

    def test_addresses_are_stored_lower_cased(self):
        SuppressedAddress.objects.create(email="  Loud@Example.COM ")

        self.assertEqual(SuppressedAddress.objects.get().email,
                         "loud@example.com")

    def test_staff_can_add_one_by_hand(self):
        response = self.client.post(
            "/timbit/admin/main/suppressedaddress/add/",
            {"email": "byhand@example.com", "reason": "asked us to stop"})

        self.assertEqual(response.status_code, 302)
        row = SuppressedAddress.objects.get(email="byhand@example.com")
        self.assertEqual(row.created_by, self.staff)


class AdminPagesTest(MailingListTestBase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(
            "staff", "staff@example.com", "hunter2hunter2",
            is_staff=True, is_superuser=True)

    def test_the_admin_moved_under_timbit(self):
        response = self.client.get("/timbit/admin/")

        # Not logged in, so the admin bounces to its own login -- but it is
        # there, which is what this is checking.
        self.assertEqual(response.status_code, 302)
        self.assertIn("/timbit/admin/login/", response["Location"])

    def test_the_old_admin_path_still_gets_you_there(self):
        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/timbit/admin/")

    def test_a_deep_old_admin_link_keeps_its_path(self):
        response = self.client.get("/admin/main/product/")

        self.assertEqual(response["Location"], "/timbit/admin/main/product/")

    def test_the_admin_home_needs_staff(self):
        response = self.client.get("/timbit/admin/home")

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_the_admin_home_lists_the_pages(self):
        self.client.force_login(self.staff)

        response = self.client.get("/timbit/admin/home")

        self.assertEqual(response.status_code, 200)
        for path in ["/timbit/admin/newsletter/subscription/",
                     "/timbit/admin/newsletter/newsletter/",
                     "/timbit/admin/mailing-list/import",
                     "/timbit/admin/main/suppressedaddress/",
                     "/timbit/admin/main/mailinglistmessage/",
                     "mailing-list/signup-form.html"]:
            self.assertContains(response, path)

    def test_sending_is_reachable_from_a_saved_mailing(self):
        # Saving a mailing is not sending it, so the change page has to say
        # where sending happens.
        message = MailingListMessage.objects.create(subject="s", body="b")
        self.client.force_login(self.staff)

        response = self.client.get(
            f"/timbit/admin/main/mailinglistmessage/{message.pk}/change/")

        self.assertContains(
            response, f"/timbit/admin/mailing-list/send/{message.pk}")

    def test_the_import_page_says_which_list_and_names_the_suppression_list(
            self):
        self.client.force_login(self.staff)

        response = self.client.get("/timbit/admin/mailing-list/import")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mailing list")
        self.assertContains(response, "suppressedaddress")

    def test_the_send_page_needs_staff(self):
        message = MailingListMessage.objects.create(subject="s", body="b")

        response = self.client.get(
            f"/timbit/admin/mailing-list/send/{message.pk}")

        self.assertEqual(response.status_code, 302)

    def test_the_send_page_says_who_it_would_go_to(self):
        self.subscriber("in@example.com", self.general)
        message = MailingListMessage.objects.create(subject="s", body="b")
        message.interests.add(self.general)
        self.client.force_login(self.staff)

        response = self.client.get(
            f"/timbit/admin/mailing-list/send/{message.pk}")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.general.title)

    def test_staff_can_send_a_batch_from_the_page(self):
        self.subscriber("in@example.com", self.general)
        message = MailingListMessage.objects.create(subject="s", body="b")
        self.client.force_login(self.staff)
        mail.outbox.clear()

        response = self.client.post(
            f"/timbit/admin/mailing-list/send/{message.pk}",
            {"send_batch": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual([m.to[0] for m in mail.outbox], ["in@example.com"])

    def test_staff_can_send_themselves_a_test(self):
        self.subscriber("staff@example.com", self.general)
        message = MailingListMessage.objects.create(subject="s", body="b")
        self.client.force_login(self.staff)
        mail.outbox.clear()

        self.client.post(f"/timbit/admin/mailing-list/send/{message.pk}",
                         {"send_test": "1"})

        self.assertEqual([m.to[0] for m in mail.outbox], ["staff@example.com"])
