"""Tests for ``manage.py ensure_admin_account``.

The command is the only thing standing between "cluster freshly bootstrapped"
and "nobody can log into /timbit/admin/", and it handles credentials, so the
edges get pinned hard: unset must stay a clean skip, half-set must fail
loudly, an existing customer account must never be promoted, and the password
must never appear in the output.
"""

import os
import re
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

USERNAME = "shutterbug"
PASSWORD = "a-long-vaulted-password"
EMAIL = "admin@example.com"

FULL_ENV = {
    "DJANGO_SUPERUSER_USERNAME": USERNAME,
    "DJANGO_SUPERUSER_PASSWORD": PASSWORD,
    "DJANGO_SUPERUSER_EMAIL": EMAIL,
}


def run_command():
    out = StringIO()
    err = StringIO()
    call_command("ensure_admin_account", stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def env(**overrides):
    """A patched process environment holding exactly these admin variables."""
    cleaned = {name: "" for name in FULL_ENV}
    cleaned.update(overrides)
    return mock.patch.dict(os.environ, cleaned)


class EnsureAdminAccountCreateTest(TestCase):
    def test_creates_a_superuser_when_fully_configured(self):
        with env(**FULL_ENV):
            out, _ = run_command()

        self.assertIn("Created admin account", out)
        user = User.objects.get(username=USERNAME)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(user.email, EMAIL)

    def test_email_is_optional_at_creation(self):
        with env(DJANGO_SUPERUSER_USERNAME=USERNAME,
                 DJANGO_SUPERUSER_PASSWORD=PASSWORD):
            run_command()

        self.assertEqual(User.objects.get(username=USERNAME).email, "")

    def test_values_are_stripped_before_use(self):
        # The classic Secret mangling: a trailing newline riding in on the
        # value. Logging in types the password without it, so the stored
        # credential has to be the stripped one.
        with env(DJANGO_SUPERUSER_USERNAME=f" {USERNAME}\n",
                 DJANGO_SUPERUSER_PASSWORD=f"{PASSWORD}\n"):
            run_command()

        user = User.objects.get(username=USERNAME)
        self.assertTrue(user.check_password(PASSWORD))


class EnsureAdminAccountSkipTest(TestCase):
    def test_skips_cleanly_when_unconfigured(self):
        with env():
            out, _ = run_command()

        self.assertIn("not managing an admin account", out)
        self.assertEqual(User.objects.count(), 0)

    def test_empty_strings_count_as_unset(self):
        # cluster-setup.yaml templates the Secret keys with default(''), so
        # before the vault entries exist the pod sees empty strings, not
        # absent variables. That must stay a skip, not become an error.
        with env(DJANGO_SUPERUSER_USERNAME="  ",
                 DJANGO_SUPERUSER_PASSWORD=""):
            out, _ = run_command()

        self.assertIn("not managing an admin account", out)
        self.assertEqual(User.objects.count(), 0)

    def test_email_alone_provisions_nothing(self):
        with env(DJANGO_SUPERUSER_EMAIL=EMAIL):
            out, _ = run_command()

        self.assertIn("not managing an admin account", out)
        self.assertEqual(User.objects.count(), 0)


class EnsureAdminAccountHalfSetTest(TestCase):
    """One variable without the other is a typo'd vault key, not a request
    to skip."""

    def test_password_without_username_fails_naming_the_username(self):
        with env(DJANGO_SUPERUSER_PASSWORD=PASSWORD):
            with self.assertRaisesMessage(
                    CommandError, "DJANGO_SUPERUSER_USERNAME"):
                run_command()
        self.assertEqual(User.objects.count(), 0)

    def test_username_without_password_fails_naming_the_password(self):
        with env(DJANGO_SUPERUSER_USERNAME=USERNAME):
            with self.assertRaisesMessage(
                    CommandError, "DJANGO_SUPERUSER_PASSWORD"):
                run_command()
        self.assertEqual(User.objects.count(), 0)

    def test_the_error_points_at_the_vault(self):
        # The fix lives in colo-scripts, not in this repo; the message has to
        # say so or the operator greps the wrong codebase.
        with env(DJANGO_SUPERUSER_USERNAME=USERNAME):
            with self.assertRaisesMessage(CommandError, "vault"):
                run_command()


class EnsureAdminAccountConvergeTest(TestCase):
    def setUp(self):
        with env(**FULL_ENV):
            run_command()
        self.user = User.objects.get(username=USERNAME)

    def test_rerun_writes_nothing(self):
        # The password hash embeds a fresh salt on every set_password, so an
        # unchanged hash proves the no-drift boot skipped the write entirely.
        hash_before = self.user.password

        with env(**FULL_ENV):
            out, _ = run_command()

        self.assertIn("already up to date", out)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, hash_before)

    def test_a_rotated_password_is_applied(self):
        with env(**dict(FULL_ENV, DJANGO_SUPERUSER_PASSWORD="rotated-pw")):
            out, _ = run_command()

        self.assertIn("password rotated", out)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("rotated-pw"))
        self.assertFalse(self.user.check_password(PASSWORD))

    def test_a_dropped_staff_flag_is_restored(self):
        # A superuser without is_staff cannot log into the admin at all;
        # nothing legitimate leaves the pair split.
        User.objects.filter(pk=self.user.pk).update(is_staff=False)

        with env(**FULL_ENV):
            out, _ = run_command()

        self.assertIn("is_staff restored", out)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)

    def test_a_changed_env_email_is_applied(self):
        with env(**dict(FULL_ENV, DJANGO_SUPERUSER_EMAIL="new@example.com")):
            run_command()

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new@example.com")

    def test_an_admin_edited_email_survives_an_unset_env_email(self):
        # With the variable unset the address is admin-owned -- same
        # ownership split seed_products draws for product fields. Blanking
        # it would also break password reset for this account.
        User.objects.filter(pk=self.user.pk).update(
            email="edited@example.com")

        with env(DJANGO_SUPERUSER_USERNAME=USERNAME,
                 DJANGO_SUPERUSER_PASSWORD=PASSWORD):
            out, _ = run_command()

        self.assertIn("already up to date", out)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "edited@example.com")


class EnsureAdminAccountRefusalTest(TestCase):
    def test_an_existing_customer_account_is_never_promoted(self):
        # auth.User is also the customer table. If the configured username
        # already belongs to an ordinary signup, converging would hand that
        # customer superuser and overwrite their password.
        customer = User.objects.create_user(
            username=USERNAME, email="theirs@example.com",
            password="their-own-password")

        with env(**FULL_ENV):
            with self.assertRaisesMessage(CommandError, "promote"):
                run_command()

        customer.refresh_from_db()
        self.assertFalse(customer.is_superuser)
        self.assertFalse(customer.is_staff)
        self.assertTrue(customer.check_password("their-own-password"))

    def test_a_deactivated_superuser_stays_locked_out(self):
        # is_active=False is the admin's lockout switch; re-enabling it on
        # every deploy would make the lockout undoable while the Secret
        # exists.
        User.objects.create_superuser(
            username=USERNAME, email=EMAIL, password="old-password")
        User.objects.filter(username=USERNAME).update(is_active=False)

        with env(**FULL_ENV):
            out, err = run_command()

        self.assertIn("deactivated", err)
        user = User.objects.get(username=USERNAME)
        self.assertFalse(user.is_active)
        # Untouched means untouched: the rotated password was not applied to
        # a locked account either.
        self.assertTrue(user.check_password("old-password"))


class EnsureAdminAccountSecrecyTest(TestCase):
    def test_the_password_never_reaches_the_output(self):
        with env(**FULL_ENV):
            first_out, first_err = run_command()
        # The converge path talks about the password; it must not quote it.
        with env(**dict(FULL_ENV, DJANGO_SUPERUSER_PASSWORD="rotated-pw")):
            second_out, second_err = run_command()

        for text in (first_out, first_err, second_out, second_err):
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn("rotated-pw", text)


class StartServerAdminAccountTest(TestCase):
    """The wiring: the account only exists if startup actually runs this."""

    def setUp(self):
        with open(REPO_ROOT / "scripts" / "start-server.sh") as fh:
            self.script = fh.read()

    def test_the_primary_runs_the_command(self):
        primary_block = re.search(
            r'if \[ -n "\$\{PRIMARY:-\}" \];.*?\nfi\n', self.script, re.S)

        self.assertIsNotNone(primary_block, "the PRIMARY block moved")
        self.assertIn("ensure_admin_account", primary_block.group(0))

    def test_the_command_cannot_take_the_pod_down(self):
        """`set -e` plus a bare call would turn a half-set Secret into an
        outage; the command's own exit code and stderr are the alarm."""
        call = re.search(
            r"\./manage\.py ensure_admin_account.*?\n(.*\n)?", self.script)

        self.assertIsNotNone(call)
        self.assertIn("||", call.group(0))

    def test_the_command_runs_before_the_email_identity_backfill(self):
        # So the backfill claims the admin's EmailIdentity row in the same
        # boot instead of the next one. Match the invocations, not the bare
        # names -- both are also discussed in comments.
        self.assertLess(
            self.script.index("./manage.py ensure_admin_account"),
            self.script.index("./manage.py backfill_email_identities"))
