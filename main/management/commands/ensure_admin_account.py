"""Create -- or converge -- the Django admin account from the environment.

Nothing else provisions one. ``migrate`` builds the auth tables empty, and on
a fresh database that used to mean the admin at /timbit/admin/ had no account
that could log in until somebody ran ``createsuperuser`` by hand inside a pod.
The primary runs this on every boot (scripts/start-server.sh) so the cluster
comes up administrable, with the credentials living next to every other
production secret instead of in somebody's shell history.

The variables are ``DJANGO_SUPERUSER_USERNAME``, ``DJANGO_SUPERUSER_PASSWORD``
and (optional) ``DJANGO_SUPERUSER_EMAIL`` -- deliberately the exact names
Django's own ``createsuperuser --noinput`` reads, so the stock command works
against the same Secret if this one is ever bypassed. In the cluster they
arrive through ``pcfweb-secret``, provisioned out of the colo-scripts vault
(playbooks/cluster-setup.yaml) from ``PCF_ADMIN_USER`` / ``PCF_ADMIN_PASSWORD``
/ ``PCF_ADMIN_EMAIL``.

Converge, not just create-once, because the Secret is the source of truth:
rotating the password in the vault and rolling the primary must actually
change the login, not silently keep honoring the leaked one. Every write is
guarded by a comparison first, so a boot where nothing drifted writes nothing.

The edges are where the care went, in both directions:

* **Unset is not an error.** Local dev has no reason to set these, and the
  cluster templates them with ``default('')`` so the playbook runs before the
  vault entries exist. Missing or empty means one line of log and exit 0.
* **Half-set is an error.** A username with no password (or the reverse) is a
  typo'd vault key or a mangled Secret, and the operator believes an admin
  exists. That must be a failure naming the missing variable, not a skip.
  start-server.sh deliberately keeps it non-fatal to the pod -- an admin
  account nobody can log into is bad, a store that stopped selling books over
  it is worse -- so this command's exit code and stderr are the alarm.
* **An existing non-superuser account is never promoted.** ``auth.User`` is
  also the customer table. If the configured username already belongs to an
  ordinary signup, converging it would hand that customer superuser and
  overwrite their password. Refuse loudly instead; pick another username or
  promote deliberately in a shell.
* **A deactivated superuser stays deactivated.** ``is_active = False`` is how
  an account is locked out in the admin. Re-enabling it on every deploy would
  make that lockout undoable while the Secret exists, so a warning is printed
  and nothing is written; clearing the env or reactivating in the admin are
  both explicit acts.

Values are stripped before use, the password included: a trailing newline
smuggled in by secret plumbing (``echo`` piped into ``kubectl create secret``
is the classic) is far more likely than a deliberately whitespace-padded
password, and the resulting "right password rejected" is miserable to debug.

Ordering: start-server.sh runs this after ``migrate`` (it needs the auth
tables) and before ``backfill_email_identities``, so the backfill claims the
admin's EmailIdentity row in the same boot instead of the next one.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

USERNAME_VAR = "DJANGO_SUPERUSER_USERNAME"
PASSWORD_VAR = "DJANGO_SUPERUSER_PASSWORD"
EMAIL_VAR = "DJANGO_SUPERUSER_EMAIL"


def _env(name: str) -> Optional[str]:
    """The variable's stripped value, with empty collapsed to None.

    Empty has to mean unset: cluster-setup.yaml templates these keys with
    ``default('')`` so the Secret can be applied before the vault entries
    exist, and that ships literal empty strings into the pod environment.
    """
    value = os.environ.get(name, "").strip()
    return value or None


class Command(BaseCommand):
    help = (
        "Create the admin account from DJANGO_SUPERUSER_USERNAME / "
        "DJANGO_SUPERUSER_PASSWORD / DJANGO_SUPERUSER_EMAIL, or converge an "
        "existing one (rotated password, drifted flags or email). Skips "
        "cleanly when the variables are absent; fails when only half of "
        "them are."
    )

    def handle(self, **options: Any) -> None:
        username = _env(USERNAME_VAR)
        password = _env(PASSWORD_VAR)
        email = _env(EMAIL_VAR)

        if username is None and password is None:
            # Local dev, or the cluster before the vault entries exist. The
            # email variable alone provisions nothing, so it does not rescue
            # this branch into an error.
            self.stdout.write(
                f"{USERNAME_VAR} and {PASSWORD_VAR} are unset; "
                "not managing an admin account."
            )
            return

        if username is None or password is None:
            missing = [
                name
                for name, value in ((USERNAME_VAR, username),
                                    (PASSWORD_VAR, password))
                if value is None
            ]
            raise CommandError(
                f"{', '.join(missing)} is unset while the other admin "
                "variable is set, so the admin account cannot be managed. "
                "In the cluster both come from the pcfweb-secret Secret, "
                "provisioned by colo-scripts playbooks/cluster-setup.yaml "
                "out of the vault -- a half-set pair usually means a typo'd "
                "vault key."
            )

        # Only the primary invokes this (one replica), so there is no
        # concurrent writer to race; the transaction just keeps the
        # read-compare-write from ever being observable half-applied.
        with transaction.atomic():
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                # create_superuser hashes the password and sets is_staff,
                # is_superuser and is_active -- the account is born converged.
                User.objects.create_superuser(
                    username=username, email=email or "", password=password)
                self.stdout.write(self.style.SUCCESS(
                    f"Created admin account {username!r}."))
                return

            if not user.is_superuser:
                raise CommandError(
                    f"A non-superuser account named {username!r} already "
                    "exists -- likely an ordinary signup, since auth.User is "
                    "also the customer table. Refusing to promote it and "
                    "overwrite its password. Pick a different "
                    f"{USERNAME_VAR}, or promote this account deliberately "
                    "in a shell if it really is yours."
                )

            if not user.is_active:
                # Deactivation is the admin's lockout switch. Flipping it
                # back on every deploy would make locking this account out
                # impossible for as long as the Secret exists.
                self.stderr.write(
                    f"Admin account {username!r} is deactivated; leaving it "
                    "untouched. Reactivate it in the admin, or unset "
                    f"{USERNAME_VAR}/{PASSWORD_VAR} to silence this."
                )
                return

            changed: List[str] = []
            update_fields: List[str] = []

            # check_password instead of an unconditional set_password: the
            # usual boot rotates nothing, and rewriting the hash anyway would
            # burn a PBKDF2 derivation and dirty the row just to store a new
            # salt. The comparison costs the same derivation but writes
            # nothing -- and never logs which way it went beyond "rotated".
            if not user.check_password(password):
                user.set_password(password)
                changed.append("password rotated")
                update_fields.append("password")

            if not user.is_staff:
                # A superuser without is_staff cannot log into the admin at
                # all -- nothing legitimate leaves the pair split, so this is
                # drift, not a decision to preserve.
                user.is_staff = True
                changed.append("is_staff restored")
                update_fields.append("is_staff")

            # Only an explicitly configured email converges. With the
            # variable unset, an address edited in the admin is theirs to
            # keep -- blanking it would also break password reset for the
            # one account that most needs it.
            if email is not None and user.email != email:
                user.email = email
                changed.append("email updated")
                update_fields.append("email")

            if not changed:
                self.stdout.write(
                    f"Admin account {username!r} already up to date.")
                return

            user.save(update_fields=update_fields)
            self.stdout.write(self.style.SUCCESS(
                f"Admin account {username!r} converged: "
                f"{', '.join(changed)}."))
