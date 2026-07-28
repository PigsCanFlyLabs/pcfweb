"""Backfill EmailIdentity rows for existing accounts.

The initial signup-race rollout can leave a narrow residue under RollingUpdate:
an old web replica may create a working auth.User row after the migration's
one-time backfill has already run, so that account has no EmailIdentity row.
Running this command on every primary startup closes that window permanently at
zero downtime.

Deliberately not fatal on row-level data problems. Historic duplicate emails
already exist in auth_user, and this cleanup is best-effort: one bad address
must not stop the primary pod from booting under set -euo pipefail. Each such
row is logged and skipped so later users are still processed.
"""

from __future__ import annotations

import logging

from typing import Any

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import IntegrityError

from main.models import EmailIdentity, normalize_email_identity

logger = logging.getLogger(__name__)


def backfill_email_identities() -> dict[str, int]:
    counts = {
        "created": 0,
        "claimed": 0,
        "duplicates": 0,
        "errors": 0,
    }
    for identityless_user in (User.objects
                              .filter(email_identity__isnull=True)
                              .exclude(email="")
                              .order_by("pk")
                              .iterator()):
        normalized = normalize_email_identity(identityless_user.email)
        if not normalized:
            continue
        try:
            identity, created = EmailIdentity.objects.get_or_create(
                normalized_email=normalized,
                defaults={"user": identityless_user})
        except IntegrityError:
            logger.warning(
                "Could not reserve %r for user #%s during backfill: another "
                "row with that normalized email already exists.",
                normalized, identityless_user.pk)
            counts["duplicates"] += 1
            continue
        except Exception:
            logger.exception(
                "Could not backfill EmailIdentity for user #%s.",
                identityless_user.pk)
            counts["errors"] += 1
            continue

        if created:
            counts["created"] += 1
            continue
        if identity.user_id is None:
            claimed = EmailIdentity.objects.filter(
                pk=identity.pk, user__isnull=True).update(user=identityless_user)
            if claimed:
                counts["claimed"] += 1
                continue
            identity.refresh_from_db(fields=["user"])
        if identity.user_id != identityless_user.pk:
            logger.warning(
                "Left user #%s without an EmailIdentity: %r is already "
                "reserved by user #%s.",
                identityless_user.pk, normalized, identity.user_id)
            counts["duplicates"] += 1
    return counts


class Command(BaseCommand):
    help = (
        "Backfill EmailIdentity rows for existing auth users that do not "
        "have one. Best-effort: duplicate-address residue is logged and "
        "skipped rather than aborting startup.")

    def handle(self, *args: Any, **options: Any) -> None:
        counts = backfill_email_identities()
        self.stdout.write(
            "Backfilled EmailIdentity rows: "
            f"created={counts['created']} "
            f"claimed={counts['claimed']} "
            f"duplicates={counts['duplicates']} "
            f"errors={counts['errors']}")
