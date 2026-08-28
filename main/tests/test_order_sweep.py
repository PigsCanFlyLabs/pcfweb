"""The sweeper, and the gap in the retry story it exists to close.

``StripeWebhookView.fulfil_order`` resumes whatever is still incomplete on a
paid order, and every post-payment action records its own failure and lets the
webhook answer 200. Those two facts were assumed to compose into a retry loop
driven by Stripe's redelivery. They do not: Stripe redelivers a *failed*
delivery, and a graceful failure is answered with a clean 200, so the resume
logic had no trigger for exactly the failures it was written for.

``WebhookGracefulFailureIsNeverRetriedTest`` below pins that down -- it is the
bug -- and everything after it covers the command that repairs both halves:
paid orders that never finished, and pending orders no webhook ever arrived
for at all.
"""

import contextlib
from datetime import timedelta
from io import StringIO
from unittest import mock

import yaml
from django.core import mail
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from main.models import Order, Product
from main.tests.base import (
    EBOOK_PK, REPO_ROOT, BookAssetRootMixin, OrderTestBase, customer_mail,
)
from main.views import StripeWebhookView


def sweep(**options) -> str:
    """Run the command, returning everything it printed."""
    out = StringIO()
    call_command("sweep_orders", stdout=out, stderr=out, **options)
    return out.getvalue()


@contextlib.contextmanager
def broken_mail(message: str = "SMTP is down"):
    """Break every way a sale sends mail, not just one of them.

    notify_owner() is on send_mail, while the receipt and the download go
    through send_sales_email (which also copies the owner). An outage that
    broke only one of the two would leave half the fulfilment succeeding --
    not the failure any of these tests is about, and enough to make a
    "nothing was sent" assertion pass on mail that did go out.
    """
    error = OSError(message)
    with mock.patch("main.models.send_mail", side_effect=error), \
            mock.patch("main.models.send_sales_email", side_effect=error):
        yield


def backdate(order: Order, **delta) -> Order:
    """Age an order past a grace period.

    Moves paid_at along with created_at when the order has one, because the
    paid sweep's window measures how long fulfilment has been outstanding --
    so an order that is merely old, but was paid ten minutes ago, is not the
    thing a caller asking for a 90-day-old order means.

    A plain UPDATE because created_at is auto_now_add, which save() would
    leave untouched and which no test can otherwise move.
    """
    when = timezone.now() - timedelta(**delta)
    fields = {"created_at": when}
    if order.paid_at is not None:
        fields["paid_at"] = when
    Order.objects.filter(pk=order.pk).update(**fields)
    order.refresh_from_db()
    return order


class WebhookGracefulFailureIsNeverRetriedTest(OrderTestBase):
    """The bug: a 200 means Stripe never comes back, so nothing retries.

    Both halves are asserted together on purpose. Either one alone reads like
    correct behaviour -- returning 200 on a mail failure is deliberate, and
    resuming on redelivery is deliberate -- and it is only the pair that shows
    the order is stranded.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=100, quantity=1)

    def test_a_failed_owner_email_leaves_a_paid_order_stripe_will_not_revisit(self):
        with broken_mail():
            with self.assertLogs("main.models", level="ERROR"):
                response = self.deliver(self.event_body(self.order))

        # A 2xx is Stripe's definition of a delivered webhook. It will not be
        # redelivered, so nothing in the system will call fulfil_order again.
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNone(self.order.notified_at)
        self.assertIn("SMTP is down", self.order.notification_error)
        self.assertEqual(self.order_emails(), [])
        # The receipt went out through the same dead SMTP server, so both are
        # still owed -- and neither has anything left that would retry it.
        self.assertEqual(
            self.order.outstanding_fulfilment(),
            ["receipt not sent", "owner not notified"])

    def test_a_crash_is_the_only_failure_stripe_does_come_back_for(self):
        """The one shape the original design genuinely covers."""
        with mock.patch.object(
                StripeWebhookView, "fulfil_order",
                side_effect=SystemExit("simulated worker crash")):
            with self.assertRaises(SystemExit):
                self.deliver(self.event_body(self.order))

        # No response was ever returned, so Stripe records a failed delivery
        # and retries -- which is why this case was never the stranded one.
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNone(self.order.notified_at)

    def test_the_sweeper_is_what_finally_finishes_it(self):
        with broken_mail():
            with self.assertLogs("main.models", level="ERROR"):
                self.deliver(self.event_body(self.order))

        output = sweep()

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.notified_at)
        self.assertEqual(self.order.notification_error, "")
        self.assertEqual(len(self.order_emails()), 1)
        self.assertEqual(self.order.outstanding_fulfilment(), [])
        self.assertIn(f"order #{self.order.pk}", output)
        self.assertIn("owner not notified", output)


@override_settings(STRIPE_API_KEY="sk_test_sweep")
class SweepPaidOrdersTest(OrderTestBase):
    """Resuming the actions a paid order still owes."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=100, quantity=1)
        self.deliver(self.event_body(self.order))
        self.order.refresh_from_db()
        mail.outbox.clear()

    def test_a_fully_fulfilled_order_is_left_alone(self):
        self.assertEqual(self.order.outstanding_fulfilment(), [])

        output = sweep()

        self.assertEqual(mail.outbox, [])
        self.assertIn("Nothing outstanding", output)

    def test_an_unnotified_order_is_notified(self):
        Order.objects.filter(pk=self.order.pk).update(notified_at=None)

        sweep()

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.notified_at)
        self.assertEqual(len(self.order_emails()), 1)

    def test_an_unsent_receipt_is_sent(self):
        Order.objects.filter(pk=self.order.pk).update(receipt_sent_at=None)

        sweep()

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.receipt_sent_at)
        self.assertEqual(
            len(customer_mail("Your receipt")), 1)

    def test_an_unreconciled_order_is_reconciled(self):
        Order.objects.filter(pk=self.order.pk).update(reconciled_at=None)

        sweep()

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.reconciled_at)

    def test_nothing_already_done_is_repeated(self):
        Order.objects.filter(pk=self.order.pk).update(notified_at=None)

        sweep()
        sweep()

        self.assertEqual(len(self.order_emails()), 1,
                         "the second sweep must find nothing left to do")

    def test_an_order_whose_fulfilment_is_claimed_is_left_to_its_worker(self):
        Order.objects.filter(pk=self.order.pk).update(
            notified_at=None, fulfilment_claimed_at=timezone.now())

        with self.assertLogs("main.views", level="INFO"):
            sweep()

        self.order.refresh_from_db()
        self.assertIsNone(self.order.notified_at)
        self.assertEqual(self.order_emails(), [])

    def test_a_claim_left_by_a_dead_worker_is_reclaimed(self):
        stale = timezone.now() - StripeWebhookView.FULFILMENT_LEASE - timedelta(
            minutes=1)
        Order.objects.filter(pk=self.order.pk).update(
            notified_at=None, fulfilment_claimed_at=stale)

        sweep()

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.notified_at)

    def test_a_pending_order_is_not_treated_as_needing_fulfilment(self):
        pending = self.place_order(
            product_pk=100, quantity=1, session_id="cs_still_pending")

        self.assertNotIn(
            pending, list(Order.needing_fulfilment()),
            "only PAID orders are fulfilment's business")

    def test_a_still_incomplete_order_is_reported_rather_than_hidden(self):
        Order.objects.filter(pk=self.order.pk).update(notified_at=None)

        with broken_mail("still down"):
            with self.assertLogs("main.models", level="ERROR"):
                output = sweep()

        self.assertIn("still incomplete after the retry", output)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.notified_at)

    def test_fail_exits_non_zero_when_something_is_left(self):
        Order.objects.filter(pk=self.order.pk).update(notified_at=None)

        with broken_mail("still down"):
            with self.assertLogs("main.models", level="ERROR"):
                with self.assertRaises(SystemExit):
                    sweep(fail=True)

    def test_a_sweep_that_finally_reconciles_reissues_the_pick_list(self):
        """The reissue that previously needed a redelivery that never came.

        An owner emailed from the cart snapshot was told the quantities were
        unverified. When a later attempt gets the real ones from Stripe they
        may differ, so fulfil_order reissues the notification -- and until
        the sweep existed, nothing was ever going to make that later attempt.
        """
        Order.objects.filter(pk=self.order.pk).update(
            reconciled_at=None,
            reconciliation_error="Timeout: Stripe took too long")
        self.billed_quantities = {100: 5}

        sweep()

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.reconciled_at)
        self.assertEqual(self.order.items.first().quantity, 5)
        self.assertIsNotNone(self.order.notified_at)
        reissued = self.order_emails()
        self.assertEqual(len(reissued), 1)
        self.assertIn("5", reissued[0].body)

    def test_a_window_keeps_the_sweep_off_ancient_orders(self):
        Order.objects.filter(pk=self.order.pk).update(notified_at=None)
        backdate(self.order, days=90)

        sweep(window_days=30)

        self.order.refresh_from_db()
        self.assertIsNone(self.order.notified_at)

        sweep(window_days=0)

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.notified_at)

    def test_the_window_runs_from_payment_not_from_checkout(self):
        """A delayed payment method pays an order long after it is created.

        Windowing on created_at puts an order that was just paid, but placed
        a while ago, permanently outside every sweep -- stranded exactly the
        way this command exists to prevent.
        """
        Order.objects.filter(pk=self.order.pk).update(notified_at=None)
        # Placed 90 days ago; the ACH debit settled ten minutes ago.
        backdate(self.order, days=90)
        Order.objects.filter(pk=self.order.pk).update(
            paid_at=timezone.now() - timedelta(minutes=10))

        sweep(window_days=30)

        self.order.refresh_from_db()
        self.assertIsNotNone(
            self.order.notified_at,
            "fulfilment has been outstanding ten minutes, not 90 days")

    def test_a_capped_run_reads_only_its_budget_of_rows(self):
        """--limit has to bound the work, not just the retries.

        Walking the whole queryset to count what it would skip consulted
        outstanding_fulfilment() -- which reads an order's items -- on every
        matching row, so a large backlog turned a job capped at 200 retries
        into thousands of queries.

        fulfil_order is stubbed out so the only thing left that would touch
        a candidate row is the scan itself.
        """
        for index in range(4):
            extra = self.place_order(
                product_pk=100, quantity=1, session_id=f"cs_backlog_{index}")
            self.deliver(self.event_body(extra))
            Order.objects.filter(pk=extra.pk).update(notified_at=None)
        Order.objects.filter(pk=self.order.pk).update(notified_at=None)
        self.assertEqual(Order.needing_fulfilment().count(), 5)

        with mock.patch.object(StripeWebhookView, "fulfil_order"):
            with mock.patch.object(
                    Order, "outstanding_fulfilment", autospec=True,
                    side_effect=Order.outstanding_fulfilment) as consulted:
                output = sweep(limit=1)

        # The one row this run's budget bought, and the same row re-checked
        # after fulfilment. Not one per matching order.
        self.assertLessEqual(
            consulted.call_count, 2,
            f"examined {consulted.call_count} orders for a --limit of 1")
        self.assertIn("unexamined", output)

    def test_a_capped_run_does_not_spend_itself_on_the_same_stuck_orders(self):
        """The starvation --limit would otherwise cause every single run.

        Oldest-first spends the whole budget on the lowest ids, so orders
        that can never clear their marker would hide every order placed
        after them -- forever, with --window-days=0.
        """
        newer = self.place_order(
            product_pk=100, quantity=1, session_id="cs_newer")
        self.deliver(self.event_body(newer))
        Order.objects.filter(pk=newer.pk).update(notified_at=None)
        # self.order is older and permanently stuck: its send never succeeds.
        Order.objects.filter(pk=self.order.pk).update(notified_at=None)
        self.assertLess(self.order.pk, newer.pk)
        mail.outbox.clear()

        sweep(limit=1)

        newer.refresh_from_db()
        self.assertIsNotNone(
            newer.notified_at,
            "a capped run must reach the newest incomplete order")

    def test_fail_reports_a_backlog_the_limit_never_reached(self):
        """--fail promises to fail when anything is left unfinished."""
        for index in range(2):
            extra = self.place_order(
                product_pk=100, quantity=1, session_id=f"cs_backlog_{index}")
            self.deliver(self.event_body(extra))
            Order.objects.filter(pk=extra.pk).update(notified_at=None)

        with self.assertRaises(SystemExit):
            sweep(fail=True, limit=1)

    def test_fail_is_quiet_when_the_limit_reached_everything(self):
        Order.objects.filter(pk=self.order.pk).update(notified_at=None)

        sweep(fail=True, limit=50)

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.notified_at)

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        Order.objects.filter(pk=self.order.pk).update(notified_at=None)

        output = sweep(dry_run=True)

        self.order.refresh_from_db()
        self.assertIsNone(self.order.notified_at)
        self.assertEqual(mail.outbox, [])
        self.assertIn("Dry run", output)
        self.assertIn(f"order #{self.order.pk}", output)


class SweepDigitalOrderTest(BookAssetRootMixin, OrderTestBase):
    """The download half, which is the one a buyer notices."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=EBOOK_PK, quantity=1)

    def test_an_undelivered_download_is_delivered_by_the_sweep(self):
        with broken_mail():
            with self.assertLogs("main.models", level="ERROR"):
                self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.assertIsNone(self.order.digital_delivery_sent_at)
        self.assertIn("download not delivered",
                      self.order.outstanding_fulfilment())

        sweep()

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.digital_delivery_sent_at)
        self.assertEqual(
            len(customer_mail("Your download")), 1)

    def test_a_physical_order_is_not_waiting_on_a_download(self):
        physical = self.place_order(
            product_pk=100, quantity=1, session_id="cs_physical")
        self.deliver(self.event_body(physical))
        physical.refresh_from_db()

        self.assertNotIn("download not delivered",
                         physical.outstanding_fulfilment())
        self.assertNotIn(physical, list(Order.needing_fulfilment()))

    def test_a_deleted_product_does_not_make_the_sweep_spin(self):
        """needing_fulfilment() is coarser than fulfil_order()'s branches."""
        self.deliver(self.event_body(self.order))
        Order.objects.filter(pk=self.order.pk).update(
            digital_delivery_sent_at=None)
        Product.objects.filter(pk=EBOOK_PK).delete()
        self.order.refresh_from_db()

        self.assertEqual(self.order.outstanding_fulfilment(), [])

        output = sweep()

        self.assertIn("Nothing outstanding", output)


@override_settings(STRIPE_API_KEY="sk_test_sweep")
class SweepPendingOrdersTest(OrderTestBase):
    """Orders no webhook ever arrived for, which is the outage case."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=100, quantity=1)
        backdate(self.order, hours=2)

    def retrieve(self, **overrides):
        session = self.session_payload(self.order, **overrides)
        return mock.patch("stripe.checkout.Session.retrieve",
                          return_value=session)

    def test_an_order_stripe_says_was_paid_is_paid_and_fulfilled(self):
        with self.retrieve(status="complete"):
            with self.assertLogs("main.management.commands.sweep_orders",
                                 level="ERROR") as log:
                output = sweep()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNotNone(self.order.paid_at)
        self.assertIsNotNone(self.order.notified_at)
        self.assertEqual(len(self.order_emails()), 1)
        self.assertTrue(any("no webhook ever recorded it" in m
                            for m in log.output))
        self.assertIn("Check STRIPE_WEBHOOK_SECRET", output)

    def test_a_free_order_stripe_never_charges_for_is_also_paid(self):
        with self.retrieve(status="complete",
                           payment_status="no_payment_required"):
            with self.assertLogs("main.management.commands.sweep_orders",
                                 level="ERROR"):
                sweep()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_an_expired_session_cancels_the_order(self):
        with self.retrieve(status="expired", payment_status="unpaid"):
            output = sweep()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)
        self.assertEqual(mail.outbox, [])
        self.assertIn("session expired unpaid", output)

    def test_a_session_still_open_is_left_alone(self):
        with self.retrieve(status="open", payment_status="unpaid"):
            output = sweep()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertIn("1 still open", output)

    def test_a_delayed_payment_that_has_not_settled_is_left_alone(self):
        with self.retrieve(status="complete", payment_status="unpaid"):
            sweep()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_an_order_inside_the_grace_period_is_not_asked_about(self):
        fresh = self.place_order(
            product_pk=100, quantity=1, session_id="cs_fresh")

        with mock.patch("stripe.checkout.Session.retrieve") as retrieve:
            retrieve.return_value = self.session_payload(
                self.order, status="open", payment_status="unpaid")
            sweep(pending_after_minutes=60)

        asked = [call.args[0] for call in retrieve.call_args_list]
        self.assertNotIn("cs_fresh", asked,
                         "a buyer may still be on Stripe's page")
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, Order.Status.PENDING)

    def test_a_session_that_is_not_the_one_asked_for_is_refused(self):
        """paid_fields() writes stripe_session_id, so the binding is checked."""
        other = self.place_order(
            product_pk=100, quantity=1, session_id="cs_someone_else")

        with mock.patch("stripe.checkout.Session.retrieve",
                        return_value=self.session_payload(other)):
            with self.assertLogs("main.management.commands.sweep_orders",
                                 level="ERROR") as log:
                output = sweep()

        self.order.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(self.order.stripe_session_id, "cs_test_session")
        self.assertEqual(other.status, Order.Status.PENDING)
        self.assertTrue(any("Not marking it paid" in m for m in log.output))
        self.assertIn("not the one asked for", output)

    def test_an_order_with_no_session_id_is_never_looked_up(self):
        Order.objects.filter(pk=self.order.pk).update(stripe_session_id=None)

        with mock.patch("stripe.checkout.Session.retrieve") as retrieve:
            sweep()

        self.assertEqual(retrieve.call_count, 0)

    def test_a_stripe_lookup_failure_does_not_stop_the_sweep(self):
        second = self.place_order(
            product_pk=100, quantity=1, session_id="cs_second")
        backdate(second, hours=2)
        good = self.session_payload(second, status="complete")

        def retrieve(session_id, *args, **kwargs):
            if session_id == self.order.stripe_session_id:
                raise RuntimeError("Stripe is unreachable")
            return good

        with mock.patch("stripe.checkout.Session.retrieve",
                        side_effect=retrieve):
            with self.assertLogs("main.management.commands.sweep_orders"):
                output = sweep()

        self.order.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(second.status, Order.Status.PAID,
                         "one bad session must not strand the rest")
        self.assertIn("1 unreadable", output)

    def test_the_owner_is_mailed_that_the_webhook_is_not_working(self):
        """The one failure that is invisible from the site itself."""
        with self.retrieve(status="complete"):
            with self.assertLogs("main.management.commands.sweep_orders",
                                 level="ERROR"):
                sweep()

        alerts = [m for m in mail.outbox
                  if "no webhook recorded" in m.subject]
        self.assertEqual(len(alerts), 1)
        self.assertIn(f"#{self.order.pk}", alerts[0].body)
        self.assertIn("STRIPE_WEBHOOK_SECRET", alerts[0].body)

    def test_one_alert_covers_every_stranded_order(self):
        second = self.place_order(
            product_pk=100, quantity=1, session_id="cs_second")
        backdate(second, hours=2)

        def retrieve(session_id, *args, **kwargs):
            asked = Order.objects.get(stripe_session_id=session_id)
            return self.session_payload(asked, status="complete")

        with mock.patch("stripe.checkout.Session.retrieve",
                        side_effect=retrieve):
            with self.assertLogs("main.management.commands.sweep_orders",
                                 level="ERROR"):
                sweep()

        alerts = [m for m in mail.outbox
                  if "no webhook recorded" in m.subject]
        self.assertEqual(len(alerts), 1, "one mail, not one per order")
        self.assertIn(f"#{self.order.pk}", alerts[0].body)
        self.assertIn(f"#{second.pk}", alerts[0].body)

    def test_a_working_webhook_raises_no_alert(self):
        self.deliver(self.event_body(self.order))
        mail.outbox.clear()

        sweep()

        self.assertEqual(
            [m for m in mail.outbox if "no webhook recorded" in m.subject], [])

    def test_a_failed_alert_does_not_lose_the_repair(self):
        with self.retrieve(status="complete"):
            with self.assertLogs("main.management.commands.sweep_orders",
                                 level="ERROR"):
                with mock.patch("main.utils.send_mail",
                                side_effect=OSError("SMTP is down")):
                    sweep()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID,
                         "the alert is best-effort; the repair is not")

    def test_dry_run_raises_no_alert(self):
        with self.retrieve(status="complete"):
            sweep(dry_run=True)

        self.assertEqual(
            [m for m in mail.outbox if "no webhook recorded" in m.subject], [])

    def test_dry_run_asks_stripe_but_changes_nothing(self):
        with self.retrieve(status="complete"):
            output = sweep(dry_run=True)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(mail.outbox, [])
        self.assertIn("paying and fulfilling it", output)

    def test_a_paid_backlog_cannot_starve_the_pending_sweep(self):
        """--limit is per phase, and the urgent phase runs first."""
        for index in range(3):
            stale = self.place_order(
                product_pk=100, quantity=1, session_id=f"cs_paid_{index}")
            self.deliver(self.event_body(stale))
            Order.objects.filter(pk=stale.pk).update(notified_at=None)
        mail.outbox.clear()

        with self.retrieve(status="complete"):
            with self.assertLogs("main.management.commands.sweep_orders",
                                 level="ERROR"):
                sweep(limit=1)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID,
                         "the unrecorded payment must still be reached")

    def test_the_limit_bounds_the_run_and_says_what_it_skipped(self):
        for index in range(3):
            extra = self.place_order(
                product_pk=100, quantity=1, session_id=f"cs_extra_{index}")
            backdate(extra, hours=2)

        def retrieve(session_id, *args, **kwargs):
            # Answer for whichever session was asked about, so this exercises
            # the limit rather than tripping the session-binding guard.
            asked = Order.objects.get(stripe_session_id=session_id)
            return self.session_payload(
                asked, status="open", payment_status="unpaid")

        with mock.patch("stripe.checkout.Session.retrieve",
                        side_effect=retrieve) as retrieved:
            output = sweep(limit=2)

        self.assertEqual(retrieved.call_count, 2)
        self.assertIn("unexamined", output)

    def test_a_capped_pending_run_reads_only_its_budget_of_rows(self):
        """The pending phase is bounded the same way the paid one is."""
        for index in range(4):
            extra = self.place_order(
                product_pk=100, quantity=1, session_id=f"cs_pending_{index}")
            backdate(extra, hours=2)

        def retrieve(session_id, *args, **kwargs):
            asked = Order.objects.get(stripe_session_id=session_id)
            return self.session_payload(
                asked, status="open", payment_status="unpaid")

        with mock.patch("stripe.checkout.Session.retrieve",
                        side_effect=retrieve) as retrieved:
            output = sweep(limit=2)

        self.assertEqual(retrieved.call_count, 2)
        self.assertIn("unexamined", output)

    def test_each_phase_gets_its_own_budget(self):
        """--limit is per phase, and the urgent phase is never starved."""
        for index in range(3):
            stale = self.place_order(
                product_pk=100, quantity=1, session_id=f"cs_stale_{index}")
            self.deliver(self.event_body(stale))
            Order.objects.filter(pk=stale.pk).update(notified_at=None)
        mail.outbox.clear()

        with self.retrieve(status="complete"):
            with self.assertLogs("main.management.commands.sweep_orders",
                                 level="ERROR"):
                sweep(limit=1)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID,
                         "the unrecorded payment must still be reached")

    def test_a_sweep_racing_a_live_webhook_pays_the_order_once(self):
        body = self.event_body(self.order)
        self.deliver(body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        emails_after_webhook = len(self.order_emails())

        with self.retrieve(status="complete"):
            sweep()

        self.assertEqual(len(self.order_emails()), emails_after_webhook,
                         "the sweep must not re-notify a finished order")


class OrderSweepIsScheduledTest(SimpleTestCase):
    """The command only closes the gap if something actually runs it.

    Written against deploy.yaml rather than trusting the CronJob to stay
    there: the whole point of the sweeper is that nobody is watching, so a
    manifest edit that quietly drops it would restore exactly the silence it
    was added to break.
    """

    def setUp(self):
        with open(REPO_ROOT / "deploy.yaml") as fh:
            docs = [doc for doc in yaml.safe_load_all(fh) if doc]
        self.cronjob = next(
            doc for doc in docs
            if doc.get("kind") == "CronJob"
            and doc["metadata"]["name"] == "order-sweep")

    def container(self):
        spec = self.cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        return spec["containers"][0]

    def test_the_cronjob_runs_the_sweeper(self):
        self.assertIn("manage.py sweep_orders",
                      " ".join(self.container()["args"]))

    def test_it_runs_at_least_hourly(self):
        # A buyer waiting on a download after an SMTP outage waits one run.
        minute, hour = self.cronjob["spec"]["schedule"].split()[:2]
        self.assertEqual(hour, "*")
        self.assertNotIn("*", minute, "an every-minute sweep is not the point")

    def test_two_sweeps_can_never_overlap(self):
        self.assertEqual(self.cronjob["spec"]["concurrencyPolicy"], "Forbid")

    def test_it_reaches_the_database_and_stripe_credentials(self):
        sources = self.container()["envFrom"]
        self.assertIn({"secretRef": {"name": "pcfweb-secret"}}, sources)
        self.assertIn({"configMapRef": {"name": "pcfweb-db-config"}}, sources)
        self.assertTrue(any(env["name"] == "DBPASSWORD"
                            for env in self.container()["env"]))
