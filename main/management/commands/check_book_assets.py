"""Audit, on primary startup, that every sellable e-book has its archive.

The failure this exists to catch: a product is marked DIGITAL with
``sells_ebook`` set -- so the store will happily sell it and the Stripe
webhook will try to deliver it -- but ``<digital_asset_name>.zip`` is not
under ``settings.BOOK_ASSET_ROOT`` in the running image. Until now the first
thing to notice that was ``Order.deliver_digital_goods()``, i.e. a customer
who had already paid and got nothing. That is precisely the wrong moment:
the fix is to add the file to the pcfweb-book-assets repository and rebuild
the image, which takes a deploy, while the buyer waits.

``scripts/check-book-assets.sh`` already refuses to *build* an image whose
archives are LFS pointers or truncated, but it can only look at the files it
is handed -- it has no database, so it cannot know that product 106 expects
``distributed_computing_4_kids.zip`` and that nobody ever added it. This
check is the other half: it reads the catalogue and asks, for each thing we
sell as a download, whether the file is actually there. Hence running it at
startup on the primary (scripts/start-server.sh), right after
``seed_products`` has brought the catalogue up to date.

Reported twice, deliberately:

  * ``logger.error`` per broken product, so it is in the pod logs where
    anyone debugging a delivery complaint will find it, and
  * one mail to ADMINS, because a log line in a pod nobody is tailing is not
    a notification. This is a "you cannot sell this until you deploy again"
    problem, so it has to reach a person.

The mail repeats on every primary restart while the archive stays missing.
That is the intended trade: the primary is a single replica that restarts
rarely, and a nag that stops nagging is how this ends up ignored.
"""

from __future__ import annotations

import logging

from typing import Any, List, Tuple

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from main.digital import DigitalAssetError, asset_root, open_asset
from main.models import Product
from main.utils import admin_recipients

logger = logging.getLogger(__name__)

SUBJECT = "[pcfweb] Missing e-book file(s): downloads cannot be delivered"

# What to do about it, said once at the bottom of the mail rather than per
# product. The archives live in a separate Git LFS repository and are baked
# into the image at build time, so there is no way to fix this from the admin.
REMEDY = """\
These files are baked into the container image at build time from the
pcfweb-book-assets repository, so this cannot be fixed from the Django admin:

  1. add or restore <name>.zip (EPUB + PDF, no DRM) in pcfweb-book-assets,
     commit it through Git LFS and push;
  2. re-run ./build.sh, which validates every archive and bakes them into a
     new image, then deploys it.

Until that is done, anyone buying one of the products above pays and gets
nothing: the delivery failure is recorded on the order and the customer has
to be sent the file by hand or refunded. If a product should not be on sale
as a download at all, clear its "sells ebook" flag in the admin instead."""


def audit_digital_assets() -> List[Tuple[Product, str]]:
    """Every digitally-fulfilled product whose archive cannot be served.

    Scoped to ``is_digitally_fulfilled()`` -- DIGITAL *and* sells_ebook --
    because that is exactly the set the webhook will try to deliver. The
    O'Reilly titles are DIGITAL with sells_ebook unset (we do not hold the
    distribution rights), carry no asset name and never will, so including
    them would mean mailing the owner a list of non-problems on every
    restart, which is how a real one gets skimmed past.

    Returns (product, problem) pairs, ordered by pk for a stable report.
    """
    problems: List[Tuple[Product, str]] = []
    for product in Product.objects.filter(
            delivery_type=Product.DeliveryTypes.DIGITAL,
            sells_ebook=True).order_by("pk"):
        if not product.digital_asset_name:
            # Distinct from the DigitalAssetError below, which would call the
            # empty string an invalid name: nothing was ever typed in, so the
            # fix is an admin field and not a missing file.
            problems.append((
                product,
                "no digital asset name is set on the product, so there is no "
                "archive to look for"))
            continue
        try:
            # open() rather than exists(): the same call delivery makes, so
            # an unreadable-but-present file is caught here too.
            open_asset(product.digital_asset_name).close()
        except DigitalAssetError as e:
            problems.append((product, str(e)))
    return problems


def report_body(problems: List[Tuple[Product, str]]) -> str:
    lines = [
        f"{len(problems)} product(s) are on sale as downloads but their book "
        "file is missing from this image.",
        "",
        f"Looked in: {asset_root()}",
        f"Site: {getattr(settings, 'SITE_BASE_URL', '(unset)')}",
        "",
    ]
    for product, problem in problems:
        lines += [
            f"* {product.name} (product #{product.pk}, "
            f"asset name {product.digital_asset_name or '(unset)'!r})",
            f"    {problem}",
        ]
    lines += ["", REMEDY]
    return "\n".join(lines)


class Command(BaseCommand):
    help = (
        "Check that every product sold as a download has its book archive "
        "present under BOOK_ASSET_ROOT; log and email ADMINS about any that "
        "do not. Run on primary startup.")

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--no-email", action="store_true",
            help="Report to the log and stdout only; do not mail ADMINS.")
        parser.add_argument(
            "--fail", action="store_true",
            help=("Exit non-zero when something is missing. Off by default "
                  "because startup calls this: a missing archive must not "
                  "stop the site from serving everything else."))

    def handle(self, **options: Any) -> None:
        problems = audit_digital_assets()
        if not problems:
            self.stdout.write(self.style.SUCCESS(
                "Every product sold as a download has its book file."))
            return

        for product, problem in problems:
            logger.error(
                "Product #%s %r is sold as a download but cannot be "
                "delivered: %s", product.pk, product.name, problem)
        body = report_body(problems)
        self.stderr.write(self.style.ERROR(body))

        if not options.get("no_email"):
            self._email(body)
        if options.get("fail"):
            # SystemExit rather than CommandError: the report above is the
            # message, and CommandError would print it a second time.
            raise SystemExit(1)

    def _email(self, body: str) -> None:
        """Mail the owner, and never let that failure be the loud one.

        Called from pod startup, so an unreachable SMTP server must not stop
        the primary from booting -- the log lines above have already been
        emitted and are what the deploy actually depends on.
        """
        recipients = admin_recipients()
        if not recipients:
            logger.error(
                "No ADMINS configured, so nobody was mailed about the missing "
                "book file(s). Set the ORDER_NOTIFICATION_EMAIL env var.")
            return
        try:
            send_mail(
                SUBJECT,
                body,
                settings.DEFAULT_FROM_EMAIL,
                recipients,
                fail_silently=False,
            )
        except Exception:
            logger.exception(
                "Could not email %s about the missing book file(s).",
                ", ".join(recipients))
            return
        self.stdout.write(f"Emailed {', '.join(recipients)}.")
