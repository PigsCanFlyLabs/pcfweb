import contextlib
import functools
import ipaddress
import logging
import secrets

from typing import Iterable, List, Optional

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMessage, get_connection, send_mail

logger = logging.getLogger(__name__)


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


def sales_bcc(exclude: Iterable[str] = ()) -> List[str]:
    """Who gets a blind copy of what a sale sends the buyer.

    `exclude` is the message's own visible recipients. The owner buying from
    their own shop -- which is how the checkout gets tested -- is otherwise on
    the message twice, and Django hands both to the relay, so they get two
    copies of their own receipt. Compared normalized because case is not
    identity for a mailbox, but the address is *sent* exactly as configured:
    the local part is case-sensitive per the RFC, so lower-casing somebody
    else's mailbox on the way out is not ours to do.
    """
    excluded = {normalize_email(address) for address in exclude}
    recipients = []
    seen = set()
    for address in getattr(settings, "SALES_BCC_EMAILS", None) or []:
        normalized = normalize_email(address)
        if not normalized or normalized in excluded or normalized in seen:
            continue
        seen.add(normalized)
        recipients.append(address.strip())
    return recipients


def send_sales_email(subject: str, body: str, to: List[str]) -> None:
    """Send one customer-facing sale email, copied to SALES_BCC_EMAILS.

    Raises whatever the mail backend raises. Every caller is inside the Stripe
    webhook and already catches, records the failure on the order and returns
    2xx anyway -- a retried webhook means duplicate mail, so that decision
    belongs to them and must not be pre-empted here.

    A plain EmailMessage rather than send_mail() because that helper has no
    Bcc argument; this is the same message it would have built.
    """
    EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to,
        bcc=sales_bcc(exclude=to),
    ).send(fail_silently=False)


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
