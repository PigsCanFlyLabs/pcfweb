import contextlib
import functools
import ipaddress
import logging
import secrets

from email.utils import parseaddr
from typing import Iterable, List, Optional

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import EmailMessage, get_connection, send_mail
from django.core.validators import validate_email as django_validate_email

logger = logging.getLogger(__name__)

# Marks the owner's copy of a sale email. The copy is the same message with
# the same subject, so without this neither a mail rule nor a test can tell
# it from the one the customer got -- and "exactly one receipt went out" is
# an assertion the suite makes in a dozen places.
SALE_COPY_HEADER = "X-PCF-Copy"


def admin_recipients() -> List[str]:
    """Who to tell when something the owner has to fix goes wrong.

    Shared by the order/digital-delivery notifications on Order and by the
    startup asset audit (main/management/commands/check_book_assets.py), so
    the two cannot end up mailing different people.
    """
    recipients = []
    for entry in getattr(settings, "ADMINS", None) or []:
        # Django 5.2 requires 2-tuples, but be forgiving about the bare
        # string form so a mis-set env var is not a crash in a webhook.
        recipients.append(entry if isinstance(entry, str) else entry[1])
    return [r for r in recipients if r]


def email_admins(subject: str, body: str, error_logger: logging.Logger,
                 failure_message: str) -> bool:
    """Mail ADMINS, and never let alert delivery be the loud failure.

    Startup and management-command alerts are best-effort notifications: if
    SMTP is down, the original problem still needs to stay visible in logs and
    stdout/stderr rather than being replaced by a secondary mail exception.
    """
    recipients = admin_recipients()
    if not recipients:
        error_logger.error(
            "No ADMINS configured, so nobody was mailed: %s",
            failure_message)
        return False
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )
    except Exception:
        error_logger.exception(
            "Could not email %s: %s", ", ".join(recipients), failure_message)
        return False
    return True


def _addr_spec(address: str) -> str:
    """The bare mailbox out of an address that may carry a display name.

    "Owner <holden@pigscanfly.ca>" is a perfectly good value to type into a
    comma-separated list of addresses, and the relay delivers it, so the
    comparison key has to be the mailbox rather than the whole string --
    otherwise it never matches the bare form and the de-duplication below
    silently stops working. Normalized for the same reason normalize_email
    exists: case is not identity for a mailbox.
    """
    return normalize_email(parseaddr(address)[1])


def sales_copy_recipients(exclude: Iterable[str] = ()) -> List[str]:
    """Who gets a copy of what a sale sends the buyer, from SALES_COPY_EMAILS.

    `exclude` is the recipients of the message being copied. The owner buying
    from their own shop -- which is how the checkout gets tested -- would
    otherwise get the mail twice, once as the customer and once as the copy.

    Anything that is not a usable address is dropped and logged rather than
    passed on. Django's SMTP backend runs every recipient through
    sanitize_address *before* the send and outside its own error handling, so
    an entry this let through would raise ValueError out of the copy send
    instead of being a copy that simply does not go -- and the operator who
    typed it would have no way to tell which entry was wrong. Addresses are
    sent in the case they were configured in: the local part is case-sensitive
    per the RFC, so lower-casing somebody else's mailbox is not ours to do.
    """
    configured = getattr(settings, "SALES_COPY_EMAILS", None) or []
    if isinstance(configured, str):
        # Documented as a comma-separated string, and parse_comma_list turns
        # it into a list at the settings layer -- but a direct assignment or
        # an override_settings would otherwise be iterated character by
        # character, which is a Bcc per letter. admin_recipients() above is
        # forgiving about the same mistake for the same reason.
        configured = [part for part in configured.split(",") if part.strip()]

    excluded = {_addr_spec(address) for address in exclude}
    recipients = []
    seen = set()
    for address in configured:
        if not isinstance(address, str):
            logger.warning(
                "Ignoring a SALES_COPY_EMAILS entry that is not an address: "
                "%r.", address)
            continue
        if not address.strip():
            continue
        mailbox = _addr_spec(address)
        if mailbox in excluded or (mailbox and mailbox in seen):
            continue
        try:
            # parseaddr answers "" rather than raising for something it
            # cannot read at all ("a@b.com; c@d.com", a stray bracket), so
            # the empty case is a typo to report and not a blank to skip.
            if not mailbox:
                raise DjangoValidationError("no address in the entry")
            django_validate_email(mailbox)
        except DjangoValidationError:
            logger.warning(
                "Ignoring the unusable SALES_COPY_EMAILS entry %r.", address)
            continue
        seen.add(mailbox)
        recipients.append(address.strip())
    return recipients


def send_sales_email(subject: str, body: str, to: List[str]) -> None:
    """Send one customer-facing sale email, then copy it to the owner.

    The buyer's message goes out on its own, addressed to nobody else, and
    this raises whatever the mail backend raises for it. That is not merely
    tidiness: smtplib raises only when *every* envelope recipient is refused
    (`len(senderrs) == len(to_addrs)`), so a copy address sharing the buyer's
    envelope would turn "the buyer's address was rejected" into a silent
    success -- and the callers stamp receipt_sent_at / digital_delivery_sent_at
    on anything that does not raise, which the webhook then never retries. One
    recipient per envelope is what keeps a refused buyer a recorded failure.

    Raising is likewise deliberate: the callers catch, record the failure on
    the order and answer Stripe 2xx anyway, because a retried webhook means
    duplicate mail. That decision is theirs. Note they are not only webhooks --
    CheckoutSuccessView calls the same fulfilment path inline (main/views.py),
    so this can run in a request the buyer is watching render.

    The copy is sent afterwards and separately, and never raises: it is built
    from the same subject and body, so it cannot differ from what the customer
    got, and a copy that fails must not cost the buyer their receipt or have
    the order retried into a second one.
    """
    EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to,
    ).send(fail_silently=False)
    _send_sales_copy(subject, body, to)


def _send_sales_copy(subject: str, body: str, to: List[str]) -> bool:
    """Copy a sale email to SALES_COPY_EMAILS. Never raises.

    One message to all of them rather than one each: they are the owner's own
    addresses, so there is nothing to keep blind from anybody, and the buyer
    is not on it at all -- which is what makes this copy incapable of
    affecting their delivery.
    """
    recipients = sales_copy_recipients(exclude=to)
    if not recipients:
        return False
    try:
        EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
            headers={SALE_COPY_HEADER: "sale"},
        ).send(fail_silently=False)
    except Exception:
        logger.exception(
            "Could not copy %s to %s. The customer's own copy was sent.",
            subject, ", ".join(recipients))
        return False
    return True


def generate_username(email: str):
    username = email.split('@')[0]
    while User.objects.filter(username=username).exists():
        username += secrets.token_hex(8)
    return username


def get_client_ip(request) -> Optional[str]:
    # The in-pod nginx appends to X-Forwarded-For (see conf/nginx.default),
    # so the first entry is the client as seen by the ingress.
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


@functools.lru_cache(maxsize=1)
def _get_geoip():
    try:
        from django.contrib.gis.geoip2 import GeoIP2

        return GeoIP2()
    except Exception as e:
        logger.warning(
            f"GeoIP database unavailable; country detection disabled: {e}")
        return None


def get_country_code(request) -> Optional[str]:
    """Resolve the visitor's ISO country code via MaxMind GeoLite2.

    Returns None whenever the lookup can't happen (no GeoLite2 database
    bundled, private/unknown address, ...) so callers fall back to the
    default links.
    """
    ip = get_client_ip(request)
    geoip = _get_geoip()
    if not ip or geoip is None:
        return None
    try:
        return geoip.country_code(ip)
    except Exception as e:
        logger.debug(f"No GeoIP country for {ip}: {e}")
        return None


def get_storable_client_ip(request) -> Optional[str]:
    """The client IP, but only when it is actually an IP address.

    X-Forwarded-For is client-supplied text; nginx appends to whatever
    arrived. Handing that straight to a GenericIPAddressField turns a forged
    header into a database error on a public endpoint, so anything that is not
    a literal address is recorded as unknown instead.
    """
    ip = get_client_ip(request)
    if not ip:
        return None
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        logger.debug("Ignoring an unusable client address %r.", ip)
        return None
    return ip


def normalize_email(email: str) -> str:
    """Lower-case and strip, so Foo@Example.com is not a second subscriber.

    The local part is case-sensitive per the RFC and case-insensitive at every
    mail host anybody actually uses. Treating them as one address is what a
    subscriber expects, and -- because django-newsletter's own signup page and
    admin do not normalise -- it is also the only thing keeping one person from
    being two rows and getting two copies of a mailing.

    Django's BaseUserManager.normalize_email is not a substitute: it
    lower-cases only the domain.
    """
    return (email or "").strip().lower()


@contextlib.contextmanager
def smtp_connection():
    """One SMTP connection for a run of messages, closed however it ends.

    Not Django's `with get_connection(...)`, which is nearly this: its
    __exit__ closes unguarded, and the SMTP backend's close() can raise. A
    failure hanging up on us after the mail went out must not turn into an
    exception that loses the count of what was sent.
    """
    connection = get_connection(fail_silently=False)
    try:
        connection.open()
        yield connection
    finally:
        try:
            connection.close()
        except Exception:
            logger.warning("Could not close the SMTP connection.",
                           exc_info=True)
