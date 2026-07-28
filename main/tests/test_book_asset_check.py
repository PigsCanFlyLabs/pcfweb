"""Tests for the startup audit of the downloadable book archives.

``manage.py check_book_assets`` is what stands between a missing book file
and a paying customer discovering it, so what matters here is: it notices a
missing archive, it says so both in the log and by mail, it does not cry wolf
about the titles we deliberately do not deliver, and -- because pod startup
runs it -- it cannot take the primary down.
"""

import re
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings

from main.management.commands.check_book_assets import audit_digital_assets
from main.models import Product
from main.tests.base import EBOOK_PK, EBOOK_STEM, OWNER_EMAIL, BookAssetRootMixin


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOGGER = "main.management.commands.check_book_assets"


@override_settings(ADMINS=[("Owner", OWNER_EMAIL)],
                   DEFAULT_FROM_EMAIL="support@pigscanfly.ca")
class BookAssetAuditTest(BookAssetRootMixin, TestCase):
    """The audit itself, against a real asset directory holding one book."""

    fixtures = ["initial_products"]

    def run_command(self, **options):
        out, err = StringIO(), StringIO()
        call_command("check_book_assets", stdout=out, stderr=err, **options)
        return out.getvalue(), err.getvalue()

    def run_over_a_broken_catalogue(self, **options):
        """run_command(), asserting the ERROR logging it must emit.

        Every call below runs over a catalogue with something missing, so
        capturing the log is both the assertion that it is reported at all
        and what keeps the suite's own output readable.
        """
        with self.assertLogs(LOGGER, level="ERROR") as logs:
            out, err = self.run_command(**options)
        return out, err, logs

    def test_a_complete_catalogue_reports_nothing(self):
        """The fixture's one sellable e-book, and its archive is present."""
        self.assertEqual(audit_digital_assets(), [])

        out, err = self.run_command()

        self.assertIn("has its book file", out)
        self.assertEqual(err, "")
        self.assertEqual(mail.outbox, [])

    def test_a_missing_archive_is_found(self):
        self.archive.unlink()

        problems = audit_digital_assets()

        self.assertEqual([p.pk for p, _ in problems], [EBOOK_PK])
        self.assertIn("missing", problems[0][1])

    def test_a_missing_archive_is_logged_as_an_error(self):
        """The pod log is where a delivery complaint gets debugged."""
        self.archive.unlink()

        _, _, logs = self.run_over_a_broken_catalogue(no_email=True)

        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        self.assertIn(str(EBOOK_PK), message)
        self.assertIn(f"{EBOOK_STEM}.zip", message)

    def test_a_missing_archive_emails_the_owner(self):
        """A log line nobody is tailing is not a notification."""
        self.archive.unlink()

        self.run_over_a_broken_catalogue()

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, [OWNER_EMAIL])
        self.assertIn("Missing e-book file", sent.subject)
        # Enough to act on without opening a shell: which product, which
        # file, where we looked, and what to do about it.
        self.assertIn(str(EBOOK_PK), sent.body)
        self.assertIn(EBOOK_STEM, sent.body)
        self.assertIn(str(self.asset_root), sent.body)
        self.assertIn("pcfweb-book-assets", sent.body)

    def test_an_unset_asset_name_is_reported_as_its_own_problem(self):
        """Not "the file is missing": nothing was ever named to look for."""
        Product.objects.filter(pk=EBOOK_PK).update(digital_asset_name="")

        problems = audit_digital_assets()

        self.assertEqual(len(problems), 1)
        self.assertIn("no digital asset name", problems[0][1])

    def test_a_hostile_asset_name_is_reported_rather_than_raised(self):
        """resolve_asset_path() rejects it; the audit must survive that."""
        Product.objects.filter(pk=EBOOK_PK).update(
            digital_asset_name="../../etc/passwd")

        problems = audit_digital_assets()

        self.assertEqual(len(problems), 1)
        self.assertIn("not a usable digital asset name", problems[0][1])

    def test_titles_we_do_not_distribute_are_not_reported(self):
        """sells_ebook off takes a product out of the audit entirely.

        That flag means the rights to hand out the file are not ours, so no
        archive of it should ever be in this image and delivery will withhold
        the order anyway (and say so, per order). Reporting its absent file as
        "missing" would mean mailing the owner a non-problem on every restart
        -- which is how the one real entry gets skimmed past.
        """
        Product.objects.filter(pk=EBOOK_PK).update(sells_ebook=False)

        self.assertEqual(
            Product.objects.filter(
                delivery_type=Product.DeliveryTypes.DIGITAL).count(),
            1,
            "fixture no longer has a DIGITAL product to check this with")
        self.assertEqual(audit_digital_assets(), [])

    def test_every_broken_product_is_listed_not_just_the_first(self):
        second = Product.objects.get(pk=EBOOK_PK)
        second.pk = None
        second.external_product_id = ""
        second.name = "Second Book"
        second.digital_asset_name = "second_book"
        second.save_base(raw=True)  # bypass Product.save()'s Stripe call
        self.archive.unlink()

        self.run_over_a_broken_catalogue()

        problems = audit_digital_assets()
        self.assertEqual(len(problems), 2)
        body = mail.outbox[0].body
        self.assertIn("2 product(s)", body)
        self.assertIn("second_book", body)
        self.assertIn(EBOOK_STEM, body)

    def test_it_exits_zero_by_default_so_startup_continues(self):
        """scripts/start-server.sh runs under `set -e`."""
        self.archive.unlink()

        # call_command re-raises SystemExit; none is expected here.
        self.run_over_a_broken_catalogue(no_email=True)

    def test_fail_opts_in_to_a_non_zero_exit(self):
        """For a caller that does want the exit code (a human, or CI)."""
        self.archive.unlink()

        with self.assertRaises(SystemExit):
            self.run_over_a_broken_catalogue(fail=True, no_email=True)

    def test_no_email_reports_without_mailing(self):
        self.archive.unlink()

        _, err, _ = self.run_over_a_broken_catalogue(no_email=True)

        self.assertIn(EBOOK_STEM, err)
        self.assertEqual(mail.outbox, [])

    def test_a_dead_mail_server_does_not_stop_the_pod_booting(self):
        """The log lines are the part startup actually depends on."""
        self.archive.unlink()

        with mock.patch(
                "main.management.commands.check_book_assets.send_mail",
                side_effect=OSError("connection refused")):
            _, _, logs = self.run_over_a_broken_catalogue()

        self.assertTrue(
            any("Could not email" in r.getMessage() for r in logs.records))

    @override_settings(ADMINS=[])
    def test_no_admins_is_itself_logged(self):
        self.archive.unlink()

        _, _, logs = self.run_over_a_broken_catalogue()

        self.assertEqual(mail.outbox, [])
        self.assertTrue(
            any("No ADMINS" in r.getMessage() for r in logs.records))


class StartServerBookAssetCheckTest(TestCase):
    """The wiring: the audit is only useful if startup actually runs it."""

    def setUp(self):
        with open(REPO_ROOT / "scripts" / "start-server.sh") as fh:
            self.script = fh.read()

    def test_the_primary_runs_the_check(self):
        primary_block = re.search(
            r'if \[ -n "\$\{PRIMARY:-\}" \];.*?\nfi\n', self.script, re.S)

        self.assertIsNotNone(primary_block, "the PRIMARY block moved")
        self.assertIn("check_book_assets", primary_block.group(0))

    def test_the_check_cannot_take_the_pod_down(self):
        """`set -e` plus a bare call would turn a missing book into an outage."""
        call = re.search(r"\./manage\.py check_book_assets.*?\n(.*\n)?",
                         self.script)

        self.assertIsNotNone(call)
        self.assertIn("||", call.group(0))
