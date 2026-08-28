"""Finish the orders nothing else is going to come back for.

Every post-payment action -- the Stripe line-item reconciliation, the buyer's
download, the buyer's receipt, the owner's pick-and-pack email -- is
deliberately best-effort: it catches its own exceptions, records the failure
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

This command is that trigger, and the other half of it: orders still PENDING
because no delivery ever arrived at all. A wrong or rotated
``STRIPE_WEBHOOK_SECRET``, an endpoint missing from the Stripe Dashboard, a
pod clock more than five minutes off, an ingress eating the POST -- all of
them look the same from in here, which is nothing happening. For each such
order it asks Stripe what really became of the session and acts on the
answer, so a webhook outage becomes a delay rather than a lost sale.

Safe to run repeatedly and safe to run concurrently with a live webhook:
every write goes through the same guarded transition and the same fulfilment
lease the webhook uses, so a sweep racing a delivery loses the race
harmlessly rather than sending anything twice.

    ./manage.py sweep_orders --dry-run     # what would it do
    ./manage.py sweep_orders               # do it
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Optional

import stripe
from django.core.management.base import BaseCommand
from django.utils import timezone

from main.models import Order
from main.utils import email_admins
from main.views import StripeWebhookView

logger = logging.getLogger(__name__)

# How long an order is left alone before the sweeper will ask Stripe about it.
# A buyer can legitimately sit on Stripe's hosted page for a while, and the
# webhook usually lands within seconds of them paying, so this only has to be
# longer than "still checking out" -- not longer than any plausible webhook
# delay, because asking Stripe about an order that is genuinely still open is
# a read that changes nothing.
DEFAULT_PENDING_AFTER_MINUTES = 30

# How far back to look. Stripe Checkout sessions expire after 24 hours, so a
# PENDING order older than this window is settled history; a PAID one that has
# been incomplete for a month is a thing for a human to look at, not something
# to keep retrying on every run forever. Both are overridable, and 0 means "no
# limit" for the run where somebody is repairing a long outage.
DEFAULT_WINDOW_DAYS = 30

# Ceiling on Stripe session lookups per run, so a backlog cannot turn one
# invocation into thousands of API calls. The remainder is reported and picked
# up by the next run.
DEFAULT_LIMIT = 200

ALERT_SUBJECT = "[pcfweb] Orders were paid on Stripe with no webhook recorded"

# Sent once per run, not once per order. An endpoint that has stopped working
# strands every order at once, so a per-order mail would bury the one sentence
# that matters under a mailbox full of the same sentence.
ALERT_BODY = """\
The order sweep found {count} order(s) that Stripe says were paid but that no
webhook ever recorded. It has now recorded and fulfilled them, so nothing is
lost -- but until the endpoint is fixed every new sale waits for the next
hourly sweep instead of being handled in seconds, and the buyer's download
waits with it.

Orders: {orders}

This means deliveries are not reaching POST /stripe/webhook, or are being
rejected when they do. In the Stripe Dashboard, under Developers -> Webhooks,
check the endpoint for https://www.pigscanfly.ca/stripe/webhook:

  * Is it still registered, and subscribed to checkout.session.completed and
    checkout.session.async_payment_succeeded?
  * What does its recent delivery log say -- nothing at all, or failures?
  * If failures: the pod logs record which of the three rejection causes it
    was. A signature mismatch means STRIPE_WEBHOOK_SECRET does not match this
    endpoint's signing secret (a rotation, or a test/live mix-up). A timestamp
    outside the tolerance zone means the pod clock has drifted.
"""


class Command(BaseCommand):
    help = ("Retry incomplete fulfilment on paid orders, and settle pending "
            "orders whose Stripe webhook never arrived.")

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be done without writing or emailing "
                 "anything. Still reads from Stripe.")
        parser.add_argument(
            "--pending-after-minutes", type=int,
            default=DEFAULT_PENDING_AFTER_MINUTES,
            help="Leave pending orders younger than this alone "
                 f"(default {DEFAULT_PENDING_AFTER_MINUTES}).")
        parser.add_argument(
            "--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
            help="Only consider orders created within this many days; 0 for "
                 f"no limit (default {DEFAULT_WINDOW_DAYS}).")
        parser.add_argument(
            "--limit", type=int, default=DEFAULT_LIMIT,
            help="Ceiling on orders touched per phase; 0 for no limit "
                 f"(default {DEFAULT_LIMIT}).")
        parser.add_argument(
            "--fail", action="store_true",
            help="Exit non-zero if anything was left unfinished. Off by "
                 "default so a cron entry does not page on a known-bad order.")

    def handle(self, *args: Any, **options: Any) -> None:
        self.dry_run: bool = options["dry_run"]
        window_days: int = options["window_days"]
        self.limit: int = options["limit"]

        since = None
        if window_days > 0:
            since = timezone.now() - timedelta(days=window_days)

        if self.dry_run:
            self.stdout.write("Dry run: nothing will be written or sent.\n")

        # Pending first, and on its own budget. An unrecorded payment is the
        # more urgent of the two -- a buyer has paid and nothing at all has
        # happened -- so a long backlog of half-finished paid orders must not
        # be able to spend the whole run before this half is reached. An
        # order paid here is fulfilled inline, so it needs nothing from the
        # phase below.
        pending = self.sweep_pending(
            since, options["pending_after_minutes"])
        paid = self.sweep_paid(since)

        self.report(paid, pending)

        if pending["paid"] and not self.dry_run:
            self.alert(pending)

        # Orders --limit never reached count as unfinished too. They are the
        # one kind of "left undone" the run knows about without having looked
        # at them, and a --fail invocation reporting success over a backlog it
        # ran out of budget for is the exact opposite of what the flag is for.
        left = (paid["still_incomplete"] or pending["stuck"]
                or paid["skipped"] or pending["skipped"])
        if options["fail"] and left:
            raise SystemExit(1)

    def alert(self, pending: Dict[str, Any]) -> None:
        """Mail the owner that the webhook is not doing its job.

        Only for orders Stripe had already been paid for and nothing here
        knew about. Every other thing this command repairs is a delayed
        email; this one is the payment pipeline itself being down, and it is
        invisible from the site -- checkout still works, the buyer still pays,
        and nothing happens afterwards.

        Best-effort like every other alert here: the ERROR lines are already
        in the log, and a dead SMTP server must not be what stops the sweep
        reporting its result.
        """
        body = ALERT_BODY.format(
            count=pending["paid"],
            orders=", ".join(f"#{pk}" for pk in pending["paid_order_pks"]))
        if email_admins(
                ALERT_SUBJECT, body, logger,
                "about orders paid on Stripe with no webhook recorded. Set "
                "the ORDER_NOTIFICATION_EMAIL env var."):
            self.stdout.write("Emailed ADMINS about the missing webhooks.")

    # ---- paid orders that never finished ----

    def sweep_paid(self, since) -> Dict[str, Any]:
        """Resume fulfilment on paid orders with a marker still empty."""
        self.start_phase()
        counts: Dict[str, Any] = {
            "found": 0, "fulfilled": 0, "still_incomplete": 0, "skipped": 0,
            "details": [],
        }
        orders = Order.needing_fulfilment()
        if since is not None:
            orders = orders.filter(created_at__gte=since)

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
        for order in orders.order_by("-pk"):
            counts["found"] += 1
            outstanding = order.outstanding_fulfilment()
            if not outstanding:
                # needing_fulfilment() is a coarser filter than the branch
                # fulfil_order() actually takes -- an order with a deleted
                # digital Product is the case in practice -- so the row is
                # rechecked here rather than claimed and then done nothing to.
                continue
            if not self.take_slot():
                counts["skipped"] += 1
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

    # ---- pending orders no webhook ever arrived for ----

    # What Stripe says became of a Checkout session. "open" means the buyer
    # could still pay it, so it is left alone.
    EXPIRED_SESSION_STATUS = "expired"

    def sweep_pending(self, since, after_minutes: int) -> Dict[str, Any]:
        """Ask Stripe about pending orders the webhook never reported on."""
        self.start_phase()
        counts: Dict[str, Any] = {
            "found": 0, "paid": 0, "expired": 0, "open": 0, "stuck": 0,
            "skipped": 0, "details": [], "paid_order_pks": [],
        }
        cutoff = timezone.now() - timedelta(minutes=after_minutes)
        orders = Order.objects.filter(
            status=Order.Status.PENDING,
            created_at__lt=cutoff,
        ).exclude(stripe_session_id__isnull=True).exclude(
            stripe_session_id="")
        if since is not None:
            orders = orders.filter(created_at__gte=since)

        webhook = StripeWebhookView()
        # Newest first, for the reason sweep_paid() is: a session that cannot
        # be read at all stays pending forever, and an oldest-first capped run
        # would let a handful of those hide every unrecorded payment since.
        for order in orders.order_by("-pk"):
            counts["found"] += 1
            session_id = order.stripe_session_id
            if not session_id:
                # Excluded by the query above; re-read here so the narrowing
                # is local to the use, and so a later edit to that filter
                # cannot quietly start asking Stripe about None.
                continue
            if not self.take_slot():
                counts["skipped"] += 1
                continue
            try:
                session = stripe.checkout.Session.retrieve(session_id)
            except Exception as e:
                # Never fatal: one unreadable session must not stop the sweep
                # reaching the rest. Recorded loudly because a *run* of these
                # is a Stripe credential problem, not an order problem.
                logger.warning(
                    "Sweep could not retrieve Stripe session %s for order "
                    "#%s: %s", session_id, order.pk, e)
                counts["stuck"] += 1
                counts["details"].append(
                    f"  order #{order.pk}: Stripe lookup failed ({e})")
                continue

            # paid_fields() writes stripe_session_id, and it is only ever safe
            # to do that because the caller has established the order is bound
            # to this very session -- the webhook gets that from find_order().
            # Here the lookup key *is* the order's own session id, so this can
            # only trip if Stripe answered with something other than what was
            # asked for. Refuse rather than re-point a real order.
            returned_id = session.get("id")
            if returned_id and returned_id != order.stripe_session_id:
                logger.error(
                    "Refusing Stripe session %s for order #%s: that order is "
                    "bound to session %s. Not marking it paid.",
                    returned_id, order.pk, order.stripe_session_id)
                counts["stuck"] += 1
                counts["details"].append(
                    f"  order #{order.pk}: Stripe returned session "
                    f"{returned_id}, not the one asked for")
                continue

            payment_status = session.get("payment_status")
            if payment_status in webhook.PAID_PAYMENT_STATUSES:
                counts["paid"] += 1
                counts["paid_order_pks"].append(order.pk)
                counts["details"].append(
                    f"  order #{order.pk}: Stripe says {payment_status!r} -- "
                    "paying and fulfilling it")
                if self.dry_run:
                    continue
                # The loudest line this command writes. An order reaching here
                # means a real payment went unrecorded until now, which is a
                # webhook problem worth going and looking at rather than a
                # thing the sweep quietly cleaned up.
                logger.error(
                    "Order #%s was paid on Stripe (%s) but no webhook ever "
                    "recorded it; the sweep is paying and fulfilling it now. "
                    "Check the Stripe Dashboard's webhook delivery log.",
                    order.pk, payment_status)
                webhook.pay_and_fulfil(order, session, source="order sweep")
                continue

            if session.get("status") == self.EXPIRED_SESSION_STATUS:
                counts["expired"] += 1
                counts["details"].append(
                    f"  order #{order.pk}: session expired unpaid -- "
                    "cancelling it")
                if self.dry_run:
                    continue
                webhook.handle_cancelled(
                    session, "the checkout session expired (found by sweep)")
                continue

            # Still open, or completed with a delayed payment that has not
            # settled. Both resolve on their own; neither is stuck.
            counts["open"] += 1
        return counts

    # ---- bookkeeping ----

    def start_phase(self) -> None:
        """Give this phase its own --limit, independent of the other's."""
        self.remaining: Optional[int] = (
            self.limit if self.limit > 0 else None)

    def take_slot(self) -> bool:
        """Spend one of this phase's budget, or report it exhausted."""
        if self.remaining is None:
            return True
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def report(self, paid: Dict[str, Any], pending: Dict[str, Any]) -> None:
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

        self.stdout.write(
            f"Pending orders past the grace period: {pending['found']} "
            f"found ({pending['paid']} paid on Stripe, "
            f"{pending['expired']} expired, {pending['open']} still open, "
            f"{pending['stuck']} unreadable).")
        for line in pending["details"]:
            self.stdout.write(line)

        if pending["paid"]:
            # Worth shouting about on stdout too: this is the number that says
            # the webhook is not doing its job.
            self.stdout.write(self.style.ERROR(
                f"  {pending['paid']} order(s) were paid on Stripe with no "
                "webhook recorded. Check STRIPE_WEBHOOK_SECRET and the "
                "endpoint's delivery log in the Stripe Dashboard."))

        skipped = paid["skipped"] + pending["skipped"]
        if skipped:
            self.stdout.write(self.style.WARNING(
                f"Stopped at this run's --limit with {skipped} order(s) "
                "unexamined; the next run picks them up."))

        if not any([paid["details"], pending["details"], skipped]):
            self.stdout.write(self.style.SUCCESS(
                "Nothing outstanding: every order is either finished or "
                "still legitimately in flight."))
