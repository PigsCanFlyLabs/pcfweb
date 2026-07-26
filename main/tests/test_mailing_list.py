"""The mailing list: signup, confirmation, the CSV import and sending.

The endpoints here are deliberately CSRF exempt so forms on other sites can
post to them, which makes several of these tests assertions about what that
does *not* open up -- an open redirect, a way to unsubscribe a stranger, a
way to be mailed without confirming.
"""

import importlib
import io
from unittest import mock

from django.apps import apps as django_apps

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from main.mailing import CsvImportError, import_csv
from main.models import (
    InterestArea, MailingListDelivery, MailingListMessage,
    MailingListSubscription)


SUBSCRIBE_URL = "/mailing-list/subscribe"


def csv_file(text: str, name: str = "list.csv"):
    upload = io.BytesIO(text.encode("utf-8"))
    upload.name = name
    return upload


class MailingListTestBase(TestCase):
    def setUp(self):
        # The signup rate limiter counts in the process-local cache, which
        # outlives a test case. Without this, tests poison each other in
        # whatever order they happen to run in.
        cache.clear()
        # The seeded groups (see migration 0012), not invented ones: these
        # tests should break if a slug an embedded form depends on changes.
        self.general = InterestArea.get_default()
        self.dc4k = InterestArea.objects.get(slug="dc4k")
        self.everything = InterestArea.objects.get(slug="all")


class SignupTest(MailingListTestBase):
    def test_signup_records_a_pending_row_and_asks_for_confirmation(self):
        response = self.client.post(SUBSCRIBE_URL, {"email": "A@Example.com"})

        self.assertEqual(response.status_code, 200)
        subscription = MailingListSubscription.objects.get()
        # Normalised, so a second signup as a@example.com is the same person.
        self.assertEqual(subscription.email, "a@example.com")
        self.assertEqual(subscription.status,
                         MailingListSubscription.Status.PENDING)
        self.assertEqual(subscription.interest, self.general)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(subscription.token, mail.outbox[0].body)

    def test_signup_without_an_area_lands_in_the_general_group(self):
        self.client.post(SUBSCRIBE_URL, {"email": "nobody@example.com"})

        self.assertEqual(
            MailingListSubscription.objects.get().interest.slug, "general")

    def test_signup_can_name_an_area(self):
        self.client.post(SUBSCRIBE_URL,
                         {"email": "kid@example.com", "interest": "dc4k"})

        self.assertEqual(
            MailingListSubscription.objects.get().interest, self.dc4k)

    def test_an_unknown_area_falls_back_rather_than_losing_the_signup(self):
        # An embedded form on another site carries a hard-coded slug. If we
        # rename or retire the area, that form must not start throwing away
        # addresses.
        self.client.post(SUBSCRIBE_URL,
                         {"email": "kid@example.com", "interest": "gone"})

        self.assertEqual(
            MailingListSubscription.objects.get().interest, self.general)

    def test_an_inactive_area_is_not_accepted_for_new_signups(self):
        self.dc4k.active = False
        self.dc4k.save()

        self.client.post(SUBSCRIBE_URL,
                         {"email": "kid@example.com", "interest": "dc4k"})

        self.assertEqual(
            MailingListSubscription.objects.get().interest, self.general)

    def test_the_endpoint_does_not_require_a_csrf_token(self):
        # The entire point: a plain <form> pasted onto another site has no
        # token to send. enforce_csrf_checks is what the real middleware does.
        client = Client(enforce_csrf_checks=True)

        response = client.post(SUBSCRIBE_URL, {"email": "far@example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            MailingListSubscription.objects.filter(
                email="far@example.com").exists())

    def test_signing_up_twice_does_not_make_a_second_row(self):
        self.client.post(SUBSCRIBE_URL, {"email": "twice@example.com"})
        self.client.post(SUBSCRIBE_URL, {"email": "twice@example.com"})

        self.assertEqual(MailingListSubscription.objects.count(), 1)

    def test_the_same_address_can_be_in_two_groups(self):
        self.client.post(SUBSCRIBE_URL, {"email": "both@example.com"})
        self.client.post(SUBSCRIBE_URL,
                         {"email": "both@example.com", "interest": "dc4k"})

        self.assertEqual(MailingListSubscription.objects.count(), 2)

    def test_resubscribing_an_active_subscriber_cannot_reset_them(self):
        subscription = MailingListSubscription.subscribe(
            "member@example.com", confirmed=True)
        mail.outbox.clear()

        self.client.post(SUBSCRIBE_URL, {"email": "member@example.com"})

        subscription.refresh_from_db()
        self.assertEqual(subscription.status,
                         MailingListSubscription.Status.SUBSCRIBED)
        # No confirmation mail either: it would be unsolicited mail to
        # somebody an attacker only had to know the address of.
        self.assertEqual(mail.outbox, [])

    def test_signing_up_again_after_unsubscribing_needs_a_new_confirmation(self):
        subscription = MailingListSubscription.subscribe(
            "back@example.com", confirmed=True)
        subscription.unsubscribe()
        old_token = subscription.token

        self.client.post(SUBSCRIBE_URL, {"email": "back@example.com"})

        subscription.refresh_from_db()
        self.assertEqual(subscription.status,
                         MailingListSubscription.Status.PENDING)
        self.assertNotEqual(subscription.token, old_token)

    def test_a_bad_address_is_rejected(self):
        response = self.client.post(SUBSCRIBE_URL, {"email": "not-an-email"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(MailingListSubscription.objects.exists())

    def test_the_honeypot_field_silently_drops_the_signup(self):
        response = self.client.post(
            SUBSCRIBE_URL,
            {"email": "bot@example.com", "website": "http://spam.example"})

        # Looks exactly like a success, so whatever filled it in learns
        # nothing, but nothing was recorded and nothing was mailed.
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MailingListSubscription.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_a_broken_mail_server_does_not_break_the_signup(self):
        # The signup often happens on somebody else's site. Our SMTP being
        # down must not show up there as an error.
        with mock.patch("main.models.send_mail",
                        side_effect=OSError("connection refused")):
            with self.assertLogs("main.models", level="ERROR"):
                response = self.client.post(
                    SUBSCRIBE_URL, {"email": "held@example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(MailingListSubscription.objects.get().status,
                         MailingListSubscription.Status.PENDING)

    def test_a_forged_x_forwarded_for_does_not_break_the_insert(self):
        response = self.client.post(
            SUBSCRIBE_URL, {"email": "spoof@example.com"},
            HTTP_X_FORWARDED_FOR="not-an-ip")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(MailingListSubscription.objects.get().ip)

    @override_settings(MAILING_LIST_SIGNUP_RATE_LIMIT=2)
    def test_a_flood_of_signups_stops_getting_confirmation_emails(self):
        # Anybody can post here, so without a ceiling this endpoint is a way
        # to have us mail an address somebody else picked, over and over.
        for i in range(2):
            self.client.post(SUBSCRIBE_URL,
                             {"email": f"flood{i}@example.com"})
        with self.assertLogs("main.views", level="WARNING"):
            for i in range(2, 4):
                self.client.post(SUBSCRIBE_URL,
                                 {"email": f"flood{i}@example.com"})

        self.assertEqual(len(mail.outbox), 2)
        # The signups are still recorded; a real person can try again later.
        self.assertEqual(MailingListSubscription.objects.count(), 4)

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
        self.assertEqual(
            MailingListSubscription.objects.get().interest, self.dc4k)

    def test_a_malformed_json_body_is_a_400_not_a_500(self):
        response = self.client.post(
            SUBSCRIBE_URL, data="{not json", content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_preflight_is_answered(self):
        response = self.client.options(SUBSCRIBE_URL)

        self.assertEqual(response.status_code, 204)
        self.assertIn("POST", response["Access-Control-Allow-Methods"])


@override_settings(MAILING_LIST_REDIRECT_HOSTS=["dc4k.example"])
class RedirectBackTest(MailingListTestBase):
    def test_a_listed_host_gets_the_visitor_back(self):
        response = self.client.post(SUBSCRIBE_URL, {
            "email": "kid@example.com",
            "next": "https://dc4k.example/thanks"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"],
                         "https://dc4k.example/thanks?subscribed=1")

    def test_an_unlisted_host_is_not_redirected_to(self):
        # Otherwise this is an open redirect on an endpoint with no CSRF
        # protection, which is a phishing primitive with our domain on it.
        with self.assertLogs("main.views", level="INFO"):
            response = self.client.post(SUBSCRIBE_URL, {
                "email": "kid@example.com",
                "next": "https://evil.example/landing"})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "mailing_list_result.html")

    def test_the_signup_is_still_recorded_when_the_redirect_is_refused(self):
        with self.assertLogs("main.views", level="INFO"):
            self.client.post(SUBSCRIBE_URL, {
                "email": "kid@example.com",
                "next": "https://evil.example/landing"})

        self.assertTrue(MailingListSubscription.objects.exists())


class SiteDomainsTest(MailingListTestBase):
    """The other sites that host a signup form, using the real setting."""

    SITES = ["liberatedbread.com", "distributedcomputing4kids.com",
             "distributedcomputing4executives.com", "highperformancespark.com"]

    def test_each_site_can_send_the_visitor_back_to_itself(self):
        for index, site in enumerate(self.SITES):
            for host in (site, f"www.{site}"):
                with self.subTest(host=host):
                    response = self.client.post(SUBSCRIBE_URL, {
                        "email": f"reader{index}@{host}",
                        "next": f"https://{host}/thanks"})

                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response["Location"],
                                     f"https://{host}/thanks?subscribed=1")

    def test_a_site_we_do_not_run_is_still_refused(self):
        with self.assertLogs("main.views", level="INFO"):
            response = self.client.post(SUBSCRIBE_URL, {
                "email": "reader@example.com",
                "next": "https://liberatedbread.com.evil.example/landing"})

        self.assertEqual(response.status_code, 200)

    def test_the_environment_adds_to_the_built_in_sites_rather_than_replacing(
            self):
        # Adding a site by restating the existing ones is how the forms
        # already deployed on them quietly stop working.
        from pigscanfly.settings import merge_hosts

        merged = merge_hosts(["liberatedbread.com"], "example.org")

        self.assertEqual(merged, ["liberatedbread.com", "example.org"])

    def test_the_embed_page_says_which_sites_are_set_up(self):
        response = self.client.get("/mailing-list/embed")

        for site in self.SITES:
            self.assertContains(response, site)


class ConfirmAndUnsubscribeTest(MailingListTestBase):
    def setUp(self):
        super().setUp()
        self.subscription = MailingListSubscription.subscribe(
            "person@example.com", interest=self.dc4k)

    def test_the_link_in_the_email_subscribes_them(self):
        response = self.client.get(
            f"/mailing-list/confirm/{self.subscription.token}")

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status,
                         MailingListSubscription.Status.SUBSCRIBED)
        self.assertIsNotNone(self.subscription.confirmed_at)

    def test_confirming_twice_is_harmless(self):
        url = f"/mailing-list/confirm/{self.subscription.token}"
        self.client.get(url)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status,
                         MailingListSubscription.Status.SUBSCRIBED)

    def test_an_old_confirmation_link_cannot_undo_an_unsubscribe(self):
        # Unsubscribing does not rotate the token, so the original
        # confirmation email still carries a working link. A forwarded copy
        # of it -- or a link scanner reaching it late -- must not put
        # somebody back on a list they left.
        self.subscription.mark_subscribed()
        self.subscription.unsubscribe()

        with self.assertLogs("main.views", level="INFO"):
            response = self.client.get(
                f"/mailing-list/confirm/{self.subscription.token}")

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status,
                         MailingListSubscription.Status.UNSUBSCRIBED)

    def test_an_unknown_token_is_a_404(self):
        self.assertEqual(
            self.client.get("/mailing-list/confirm/nope").status_code, 404)

    def test_getting_the_unsubscribe_link_does_not_unsubscribe(self):
        # Mail clients prefetch links. Doing the unsubscribe on the GET would
        # let a scanner drop somebody off the list without them touching it.
        self.subscription.mark_subscribed()

        response = self.client.get(
            f"/mailing-list/unsubscribe/{self.subscription.token}")

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status,
                         MailingListSubscription.Status.SUBSCRIBED)

    def test_posting_the_unsubscribe_form_unsubscribes(self):
        self.subscription.mark_subscribed()

        self.client.post(
            f"/mailing-list/unsubscribe/{self.subscription.token}")

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status,
                         MailingListSubscription.Status.UNSUBSCRIBED)
        self.assertIsNotNone(self.subscription.unsubscribed_at)

    def test_one_click_unsubscribe_works_without_a_csrf_token(self):
        # RFC 8058: the mail client posts this itself, from no origin.
        self.subscription.mark_subscribed()
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            f"/mailing-list/unsubscribe/{self.subscription.token}",
            {"List-Unsubscribe": "One-Click"})

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status,
                         MailingListSubscription.Status.UNSUBSCRIBED)


class SeededInterestAreasTest(MailingListTestBase):
    """The initial groups. Their slugs end up in markup on other sites, so
    they are part of the interface, not just data."""

    EXPECTED = ["all", "general", "books", "dc4k", "high-performance-spark",
                "liberatedbread", "fight-health-insurance"]

    def test_every_group_is_seeded_in_order(self):
        self.assertEqual(
            [area.slug for area in InterestArea.objects.all()], self.EXPECTED)

    def test_the_liberated_bread_slug_matches_its_domain(self):
        # Whoever pastes the form onto liberatedbread.com will reach for the
        # name of the site, not a hyphenated version of it.
        self.assertTrue(
            InterestArea.objects.filter(slug="liberatedbread").exists())
        self.assertFalse(
            InterestArea.objects.filter(slug="liberated-bread").exists())

    def test_the_rename_migration_moves_an_already_seeded_row(self):
        # Databases that ran the first version of the seed have the old slug;
        # this is what moves them.
        migration = importlib.import_module(
            "main.migrations.0014_liberatedbread_slug")
        InterestArea.objects.filter(slug="liberatedbread").update(
            slug="liberated-bread")

        migration.forwards(django_apps, None)

        self.assertTrue(
            InterestArea.objects.filter(slug="liberatedbread").exists())

    def test_the_rename_migration_leaves_an_existing_target_alone(self):
        # Renaming onto a slug that is already taken is a unique-constraint
        # error, so it has to be a no-op instead.
        migration = importlib.import_module(
            "main.migrations.0014_liberatedbread_slug")
        stray = InterestArea.objects.create(
            slug="liberated-bread", name="Stray")

        migration.forwards(django_apps, None)

        stray.refresh_from_db()
        self.assertEqual(stray.slug, "liberated-bread")

    def test_only_the_all_group_is_a_catch_all(self):
        self.assertEqual(
            [area.slug for area in
             InterestArea.objects.filter(catch_all=True)], ["all"])


class CatchAllGroupTest(MailingListTestBase):
    """"All" has to mean all, or it is a group named after everything whose
    members hear about nothing."""

    def setUp(self):
        super().setUp()
        self.everyone = MailingListSubscription.subscribe(
            "everything@example.com", interest=self.everything, confirmed=True)
        self.kid = MailingListSubscription.subscribe(
            "kid@example.com", interest=self.dc4k, confirmed=True)
        self.plain = MailingListSubscription.subscribe(
            "general@example.com", interest=self.general, confirmed=True)
        mail.outbox.clear()

    def targeted_message(self, *interests):
        message = MailingListMessage.objects.create(
            subject="Book news", body="News.")
        for interest in interests:
            message.interests.add(interest)
        return message

    def test_a_group_mailing_also_reaches_the_all_subscribers(self):
        self.targeted_message(self.dc4k).send_batch()

        self.assertEqual(sorted(m.to[0] for m in mail.outbox),
                         ["everything@example.com", "kid@example.com"])

    def test_it_does_not_drag_in_unrelated_groups(self):
        self.targeted_message(self.dc4k).send_batch()

        self.assertNotIn("general@example.com",
                         [m.to[0] for m in mail.outbox])

    def test_being_in_all_and_in_the_targeted_group_is_still_one_copy(self):
        MailingListSubscription.subscribe(
            "everything@example.com", interest=self.dc4k, confirmed=True)
        message = self.targeted_message(self.dc4k)

        message.send_batch()

        self.assertEqual(
            [m.to[0] for m in mail.outbox].count("everything@example.com"), 1)
        self.assertEqual(message.pending_count(), 0)

    def test_an_all_subscriber_who_unsubscribes_stops_getting_everything(self):
        self.everyone.unsubscribe()

        self.targeted_message(self.dc4k).send_batch()

        self.assertEqual([m.to[0] for m in mail.outbox], ["kid@example.com"])


class SubscribePageTest(MailingListTestBase):
    def test_the_groups_are_offered_in_their_curated_order(self):
        self.assertEqual(
            [area.slug for area in InterestArea.signup_choices()][:3],
            ["all", "general", "books"])

    def test_the_general_group_is_the_preselected_option(self):
        # The rule is that not choosing means the general group, and a select
        # box submits its first option when nobody touches it -- so "All",
        # which is listed first, must not be able to take that slot.
        response = self.client.get("/subscribe")

        self.assertContains(response, 'value="general" selected')
        self.assertNotContains(response, 'value="all" selected')

    def test_the_page_offers_every_active_group(self):
        response = self.client.get("/subscribe")

        self.assertContains(response, 'value="dc4k"')
        self.assertContains(response, 'value="general"')

    def test_an_inactive_group_is_not_offered(self):
        self.dc4k.active = False
        self.dc4k.save()

        response = self.client.get("/subscribe")

        self.assertNotContains(response, 'value="dc4k"')


class EmbedTest(MailingListTestBase):
    def test_the_embeddable_form_can_be_framed(self):
        response = self.client.get("/mailing-list/embed/dc4k")

        self.assertEqual(response.status_code, 200)
        # X-Frame-Options would stop the whole point of this page.
        self.assertNotIn("X-Frame-Options", response)
        self.assertContains(response, 'name="interest" value="dc4k"')

    def test_an_unknown_area_is_a_404_rather_than_a_wrong_signup_form(self):
        self.assertEqual(
            self.client.get("/mailing-list/embed/nope").status_code, 404)

    def test_the_embed_code_page_shows_the_endpoint(self):
        response = self.client.get("/mailing-list/embed?interest=dc4k")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, SUBSCRIBE_URL)

    def test_the_static_snippet_ships(self):
        from django.contrib.staticfiles import finders

        self.assertIsNotNone(finders.find("mailing-list/signup-form.html"))


class CsvImportTest(MailingListTestBase):
    def test_a_headered_file_imports(self):
        result = import_csv(
            csv_file("email,name\na@example.com,Ada\nb@example.com,Bo\n"),
            interest=self.dc4k)

        self.assertEqual(result.created, 2)
        self.assertEqual(
            MailingListSubscription.objects.filter(
                interest=self.dc4k,
                status=MailingListSubscription.Status.SUBSCRIBED).count(), 2)

    def test_imported_rows_are_subscribed_without_a_confirmation_email(self):
        # An import is the owner saying they already have consent; mailing
        # everybody a confirmation would be the surprise, not the safeguard.
        import_csv(csv_file("email\na@example.com\n"), interest=self.general)

        self.assertEqual(mail.outbox, [])
        self.assertEqual(MailingListSubscription.objects.get().status,
                         MailingListSubscription.Status.SUBSCRIBED)

    def test_a_bare_column_of_addresses_imports(self):
        result = import_csv(
            csv_file("a@example.com\nb@example.com\n"), interest=self.general)

        self.assertEqual(result.created, 2)

    def test_a_dry_run_writes_nothing(self):
        result = import_csv(
            csv_file("email\na@example.com\n"), interest=self.general,
            dry_run=True)

        self.assertEqual(result.created, 1)
        self.assertFalse(MailingListSubscription.objects.exists())

    def test_a_bad_row_is_reported_and_the_rest_still_import(self):
        result = import_csv(
            csv_file("email\ngood@example.com\nnonsense\n"),
            interest=self.general)

        self.assertEqual(result.created, 1)
        self.assertEqual([row for row, _ in result.errors], [3])

    def test_re_importing_the_same_file_changes_nothing(self):
        text = "email,name\na@example.com,Ada\n"
        import_csv(csv_file(text), interest=self.general)

        result = import_csv(csv_file(text), interest=self.general)

        self.assertEqual(result.created, 0)
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(MailingListSubscription.objects.count(), 1)

    def test_an_import_will_not_resubscribe_somebody_who_left(self):
        subscription = MailingListSubscription.subscribe(
            "gone@example.com", interest=self.general, confirmed=True)
        subscription.unsubscribe()

        result = import_csv(
            csv_file("email\ngone@example.com\n"), interest=self.general)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status,
                         MailingListSubscription.Status.UNSUBSCRIBED)
        self.assertEqual(len(result.errors), 1)

    def test_a_status_column_is_honoured(self):
        import_csv(
            csv_file("email,status\na@example.com,unsubscribed\n"),
            interest=self.general)

        self.assertEqual(MailingListSubscription.objects.get().status,
                         MailingListSubscription.Status.UNSUBSCRIBED)

    def test_an_interest_column_routes_rows_to_their_group(self):
        import_csv(
            csv_file("email,interest\na@example.com,dc4k\n"
                     "b@example.com,general\n"),
            interest=self.general)

        self.assertEqual(
            MailingListSubscription.objects.get(
                email="a@example.com").interest, self.dc4k)

    def test_an_unknown_interest_is_a_row_error_not_a_new_group(self):
        result = import_csv(
            csv_file("email,interest\na@example.com,invented\n"),
            interest=self.general)

        self.assertEqual(len(result.errors), 1)
        self.assertFalse(
            InterestArea.objects.filter(slug="invented").exists())

    def test_a_semicolon_separated_export_imports(self):
        result = import_csv(
            csv_file("email;name\na@example.com;Ada\n"), interest=self.general)

        self.assertEqual(result.created, 1)

    def test_an_excel_byte_order_mark_does_not_break_the_header(self):
        upload = io.BytesIO(
            "email,name\na@example.com,Ada\n".encode("utf-8-sig"))

        result = import_csv(upload, interest=self.general)

        self.assertEqual(result.created, 1)

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(CsvImportError):
            import_csv(csv_file(""), interest=self.general)

    def test_an_oversized_file_is_refused_before_it_is_parsed(self):
        with self.assertRaises(CsvImportError):
            import_csv(csv_file("a@example.com\n" * 500000),
                       interest=self.general)


class SendingTest(MailingListTestBase):
    def setUp(self):
        super().setUp()
        self.confirmed = MailingListSubscription.subscribe(
            "in@example.com", interest=self.general, confirmed=True)
        self.pending = MailingListSubscription.subscribe(
            "maybe@example.com", interest=self.general)
        self.kid = MailingListSubscription.subscribe(
            "kid@example.com", interest=self.dc4k, confirmed=True)
        mail.outbox.clear()

    def message(self, *interests):
        message = MailingListMessage.objects.create(
            subject="Hello", body="Some news.")
        for interest in interests:
            message.interests.add(interest)
        return message

    def test_a_message_with_no_groups_goes_to_everyone_confirmed(self):
        message = self.message()

        sent, failed = message.send_batch()

        self.assertEqual((sent, failed), (2, 0))
        self.assertEqual(
            sorted(m.to[0] for m in mail.outbox),
            ["in@example.com", "kid@example.com"])

    def test_a_message_can_be_limited_to_one_group(self):
        message = self.message(self.dc4k)

        message.send_batch()

        self.assertEqual([m.to[0] for m in mail.outbox], ["kid@example.com"])

    def test_an_unconfirmed_address_is_never_mailed(self):
        self.message().send_batch()

        self.assertNotIn("maybe@example.com",
                         [m.to[0] for m in mail.outbox])

    def test_an_unsubscribed_address_is_never_mailed(self):
        self.confirmed.unsubscribe()

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

    def test_somebody_in_two_selected_groups_only_gets_one_copy(self):
        MailingListSubscription.subscribe(
            "in@example.com", interest=self.dc4k, confirmed=True)
        message = self.message(self.general, self.dc4k)

        message.send_batch()

        self.assertEqual(
            [m.to[0] for m in mail.outbox].count("in@example.com"), 1)
        # ...and the duplicate row is accounted for, so the send can finish.
        self.assertEqual(message.pending_count(), 0)

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

        sent, failed = message.send_batch()

        self.assertEqual((sent, failed), (0, 0))

    def test_every_message_carries_a_working_unsubscribe_link(self):
        self.message().send_batch()

        body = next(m for m in mail.outbox if m.to == ["in@example.com"]).body
        self.assertIn(self.confirmed.token, body)

    def test_the_list_unsubscribe_header_is_set(self):
        self.message().send_batch()

        headers = mail.outbox[0].extra_headers
        self.assertIn("List-Unsubscribe", headers)
        self.assertIn(self.confirmed.token + ">",
                      headers["List-Unsubscribe"] + ">")

    def test_the_body_can_address_the_recipient(self):
        self.confirmed.name = "Ada"
        self.confirmed.save()
        message = MailingListMessage.objects.create(
            subject="Hi", body="Hello {{ name }}, news follows.")

        message.send_batch()

        body = next(m for m in mail.outbox if m.to == ["in@example.com"]).body
        self.assertIn("Hello Ada", body)

    def test_a_recipient_another_send_already_claimed_is_left_alone(self):
        # What a second click, a reload or a concurrent `send_mailing` looks
        # like from in here: the delivery row is already there, so that
        # address must not get a second copy.
        message = self.message()
        MailingListDelivery.objects.create(
            message=message, subscription=self.confirmed)

        sent, failed = message.send_batch()

        self.assertEqual((sent, failed), (1, 0))
        self.assertEqual([m.to[0] for m in mail.outbox], ["kid@example.com"])

    def test_two_senders_cannot_both_claim_the_same_recipient(self):
        # The batch is read before it is sent, so two processes can both have
        # the same recipient in hand. The unique constraint on the delivery
        # row is what stops both of them mailing it.
        message = self.message()

        first = message._claim(
            self.confirmed, MailingListDelivery.Status.SENT)
        with self.assertLogs("main.models", level="INFO"):
            second = message._claim(
                self.confirmed, MailingListDelivery.Status.SENT)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_a_body_that_cannot_render_is_rejected_before_it_is_saved(self):
        # Otherwise it is discovered at send time, as every delivery failing.
        message = MailingListMessage(subject="Broken", body="Hello {% oops %}")

        with self.assertRaises(ValidationError):
            message.full_clean()

    def test_two_rows_for_one_address_cannot_both_be_claimed(self):
        # Two concurrent senders can hold different subscription rows for the
        # same person, so the claim has to be exclusive on the address.
        other_row = MailingListSubscription.subscribe(
            self.confirmed.email, interest=self.dc4k, confirmed=True)
        message = self.message()

        first = message._claim(
            self.confirmed, MailingListDelivery.Status.SENT)
        with self.assertLogs("main.models", level="INFO"):
            second = message._claim(
                other_row, MailingListDelivery.Status.SENT)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_somebody_who_subscribes_mid_send_is_not_added_to_it(self):
        message = self.message()
        message.send_batch(limit=1)

        MailingListSubscription.subscribe(
            "late@example.com", interest=self.general, confirmed=True)

        self.assertNotIn(
            "late@example.com",
            [s.email for s in message.pending_recipients()])

    def test_a_finished_mailing_does_not_reopen_when_somebody_subscribes(self):
        message = self.message()
        message.send_batch()
        self.assertEqual(message.status, MailingListMessage.Status.SENT)
        mail.outbox.clear()

        MailingListSubscription.subscribe(
            "late@example.com", interest=self.general, confirmed=True)

        self.assertEqual(message.pending_count(), 0)
        self.assertEqual(message.send_batch(), (0, 0))
        self.assertEqual(mail.outbox, [])

    def test_the_command_finishes_a_list_that_starts_with_a_duplicate(self):
        # The duplicate row sorts first, so a batch size of one hands the
        # sender a batch with nothing to send in it. That must not be read as
        # "the list is done" while later recipients are still waiting.
        MailingListSubscription.subscribe(
            self.confirmed.email, interest=self.dc4k, confirmed=True)
        message = self.message(self.general, self.dc4k)
        mail.outbox.clear()

        call_command("send_mailing", message.pk, "--send", "--batch-size", "1",
                     stdout=io.StringIO())

        self.assertEqual(sorted(m.to[0] for m in mail.outbox),
                         ["in@example.com", "kid@example.com"])
        self.assertEqual(message.pending_count(), 0)

    def test_the_command_sends_nothing_without_the_send_flag(self):
        message = self.message()

        call_command("send_mailing", message.pk, stdout=io.StringIO())

        self.assertEqual(mail.outbox, [])

    def test_a_test_send_is_not_recorded_as_a_delivery(self):
        message = self.message()

        message.send_test("owner@example.com")

        self.assertEqual(mail.outbox[0].to, ["owner@example.com"])
        self.assertTrue(mail.outbox[0].subject.startswith("[test]"))
        self.assertEqual(message.deliveries.count(), 0)
        self.assertEqual(message.pending_count(), 2)


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
        for path in ["/timbit/admin/mailing-list/import",
                     "/timbit/admin/main/mailinglistsubscription/",
                     "/timbit/admin/main/mailinglistmessage/",
                     "/mailing-list/embed"]:
            self.assertContains(response, path)

    def test_the_import_page_needs_staff(self):
        response = self.client.post(
            "/timbit/admin/mailing-list/import",
            {"csv_file": csv_file("email\nsneak@example.com\n"),
             "status": MailingListSubscription.Status.SUBSCRIBED})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(MailingListSubscription.objects.exists())

    def test_staff_can_import_a_csv_through_the_page(self):
        self.client.force_login(self.staff)

        response = self.client.post("/timbit/admin/mailing-list/import", {
            "csv_file": csv_file("email\nimported@example.com\n"),
            "interest": self.dc4k.pk,
            "status": MailingListSubscription.Status.SUBSCRIBED,
            "source": "a conference",
        })

        self.assertEqual(response.status_code, 200)
        subscription = MailingListSubscription.objects.get()
        self.assertEqual(subscription.email, "imported@example.com")
        self.assertEqual(subscription.interest, self.dc4k)
        self.assertEqual(subscription.source, "a conference")

    def test_a_dry_run_through_the_page_writes_nothing(self):
        self.client.force_login(self.staff)

        response = self.client.post("/timbit/admin/mailing-list/import", {
            "csv_file": csv_file("email\nimported@example.com\n"),
            "status": MailingListSubscription.Status.SUBSCRIBED,
            "dry_run": "on",
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(MailingListSubscription.objects.exists())

    def test_an_unreadable_upload_is_an_error_not_a_500(self):
        self.client.force_login(self.staff)

        response = self.client.post("/timbit/admin/mailing-list/import", {
            "csv_file": csv_file(""),
            "status": MailingListSubscription.Status.SUBSCRIBED,
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "empty")

    def test_the_send_page_needs_staff(self):
        message = MailingListMessage.objects.create(subject="s", body="b")

        response = self.client.get(
            f"/timbit/admin/mailing-list/send/{message.pk}")

        self.assertEqual(response.status_code, 302)

    def test_the_send_page_says_who_it_would_go_to(self):
        MailingListSubscription.subscribe(
            "in@example.com", interest=self.general, confirmed=True)
        message = MailingListMessage.objects.create(subject="s", body="b")
        message.interests.add(self.general)
        self.client.force_login(self.staff)

        response = self.client.get(
            f"/timbit/admin/mailing-list/send/{message.pk}")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.general.name)

    def test_staff_can_send_a_batch_from_the_page(self):
        MailingListSubscription.subscribe(
            "in@example.com", interest=self.general, confirmed=True)
        message = MailingListMessage.objects.create(subject="s", body="b")
        self.client.force_login(self.staff)
        mail.outbox.clear()

        response = self.client.post(
            f"/timbit/admin/mailing-list/send/{message.pk}",
            {"send_batch": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual([m.to[0] for m in mail.outbox], ["in@example.com"])

    def test_staff_can_send_themselves_a_test(self):
        message = MailingListMessage.objects.create(subject="s", body="b")
        self.client.force_login(self.staff)
        mail.outbox.clear()

        self.client.post(f"/timbit/admin/mailing-list/send/{message.pk}",
                         {"send_test": "1"})

        self.assertEqual([m.to[0] for m in mail.outbox], ["staff@example.com"])
