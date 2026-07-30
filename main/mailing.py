"""The mailing list, as a thin layer over django-newsletter.

One `Newsletter` is one interest area, and its `Subscription` rows are the
subscribers -- along with the double opt-in email, the activation and
unsubscribe pages and the admin CSV import, all of which that app already
has. What lives here is only the glue our own signup form needs, plus the
one behaviour it does not provide: an old activation link losing its power
once somebody has unsubscribed.
"""

import csv
import io
import logging
import re

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email as django_validate_email
from django.db.models import Q
from django.db.models.functions import Lower
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.template.loader import render_to_string

from newsletter.admin_utils import make_subscription
from newsletter.models import Newsletter, Subscription
from newsletter.utils import get_default_sites, make_activation_code

from main.models import (
    ALL_INTEREST_SLUG, DEFAULT_INTEREST_SLUG, Product, SuppressedAddress,
    mailing_list_from_email)
from main.utils import normalize_email, smtp_connection

logger = logging.getLogger(__name__)

# Seeded by a data migration; default_newsletter() re-creates the general one
# if it ever goes away. Defined in main.models because the send layer there
# needs the All slug and this module imports that one, not the other way round.
DEFAULT_SLUG = DEFAULT_INTEREST_SLUG

# The list for people who want everything, offered as a checkbox next to the
# topic they picked. Subscribers are only ever put on it because they ticked
# that box -- signing up for one topic does not opt you into the rest -- but
# once they are on it, every mailing reaches them (see
# MailingListMessage.recipients).
ALL_SLUG = ALL_INTEREST_SLUG


def interest_choices(request=None) -> List[Newsletter]:
    """The lists to offer on a signup form, general first.

    Which one is *pre-selected* is a separate question and is not this: the
    form marks the general list selected, because the rule is that not
    choosing means general, and the one listed first must not quietly become
    the default.

    Site-scoped to match django-newsletter's own views: a list its activation
    page would 404 on is not one to offer a signup for.
    """
    newsletters = Newsletter.objects.filter(visible=True)
    if request is not None:
        # RequestSite (no sites framework row) has no id to filter on; there
        # is nothing to scope by in that case, so offer everything.
        site_id = getattr(get_current_site(request), "id", None)
        if site_id is not None:
            newsletters = newsletters.filter(site__id=site_id)
    return sorted(
        newsletters, key=lambda n: (n.slug != DEFAULT_SLUG, n.title))


# The list for anyone who bought a book we do not run a dedicated list for.
# Seeded by migration 0014 along with the rest; if it is not there, the general
# list is the answer instead -- see interest_for_products.
BOOKS_SLUG = "books"

# Everything that is not a letter or a digit, which is where a product name and
# a list title differ: "Distributed Computing 4 Kids (and Executives)" against
# "Distributed Computing 4 Kids and Executives", or "High Performance Spark,
# 2nd Edition" against "High Performance Spark".
NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")

# Shortest normalized title allowed to match a product name by prefix. Guards
# against a list somebody titles "Us" or "AI" matching half the catalogue.
MIN_TITLE_PREFIX = 4


def normalized_title(text: str) -> str:
    return NON_ALPHANUMERIC.sub("", (text or "").lower())


def interest_for_products(products: Iterable[Any]) -> str:
    """Which list to pre-select for somebody who just bought these.

    Used by the checkout success page, and *only* to decide which entry of a
    dropdown starts selected -- the buyer picks, and the subscription itself
    comes from what they submit. That is what makes matching a product name
    against a list title an acceptable way to do this: nothing is subscribed
    on the strength of the guess, and the failure mode is the general list
    being pre-selected for a book that has its own.

    An order spanning two topics gets the general list rather than whichever
    of the two happens to be found first: with no single answer, the honest
    pre-selection is "updates from us", which is also what an unticked
    everything-checkbox then means.

    Deliberately not a column on Product. A per-row slug would need keeping
    in step with the seeded lists by hand, and getting it wrong there would
    subscribe people to the wrong list rather than merely pre-select one.
    """
    titles = {}
    for newsletter in Newsletter.objects.filter(visible=True):
        if newsletter.slug in {DEFAULT_SLUG, ALL_SLUG}:
            # Neither is a topic: they are "everything" and "the rest".
            continue
        normalized = normalized_title(newsletter.title)
        if len(normalized) >= MIN_TITLE_PREFIX:
            titles[normalized] = newsletter.slug

    matched = set()
    saw_a_book = False
    for product in products:
        if product is None:
            # An OrderItem whose Product row has since been deleted still
            # names the thing that was bought, but there is nothing to read a
            # category off, so it cannot vote.
            continue
        if getattr(product, "cat", None) == Product.Categories.BOOKS:
            saw_a_book = True
        name = normalized_title(getattr(product, "name", ""))
        for normalized, slug in titles.items():
            # Prefix rather than equality: the list is for the work, and the
            # products are its editions and formats.
            if name.startswith(normalized):
                matched.add(slug)

    if len(matched) == 1:
        return matched.pop()
    if not matched and saw_a_book and BOOKS_SLUG in titles.values():
        return BOOKS_SLUG
    return DEFAULT_SLUG


def default_newsletter() -> Newsletter:
    """The fallback list, created on demand.

    A signup that names no list is the common case (the footer form), so this
    has to work even on a database where the seeded row was renamed or
    deleted, rather than 500ing the signup endpoint.
    """
    newsletter, created = Newsletter.objects.get_or_create(
        slug=DEFAULT_SLUG,
        defaults={"title": "General updates",
                  "email": settings.DEFAULT_FROM_EMAIL,
                  "sender": "Pigs Can Fly Labs"})
    if created:
        # Their views filter on site, so a list with none attached cannot be
        # confirmed or unsubscribed from. get_default_sites is the same
        # callable their own Newsletter.site field defaults to.
        newsletter.site.set(get_default_sites())
    return newsletter


def newsletter_for(slug: str) -> Newsletter:
    """The list a signup asked for, or the general one.

    An embedded form on another site carries a hard-coded slug in that site's
    markup. If we rename or hide a list, that form must not start throwing
    addresses away -- so an unknown slug falls back rather than failing.
    """
    if slug:
        # site as well as visible: django-newsletter's activation page filters
        # on site, so a list with none attached is one whose confirmation link
        # 404s -- better to land them somewhere that works.
        newsletter = Newsletter.objects.filter(
            slug=slug, visible=True, site__isnull=False).first()
        if newsletter is not None:
            return newsletter
        logger.info("Signup named an unknown list %r; using the default.",
                    slug)
    return default_newsletter()


def subscribe(email: str, newsletter: Optional[Newsletter] = None,
              name: str = "", ip: Optional[str] = None,
              also_all: bool = False,
              ) -> Tuple[Optional[Subscription], Optional[Subscription]]:
    """Record a signup.

    Returns (the subscription, the one whose activation email should go out --
    or (None, None) if we will not mail this address at all).

    `also_all` is the "send me all updates" checkbox. It subscribes them to the
    All list *instead of* the topic they picked, because All already receives
    every mailing addressed to any public list -- so one row covers both, and
    the confirmation email django-newsletter sends names exactly the list being
    confirmed.

    That is deliberately not "one click confirms two rows". An earlier version
    created a second row carrying the first one's activation code, and because
    the code was copied server-side from whatever row already existed, anyone
    who knew an address could post it with the box ticked and have the *victim's
    own* confirmation click put them on the All list. A confirmation click can
    only honestly confirm what the email it came from says.

    Signing up an address that is already subscribed is a no-op rather than a
    reset to unconfirmed: otherwise anyone could quietly knock a subscriber off
    the list by re-posting their address.
    """
    email = normalize_email(email)
    if SuppressedAddress.matching([email]):
        # They are on the never-email list. Not even the confirmation goes out:
        # the endpoint is open to the internet, so otherwise anyone could have
        # us mail a complained-about or hard-bouncing address on demand, which
        # is the exact thing that gets a domain blocked.
        logger.info("Refusing a signup for %s: it is suppressed.", email)
        return (None, None)
    if also_all:
        everything = Newsletter.objects.filter(slug=ALL_SLUG).first()
        if everything is None:
            logger.info("No %r list, so the everything checkbox did nothing.",
                        ALL_SLUG)
        else:
            newsletter = everything
    newsletter = newsletter or default_newsletter()
    # iexact, because django-newsletter's own signup page and admin do not
    # normalise: without it Bob@Example.COM becomes a second row for one
    # person, which is two copies of every mailing.
    subscription = Subscription.objects.filter(
        newsletter=newsletter, user=None, email_field__iexact=email).first()
    if subscription is None:
        subscription = Subscription.objects.create(
            newsletter=newsletter, user=None, email_field=email,
            name_field=name, ip=ip)
        return (subscription, subscription)
    if subscription.subscribed and not subscription.unsubscribed:
        return (subscription, None)
    if name and not subscription.name_field:
        subscription.name_field = name
    if subscription.unsubscribed:
        # Coming back after unsubscribing means confirming again, with a code
        # that has not been in an email before. We do not resurrect a withdrawn
        # consent on somebody else's say-so.
        subscription.activation_code = make_activation_code()
    subscription.save()
    return (subscription, subscription)


def send_activation_email(subscription: Subscription) -> bool:
    """Ask the address to confirm, using django-newsletter's own templates.

    Best effort by design: a dead SMTP server must not turn a signup into a
    500 on somebody else's site. The subscription stays unconfirmed, so
    signing up again sends another one.
    """
    try:
        subscription.send_activation_email("subscribe")
    except Exception:
        logger.exception(
            "Could not send the mailing list confirmation to %s; the signup "
            "stays unconfirmed.", subscription.email)
        return False
    return True


@receiver(pre_save, sender=Subscription,
          dispatch_uid="main.mailing.rotate_code_on_unsubscribe")
def rotate_activation_code_on_unsubscribe(sender, instance, **kwargs):
    """Kill the old activation link when somebody unsubscribes.

    django-newsletter reuses one activation code for both actions and does not
    change it on unsubscribe, so the original "confirm your subscription"
    email keeps a working link: a forwarded copy of it, or a mail scanner
    reaching it late, could put somebody back on a list they had left.
    Replacing the code at the moment they leave closes that off.

    pre_save rather than post_save so the new code is written by the save
    already in progress instead of recursing into another one.
    """
    if kwargs.get("raw") or not instance.pk:
        # raw is a loaddata save: the fixture's values are the intent, and
        # rewriting one of them from a signal is not this receiver's business.
        return
    was_unsubscribed = sender.objects.filter(
        pk=instance.pk).values_list("unsubscribed", flat=True).first()
    if was_unsubscribed is False and instance.unsubscribed:
        instance.activation_code = make_activation_code()


def unsubscribe_url(subscription: Subscription, request=None) -> str:
    """django-newsletter's own unsubscribe page for this subscription.

    Reusing theirs rather than growing our own: it already carries an
    activation code, already asks before acting, and is already the link their
    activation emails use, so a subscriber sees one unsubscribe page however
    they got there.
    """
    from main.models import absolute_site_url

    return absolute_site_url(subscription.unsubscribe_activate_url(), request)


@dataclass
class ImportResult:
    """What an import did, in the terms the person who ran it cares about."""

    found: int = 0
    added: int = 0
    already_there: int = 0
    suppressed: List[str] = field(default_factory=list)
    notified: int = 0
    notice_attempted: bool = False
    notice_skipped: bool = False

    def summary(self) -> str:
        parts = [
            f"{self.found} address(es) in the file", f"{self.added} added"]
        if self.already_there:
            parts.append(f"{self.already_there} already on that list")
        if self.suppressed:
            parts.append(
                f"{len(self.suppressed)} skipped as suppressed")
        if self.notified:
            parts.append(f"{self.notified} told the list changed")
        elif self.notice_attempted and self.added:
            # Said out loud: a silent 0 here reads as a clean import in which
            # nobody was actually told they had been added.
            parts.append(
                "but nobody could be told the list changed -- check the log "
                "and the mail server")
        return ", ".join(parts) + "."


def import_addresses(addresses: Dict[str, str], newsletter: Newsletter,
                     notify: bool = False, request=None) -> ImportResult:
    """Add parsed addresses to a list as confirmed subscribers.

    No confirmation email: an import is the owner asserting they already have
    consent for these addresses, which is the difference between an import and
    the website signup. The suppression list is the one thing that can still
    veto a row, and the optional notice is what gives the people involved a
    way out.

    Addresses that already have a subscription on this list -- including ones
    that unsubscribed from it -- are skipped by the `existing` query below.
    (django-newsletter's own parser does that while reading the file; ours
    deliberately does not filter, because the suppression import needs exactly
    the addresses a subscriber import skips.)
    """
    result = ImportResult(found=len(addresses))
    suppressed = SuppressedAddress.matching(addresses)
    # Anybody already on this list, whether they are still subscribed or left
    # it. An import must not resurrect an unsubscribe, and re-adding a current
    # subscriber would only reset their name.
    existing = set(Subscription.objects.filter(
        newsletter=newsletter).annotate(
            normalized=Lower("email_field")).filter(
                normalized__in=[normalize_email(a) for a in addresses]
    ).values_list("normalized", flat=True))
    imported = []
    for email, name in addresses.items():
        email = normalize_email(email)
        if email in suppressed:
            result.suppressed.append(email)
            logger.info("Not importing %s: it is on the suppression list.",
                        email)
            continue
        if email in existing:
            result.already_there += 1
            continue
        subscription = make_subscription(newsletter, email, name)
        subscription.save()
        imported.append(subscription)
    result.added = len(imported)
    if notify and imported:
        limit = getattr(settings, "MAILING_LIST_IMPORT_NOTICE_MAX", 500)
        if len(imported) > limit:
            # Sending these is a loop inside one request, so a big import
            # would outlive the worker timeout half-done. The send page exists
            # for that: it batches and it is resumable.
            result.notice_skipped = True
            logger.warning(
                "Imported %s addresses, more than the %s notice limit; not "
                "emailing them from the import page.", len(imported), limit)
        else:
            result.notice_attempted = True
            result.notified = send_import_notice(imported, request)
    return result


def send_import_notice(subscriptions: List[Subscription],
                       request=None) -> int:
    """Tell imported people the list changed, and how to get off it.

    Best effort per address and reported as a count: an address that cannot be
    reached is not a reason to unwind an import, and these are addresses we
    were told we already had consent for -- the notice is a courtesy and an
    escape hatch, not the consent itself.
    """
    sent = 0
    with smtp_connection() as connection:
        for subscription in subscriptions:
            link = unsubscribe_url(subscription, request)
            context = {
                "subscription": subscription,
                "interest": subscription.newsletter,
                "name": subscription.name or "",
                "unsubscribe_url": link,
            }
            subject = render_to_string(
                "email/mailing_list_import_notice_subject.txt",
                context).strip()
            body = render_to_string(
                "email/mailing_list_import_notice.txt", context)
            try:
                EmailMessage(
                    subject=subject, body=body,
                    # This notice is per list, so it can use the list's own
                    # Sender and E-mail fields -- the ones the admin offers and
                    # django-newsletter's own activation mail already honours.
                    from_email=subscription.newsletter.get_sender(),
                    to=[subscription.get_recipient()], connection=connection,
                    headers={"List-Unsubscribe": f"<{link}>"},
                ).send(fail_silently=False)
            except Exception:
                logger.exception(
                    "Could not tell %s that the list changed.",
                    subscription.email)
                continue
            sent += 1
    return sent


def suppress_addresses(addresses: Dict[str, str], reason: str = "",
                       user=None) -> Tuple[int, int]:
    """Add addresses to the never-email list. Returns (suppressed, removed).

    Also takes them off every list they are currently on: an address arriving
    here means "stop", and leaving a live subscription behind would mean the
    next mailing goes out to somebody we have just recorded as off-limits.
    """
    suppressed = removed = 0
    for email in addresses:
        email = normalize_email(email)
        if not email:
            continue
        _row, created = SuppressedAddress.objects.get_or_create(
            email=email, defaults={"reason": reason, "created_by": user})
        if created:
            suppressed += 1
        # iexact and user__email, because a subscription is either an address
        # or a site account, and django-newsletter does not normalise case.
        # A row this misses is one the next mailing goes out to, having just
        # recorded the address as off-limits.
        for subscription in Subscription.objects.filter(
                unsubscribed=False).filter(
                    Q(email_field__iexact=email)
                    | Q(user__email__iexact=email)):
            subscription.update("unsubscribe")
            removed += 1
    return (suppressed, removed)


# Column headings we accept, lower-cased. Between them these cover a Mailchimp
# export ("Email Address", "First Name", "Last Name") and a Google Forms one
# ("Email Address", "Name", plus a Timestamp nobody cares about), which are the
# two shapes this site actually gets.
EMAIL_HEADINGS = ("email", "e-mail", "mail")
NAME_HEADINGS = ("name", "full name", "first name", "given name")
SURNAME_HEADINGS = ("last name", "surname", "family name")

# Enough for any list this site will import, and small enough that a
# mis-uploaded video does not get read into the worker's memory first.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# Subscription.email_field's column width.
EMAIL_MAX_LENGTH = 254


def _decode(upload) -> str:
    raw = upload.read(MAX_UPLOAD_BYTES)
    if not isinstance(raw, bytes):
        return raw
    for encoding in ("utf-8-sig", "latin-1"):
        # utf-8-sig first because Excel writes a byte-order mark; latin-1
        # cannot fail, and a mangled accent in a name beats refusing the file.
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _heading_columns(row: List[str]) -> Optional[Dict[str, int]]:
    """Which column is the address and which are the name, or None if this is
    not a heading row (a file that is just a column of addresses has none)."""
    columns: Dict[str, int] = {}
    for index, cell in enumerate(row):
        heading = cell.strip().lower()
        if "email" not in columns and any(
                word in heading for word in EMAIL_HEADINGS):
            columns["email"] = index
        elif "surname" not in columns and any(
                word in heading for word in SURNAME_HEADINGS):
            # Before the name check, not after: every surname heading contains
            # the substring "name", so a "Surname" column ahead of a "Given
            # Name" one would otherwise claim the name slot and the given name
            # would be dropped. Mailchimp's First/Last order hides this.
            columns["surname"] = index
        elif "name" not in columns and any(
                word in heading for word in NAME_HEADINGS):
            columns["name"] = index
    return columns if "email" in columns else None


def parse_addresses(upload) -> Dict[str, str]:
    """Read {address: name} out of an uploaded CSV.

    Deliberately forgiving. These files come out of other people's tools, and
    a row this cannot make sense of is skipped rather than failing the upload:
    one bad line in a 900-line export should not mean importing nothing. What
    it will not do is guess at something that is not an email address.

    Nothing is filtered here -- not addresses already subscribed, not
    suppressed ones. Those decisions belong to the caller, because the
    suppression import wants exactly the addresses a subscriber import skips.
    """
    text = _decode(upload)
    if not text.strip():
        return {}
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        # One column and no delimiter in sight; comma is as good as anything.
        dialect = csv.get_dialect("excel")
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return {}

    columns = _heading_columns(rows[0])
    body = rows[1:] if columns else rows
    addresses: Dict[str, str] = {}
    for row in body:
        if columns:
            email = normalize_email(_cell(row, columns.get("email")))
            name = " ".join(
                part for part in (_cell(row, columns.get("name")),
                                  _cell(row, columns.get("surname")))
                if part)
            candidates = [(email, name)]
        else:
            # No headings: take any cell that is an address. Covers a
            # hand-pasted column, and a stray Timestamp column costs nothing.
            candidates = [(normalize_email(cell), "") for cell in row]
        for email, name in candidates:
            if not email or email in addresses:
                continue
            try:
                django_validate_email(email)
            except DjangoValidationError:
                continue
            if len(email) > EMAIL_MAX_LENGTH:
                # Valid but longer than the column. make_subscription().save()
                # does not full_clean(), so this would be a database error --
                # a 500 on the import page from one bad row in a CSV.
                logger.info("Skipping an address longer than %s characters.",
                            EMAIL_MAX_LENGTH)
                continue
            addresses[email] = name[:200]
    return addresses


def _cell(row: List[str], index: Optional[int]) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()
