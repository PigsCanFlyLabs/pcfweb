"""Move subscribers off django-newsletter and onto the mailing list here.

The site collected addresses through django-newsletter before this app grew
its own list, and those people are still owed the mail they signed up for.
One newsletter becomes one interest area, matched (and created if need be) by
slug, so who asked for what survives the move.

Idempotent: run it again after the next batch of signups trickles in, and
already-imported addresses are reported as unchanged rather than duplicated.
"""

import logging

from django.core.management.base import BaseCommand

from main.models import InterestArea, MailingListSubscription

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = ("Copy django-newsletter subscriptions into the mailing list, one "
            "interest area per newsletter.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the rows. Defaults to reporting what it would do.")
        parser.add_argument(
            "--interest", default=None,
            help="Put every address into this interest area (by slug) "
                 "instead of one area per newsletter.")

    def handle(self, *args, **options):
        from newsletter.models import Subscription

        apply = options["apply"]
        target = None
        if options["interest"]:
            target = InterestArea.objects.filter(
                slug=options["interest"]).first()
            if target is None:
                self.stderr.write(
                    f"No interest area with slug {options['interest']!r}.")
                return

        counts = {"created": 0, "unchanged": 0, "skipped": 0}
        areas: dict = {}
        for subscription in Subscription.objects.select_related(
                "newsletter", "user"):
            email = MailingListSubscription.normalize_email(
                subscription.email or "")
            if not email:
                counts["skipped"] += 1
                continue
            # Only people who actually confirmed, and who have not since left.
            # Importing a pending or unsubscribed row would turn "never
            # confirmed" into "on the list".
            if not subscription.subscribed or subscription.unsubscribed:
                counts["skipped"] += 1
                continue

            area = target
            if area is None:
                newsletter = subscription.newsletter
                area = areas.get(newsletter.slug)
                if area is None:
                    if apply:
                        area, _created = InterestArea.objects.get_or_create(
                            slug=newsletter.slug[:64],
                            defaults={"name": newsletter.title[:120]})
                    else:
                        area = InterestArea(slug=newsletter.slug[:64],
                                            name=newsletter.title[:120])
                    areas[newsletter.slug] = area

            if area.pk and MailingListSubscription.objects.filter(
                    email=email, interest=area).exists():
                counts["unchanged"] += 1
                continue
            if apply:
                MailingListSubscription.subscribe(
                    email=email, interest=area,
                    name=(subscription.name or "")[:200],
                    source=f"newsletter:{subscription.newsletter.slug}",
                    confirmed=True)
            counts["created"] += 1

        prefix = "Imported" if apply else "Would import"
        self.stdout.write(
            f"{prefix} {counts['created']}, {counts['unchanged']} already "
            f"here, {counts['skipped']} skipped (unconfirmed, unsubscribed "
            "or no address).")
        if not apply:
            self.stdout.write("Dry run; nothing written. Re-run with --apply.")
