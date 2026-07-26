"""Bulk import of mailing list subscribers from a CSV.

Kept out of the views and the admin so the parsing -- which is where the
surprises are, because these files come out of other people's tools -- can be
tested on its own.
"""

import csv
import io
import logging

from dataclasses import dataclass, field
from typing import Any, IO, Dict, List, Optional, Tuple

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from main.models import InterestArea, MailingListSubscription

logger = logging.getLogger(__name__)

# Bigger than any list this site will plausibly import, and small enough that
# a mis-uploaded video does not get read into the worker's memory before we
# notice it is not a CSV.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# Column names accepted for each field, lower-cased and stripped. Mailchimp,
# Substack and a hand-made spreadsheet all name these differently, and asking
# the owner to rename columns before uploading is how an import gets skipped.
COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "email": ("email", "email address", "email_address", "e-mail", "mail",
              "email_field"),
    "name": ("name", "full name", "full_name", "first name", "first_name",
             "name_field"),
    "interest": ("interest", "interest area", "interest_area", "area",
                 "list", "topic"),
    "source": ("source", "origin", "signup source", "signup_source"),
    "status": ("status", "state", "subscribed"),
}

# Values in a status column, mapped onto our own. Anything else is a row
# error rather than a guess.
STATUS_VALUES = {
    "subscribed": MailingListSubscription.Status.SUBSCRIBED,
    "subscribe": MailingListSubscription.Status.SUBSCRIBED,
    "yes": MailingListSubscription.Status.SUBSCRIBED,
    "true": MailingListSubscription.Status.SUBSCRIBED,
    "1": MailingListSubscription.Status.SUBSCRIBED,
    "active": MailingListSubscription.Status.SUBSCRIBED,
    "pending": MailingListSubscription.Status.PENDING,
    "unconfirmed": MailingListSubscription.Status.PENDING,
    "unsubscribed": MailingListSubscription.Status.UNSUBSCRIBED,
    "no": MailingListSubscription.Status.UNSUBSCRIBED,
    "false": MailingListSubscription.Status.UNSUBSCRIBED,
    "0": MailingListSubscription.Status.UNSUBSCRIBED,
    "cleaned": MailingListSubscription.Status.UNSUBSCRIBED,
}


class CsvImportError(Exception):
    """The file as a whole could not be read; no row was imported."""


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # (row number as the owner sees it in their spreadsheet, what was wrong)
    errors: List[Tuple[int, str]] = field(default_factory=list)
    dry_run: bool = False

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged

    def summary(self) -> str:
        prefix = "Would import" if self.dry_run else "Imported"
        return (f"{prefix} {self.total} row(s): {self.created} new, "
                f"{self.updated} updated, {self.unchanged} already current, "
                f"{len(self.errors)} skipped.")


def decode(upload: IO[bytes]) -> str:
    """Read an uploaded file as text.

    utf-8-sig first because Excel writes a BOM; latin-1 as the fallback
    because it cannot fail, and a mangled accent in a name is a far better
    outcome than refusing the whole list.
    """
    raw = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise CsvImportError(
            f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)}MB. "
            "Split it up and import the pieces.")
    if not raw:
        raise CsvImportError("That file is empty.")
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvImportError("Could not read that file as text; is it a CSV?")


def _header_map(row: List[str]) -> Optional[Dict[str, int]]:
    """Map our field names onto column positions, or None if this is not a
    header row (a file that is just a column of addresses has none)."""
    lowered = [cell.strip().lower() for cell in row]
    mapping = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for index, cell in enumerate(lowered):
            if cell in aliases:
                mapping[field_name] = index
                break
    if "email" not in mapping:
        return None
    return mapping


def import_csv(upload: IO[bytes], interest: InterestArea,
               default_status: str = MailingListSubscription.Status.SUBSCRIBED,
               source: str = "", dry_run: bool = False) -> ImportResult:
    """Import subscribers from an uploaded CSV.

    Rows are imported one at a time and a bad row is reported rather than
    aborting the file: a single typo in a 900-line export should not mean
    importing nothing.

    The default status is SUBSCRIBED because an import is the owner asserting
    they already have consent for these addresses -- the double opt-in on the
    web signup is what covers the case where we do not.
    """
    text = decode(upload)
    dialect: Any
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        # One column and no delimiter in sight; comma is as good as anything.
        dialect = csv.get_dialect("excel")
    rows = list(csv.reader(io.StringIO(text), dialect))
    result = ImportResult(dry_run=dry_run)
    if not rows:
        raise CsvImportError("That file has no rows in it.")

    columns = _header_map(rows[0])
    if columns is None:
        # No header: treat every row as a bare address, which is what a
        # hand-pasted list looks like.
        columns = {"email": 0}
        body = rows
        first_row_number = 1
    else:
        body = rows[1:]
        first_row_number = 2
    if not body:
        raise CsvImportError(
            "That file has a header but no rows underneath it.")

    for offset, row in enumerate(body):
        row_number = first_row_number + offset
        if not any(cell.strip() for cell in row):
            continue
        try:
            _import_row(row, columns, interest, default_status,
                        source, dry_run, result)
        except ValueError as e:
            result.errors.append((row_number, str(e)))
    return result


def _cell(row: List[str], columns: Dict[str, int], field_name: str) -> str:
    index = columns.get(field_name)
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _import_row(row: List[str], columns: Dict[str, int],
                interest: InterestArea, default_status: str, source: str,
                dry_run: bool, result: ImportResult) -> None:
    email = MailingListSubscription.normalize_email(
        _cell(row, columns, "email"))
    if not email:
        raise ValueError("No email address in that row.")
    try:
        validate_email(email)
    except ValidationError:
        raise ValueError(f"{email!r} is not a valid email address.")

    status = default_status
    raw_status = _cell(row, columns, "status").lower()
    if raw_status:
        if raw_status not in STATUS_VALUES:
            raise ValueError(f"Unknown status {raw_status!r}.")
        status = STATUS_VALUES[raw_status]

    row_interest: InterestArea = interest
    named_interest = _cell(row, columns, "interest")
    if named_interest:
        # By slug first, then by name, so an export of our own admin list
        # view imports back without editing.
        named = (
            InterestArea.objects.filter(slug=named_interest).first()
            or InterestArea.objects.filter(name__iexact=named_interest).first())
        if named is None:
            raise ValueError(
                f"No interest area matches {named_interest!r}; create it "
                "first or leave the column out.")
        row_interest = named

    name = _cell(row, columns, "name")
    row_source = _cell(row, columns, "source") or source

    existing = MailingListSubscription.objects.filter(
        email=email, interest=row_interest).first()
    if existing is not None:
        # An import must never silently re-subscribe somebody who
        # unsubscribed -- that is the one change here with a legal shape to
        # it -- so leave those alone unless the file explicitly says so.
        if (existing.status == MailingListSubscription.Status.UNSUBSCRIBED
                and status != MailingListSubscription.Status.UNSUBSCRIBED):
            raise ValueError(
                f"{email} unsubscribed from {row_interest}; not re-adding "
                "them.")
        changed = existing.status != status or (name and not existing.name)
        if not changed:
            result.unchanged += 1
            return
        if not dry_run:
            if name and not existing.name:
                existing.name = name
            if status == MailingListSubscription.Status.SUBSCRIBED:
                existing.mark_subscribed()
            elif status == MailingListSubscription.Status.UNSUBSCRIBED:
                existing.unsubscribe()
            else:
                existing.status = status
                existing.save()
        result.updated += 1
        return

    if not dry_run:
        subscription = MailingListSubscription.objects.create(
            email=email, name=name, interest=row_interest, source=row_source,
            status=status)
        if status == MailingListSubscription.Status.SUBSCRIBED:
            # Goes through the model so the newsletter mirror happens; the
            # row is already SUBSCRIBED, so this only does the mirroring.
            subscription.mark_subscribed()
    result.created += 1
