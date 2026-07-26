"""Send a mailing from the command line.

The admin's send page does the same thing a batch at a time, which is fine
for the lists this site has. This is for the one that is long enough that
clicking through it is silly, or for sending from a shell after a delivery
failure has been fixed.
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from main.models import MailingListMessage

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send a mailing list message to its remaining recipients."

    def add_arguments(self, parser):
        parser.add_argument("message_id", type=int)
        parser.add_argument(
            "--send", action="store_true",
            help="Actually send. Without it this only reports what would go "
                 "out, which is the default because the alternative default "
                 "is mailing several hundred people by accident.")
        parser.add_argument(
            "--batch-size", type=int, default=None,
            help="Recipients per SMTP connection (default: the "
                 "MAILING_LIST_SEND_BATCH_SIZE setting).")
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Stop after this many recipients. The rest stay pending and "
                 "a later run picks them up.")

    def handle(self, *args, **options):
        try:
            message = MailingListMessage.objects.get(pk=options["message_id"])
        except MailingListMessage.DoesNotExist:
            raise CommandError(f"No message with id {options['message_id']}.")

        groups = list(message.interests.all())
        pending = message.pending_count()
        self.stdout.write(f"{message.subject!r}")
        self.stdout.write(
            "  to: " + (", ".join(g.name for g in groups) if groups
                        else "everyone"))
        self.stdout.write(
            f"  confirmed recipients: {message.recipient_count()}")
        self.stdout.write(f"  already sent: {message.sent_count()}")
        self.stdout.write(f"  still to go: {pending}")

        if not options["send"]:
            self.stdout.write(
                "Dry run; nothing sent. Re-run with --send to send it.")
            return
        if not pending:
            self.stdout.write("Nothing to do.")
            return

        limit = options["limit"]
        total_sent = total_failed = 0
        # Looping on the pending count rather than on what the last batch
        # returned: a batch can legitimately come back (0, 0) -- every address
        # in it claimed by a concurrent send, say -- and stopping there would
        # quietly leave the rest of the list unmailed. The no-progress check
        # below is what stops this spinning if nothing is actually draining.
        while True:
            remaining = message.pending_count()
            if not remaining:
                break
            batch_size = options["batch_size"]
            if limit is not None:
                remaining_allowance = limit - (total_sent + total_failed)
                if remaining_allowance <= 0:
                    break
                batch_size = min(batch_size or remaining_allowance,
                                 remaining_allowance)
            sent, failed = message.send_batch(limit=batch_size)
            total_sent += sent
            total_failed += failed
            if message.pending_count() >= remaining:
                self.stderr.write(
                    f"Stopping: a batch made no progress and {remaining} "
                    "recipients are still pending. Something else may be "
                    "sending this message.")
                break
            self.stdout.write(
                f"  sent {total_sent}, failed {total_failed}, "
                f"{message.pending_count()} to go")

        self.stdout.write(
            f"Done: {total_sent} sent, {total_failed} failed, "
            f"{message.pending_count()} still pending.")
