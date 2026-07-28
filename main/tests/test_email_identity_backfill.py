from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext, override_settings

from main.models import EmailIdentity


@override_settings(
    ADMINS=[("Owner", "owner@example.com")],
    DEFAULT_FROM_EMAIL="support@pigscanfly.ca",
)
class EmailIdentityBackfillCommandTest(TestCase):
    def run_command(self, *args):
        out = StringIO()
        err = StringIO()
        call_command("backfill_email_identities", *args, stdout=out, stderr=err)
        self.last_stderr = err.getvalue()
        return out.getvalue()

    def test_backfill_creates_missing_identity_rows(self):
        first = User.objects.create_user(
            username="first", email="first@example.com", password="x")
        second = User.objects.create_user(
            username="second", email="second@example.com", password="x")

        output = self.run_command()

        self.assertIn("created=2", output)
        self.assertEqual(
            list(EmailIdentity.objects.order_by("normalized_email").values_list(
                "normalized_email", "user_id")),
            [("first@example.com", first.pk), ("second@example.com", second.pk)],
        )

    def test_backfill_logs_duplicate_addresses_and_continues(self):
        first = User.objects.create_user(
            username="first", email="dup@example.com", password="x")
        second = User.objects.create_user(
            username="second", email="DUP@example.com", password="x")

        with self.assertLogs(
                "main.management.commands.backfill_email_identities",
                level="WARNING") as caught:
            output = self.run_command()

        self.assertIn("created=1", output)
        self.assertIn("duplicates=1", output)
        self.assertEqual(EmailIdentity.objects.count(), 1)
        self.assertEqual(
            EmailIdentity.objects.get().normalized_email, "dup@example.com")
        self.assertEqual(EmailIdentity.objects.get().user_id, first.pk)
        second.refresh_from_db()
        self.assertFalse(hasattr(second, "email_identity"))
        self.assertIn("already reserved", "\n".join(caught.output))

    def test_backfill_claims_an_orphaned_identity_row(self):
        user = User.objects.create_user(
            username="orphaned", email="person@example.com", password="x")
        EmailIdentity.objects.create(normalized_email="person@example.com")

        output = self.run_command()

        self.assertIn("claimed=1", output)
        self.assertEqual(
            EmailIdentity.objects.get(normalized_email="person@example.com").user_id,
            user.pk,
        )

    def test_second_run_writes_nothing(self):
        User.objects.create_user(
            username="first", email="first@example.com", password="x")
        User.objects.create_user(
            username="second", email="second@example.com", password="x")

        first = self.run_command()
        with CaptureQueriesContext(connection) as captured:
            second = self.run_command()

        writes = [query["sql"] for query in captured.captured_queries
                  if query["sql"].lstrip().upper().startswith(
                      ("INSERT", "UPDATE", "DELETE"))]
        self.assertIn("created=2", first)
        self.assertIn("created=0", second)
        self.assertIn("claimed=0", second)
        self.assertIn("duplicates=0", second)
        self.assertIn("errors=0", second)
        self.assertEqual(writes, [])

    def test_backfill_swallow_unexpected_row_errors(self):
        User.objects.create_user(
            username="first", email="first@example.com", password="x")
        User.objects.create_user(
            username="second", email="second@example.com", password="x")

        original = EmailIdentity.objects.get_or_create

        def flaky_get_or_create(*args, **kwargs):
            if kwargs.get("normalized_email") == "first@example.com":
                raise RuntimeError("boom")
            return original(*args, **kwargs)

        with mock.patch.object(
                EmailIdentity.objects, "get_or_create",
                side_effect=flaky_get_or_create):
            with self.assertLogs(
                    "main.management.commands.backfill_email_identities",
                    level="ERROR"):
                output = self.run_command()

        self.assertIn("errors=1", output)
        self.assertIn("created=1", output)
        self.assertTrue(EmailIdentity.objects.filter(
            normalized_email="second@example.com").exists())

    def test_top_level_failure_mails_admins_and_can_fail(self):
        out = StringIO()
        err = StringIO()
        with mock.patch(
                "main.management.commands.backfill_email_identities.backfill_email_identities",
                side_effect=RuntimeError("boom")):
            with self.assertRaises(SystemExit) as caught:
                call_command(
                    "backfill_email_identities", "--fail",
                    stdout=out, stderr=err)

        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].subject,
            "[pcfweb] EmailIdentity backfill failed on primary startup",
        )
        self.assertIn("RuntimeError: boom", mail.outbox[0].body)
        self.assertIn("did not complete", err.getvalue())

    def test_alert_mail_failure_is_logged_not_raised(self):
        with mock.patch(
                "main.management.commands.backfill_email_identities.backfill_email_identities",
                side_effect=RuntimeError("boom")):
            with mock.patch(
                    "main.utils.send_mail",
                    side_effect=RuntimeError("smtp down")):
                with self.assertLogs(
                        "main.management.commands.backfill_email_identities",
                        level="ERROR") as caught:
                    output = self.run_command()

        self.assertEqual(output, "")
        self.assertEqual(mail.outbox, [])
        self.assertIn("Could not email owner@example.com", "\n".join(caught.output))
