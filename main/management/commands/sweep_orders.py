"""Finish the paid orders nothing else is going to come back for.

Every post-payment action -- the Stripe line-item reconciliation, the buyer's
download, the buyer's receipt, the owner's pick-and-pack email -- is
deliberately best-effort: it catches its own exception, records the failure
on the order and lets the webhook answer 200, because a non-2xx makes Stripe
redeliver for three days and an SMTP outage must not cost anybody a sale.

The retry story for those failures was Stripe's redelivery. That story only
ever held for one shape of failure. **Stripe redelivers a webhook only when
the delivery itself failed** -- a non-2xx, a timeout, a dropped connection --
so it does come back after a worker is killed mid-fulfilment (the response
never completes), and it never comes back after a graceful failure (the
response was a clean 200). The resume-what-is-incomplete logic in
``StripeWebhookView.fulfil_order`` is exactly right and, for an SMTP blip, a
slow line-item lookup or a missing book archive, had nothing left to trigger
it. The order sat PAID in the admin with a null marker until a human noticed.

This command is that trigger. Safe to run repeatedly and safe to run
concurrently with a live webhook: every write goes through the same
fulfilment lease the webhook uses, so a sweep racing a delivery loses the
race harmlessly rather than sending anything twice.

    ./manage.py sweep_orders --dry-run     # what would it do
    ./manage.py sweep_orders               # do it
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List

from django.core.management.base import BaseCommand
from django.db.models.functions import Coalesce
from django.utils import timezone

from main.models import Order
from main.views import StripeWebhookView

logger = logging.getLogger(__name__)

# How far back to look. An order that has been incomplete for a month is a
# thing for a human to look at, not something to keep retrying on every run
# forever. Overridable, and 0 means "no limit" for the run where somebody is
# repairing a long outage.
DEFAULT_WINDOW_DAYS = 30

# Ceiling on orders touched per run, so a backlog cannot turn one invocation
# into thousands of emails. The remainder is reported and picked up by the
# next run.
DEFAULT_LIMIT = 200


class Command(BaseCommand):
    help = "Retry incomplete fulfilment on paid orders."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be done without writing or emailing "
                 "anything.")
        parser.add_argument(
            "--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
            help="Only consider orders created within this many days; 0 for "
                 f"no limit (default {DEFAULT_WINDOW_DAYS}).")
        parser.add_argument(
            "--limit", type=int, default=DEFAULT_LIMIT,
            help="Ceiling on orders touched per run; 0 for no limit "
                 f"(default {DEFAULT_LIMIT}).")
        parser.add_argument(
            "--fail", action="store_true",
            help="Exit non-zero if anything was left unfinished. Off by "
                 "default so a cron entry does not page on a known-bad order.")

    def handle(self, *args: Any, **options: Any) -> None:
        self.dry_run: bool = options["dry_run"]
        self.limit: int = options["limit"]

        window_days: int = options["window_days"]
        since = None
        if window_days > 0:
            since = timezone.now() - timedelta(days=window_days)

        if self.dry_run:
            self.stdout.write("Dry run: nothing will be written or sent.\n")

        paid = self.sweep_paid(since)
        self.report(paid)

        # Orders --limit never reached count as unfinished too. They are the
        # one kind of "left undone" the run knows about without having looked
        # at them, and a --fail invocation reporting success over a backlog it
        # ran out of budget for is the exact opposite of what the flag is for.
        if options["fail"] and (paid["still_incomplete"] or paid["skipped"]):
            # SystemExit rather than CommandError: the report above is the
            # message, and CommandError would print it a second time.
            raise SystemExit(1)

    def sweep_paid(self, since) -> Dict[str, Any]:
        """Resume fulfilment on paid orders with a marker still empty."""
        counts: Dict[str, Any] = {
            "found": 0, "fulfilled": 0, "still_incomplete": 0, "skipped": 0,
            "details": [],
        }
        orders = Order.needing_fulfilment()
        if since is not None:
            # Measured from when the money arrived, not from when the cart
            # was snapshotted. A delayed payment method settles days after
            # the order is created -- the async_payment_succeeded delivery is
            # the whole reason PAID_EVENTS has two members -- so fulfilment
            # has only been outstanding since paid_at. Windowing on
            # created_at would put an order that was just paid, but placed a
            # while ago, permanently outside every sweep: stranded exactly
            # the way this command exists to prevent. Coalesce covers a row
            # somehow PAID with no paid_at; needing_fulfilment() returns only
            # PAID orders, so it should always be set.
            orders = orders.annotate(
                outstanding_since=Coalesce("paid_at", "created_at"),
            ).filter(outstanding_since__gte=since)

        # Newest first, which matters only once --limit starts biting. Oldest
        # first spends every run's budget on the same lowest ids, so a pile of
        # permanently incomplete old orders -- a download whose archive went
        # missing never clears its marker -- would starve every order placed
        # since, and with --window-days=0 would starve them forever.
        #
        # Reversing it puts the budget where it is worth most anyway: a sale
        # from ten minutes ago has a buyer waiting on it, while a month-old
        # order stuck on a missing file is already recorded in
        # digital_delivery_error and needs a human, not another retry. The old
        # tail still gets swept on the budget the (normally few) newer
        # incomplete orders leave behind.
        orders = orders.order_by("-pk")

        # Load at most this run's budget of rows, rather than walking the
        # whole queryset and counting what it would have skipped. Walking it
        # meant outstanding_fulfilment() -- which reads each order's items --
        # ran once per matching row, so an SMTP outage with a large backlog
        # turned a job capped at 200 retries into thousands of queries. The
        # rest is one COUNT, and the next run picks them up.
        #
        # So --limit bounds orders *examined*, not orders retried: a row that
        # turns out to owe nothing still spends a slot. That is the honest
        # meaning of a bound on work, and the alternative is the unbounded
        # scan this replaces.
        for order in self.take_batch(orders, counts):
            outstanding = order.outstanding_fulfilment()
            if not outstanding:
                # needing_fulfilment() is a coarser filter than the branch
                # fulfil_order() actually takes -- an order with a deleted
                # digital Product is the case in practice -- so the row is
                # rechecked here rather than claimed and then done nothing to.
                continue
            counts["details"].append(
                f"  order #{order.pk}: {', '.join(outstanding)}")
            if self.dry_run:
                counts["still_incomplete"] += 1
                continue
            logger.info(
                "Sweeping order #%s: %s.", order.pk, "; ".join(outstanding))
            StripeWebhookView().fulfil_order(order)
            order.refresh_from_db()
            if order.outstanding_fulfilment():
                counts["still_incomplete"] += 1
            else:
                counts["fulfilled"] += 1
        return counts

    def take_batch(self, orders, counts: Dict[str, Any]) -> List[Order]:
        """This run's slice of `orders`, recording what it did not reach."""
        if self.limit <= 0:
            batch = list(orders)
            counts["found"] = len(batch)
            return batch
        batch = list(orders[:self.limit])
        counts["found"] = orders.count()
        counts["skipped"] = max(0, counts["found"] - len(batch))
        return batch

    def report(self, paid: Dict[str, Any]) -> None:
        verb = "would resume" if self.dry_run else "resumed"
        self.stdout.write(
            f"Paid orders with unfinished fulfilment: {paid['found']} "
            f"found, {verb} {len(paid['details'])}.")
        for line in paid["details"]:
            self.stdout.write(line)

        if not self.dry_run and paid["still_incomplete"]:
            self.stdout.write(self.style.WARNING(
                f"  {paid['still_incomplete']} still incomplete after the "
                "retry -- see notification_error / digital_delivery_error / "
                "receipt_error on those rows."))

        if paid["skipped"]:
            self.stdout.write(self.style.WARNING(
                f"Stopped at this run's --limit with {paid['skipped']} "
                "order(s) unexamined; the next run picks them up."))

        if not paid["details"] and not paid["skipped"]:
            self.stdout.write(self.style.SUCCESS(
                "Nothing outstanding: every paid order is finished."))
