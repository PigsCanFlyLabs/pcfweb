"""Digital fulfilment: resolving book archives and signing download links.

The archives are baked into the image at /opt/app/book-assets (see build.sh
and the Dockerfile), which is deliberately outside both nginx aliases in
conf/nginx.default -- /static and /media are the only paths nginx serves off
disk, so nothing here is publicly reachable. The only way to a file is
DigitalDownloadView, and the only way to that is a signed link.
"""
import logging
import re

from pathlib import Path

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.urls import reverse

logger = logging.getLogger(__name__)

# The archive naming contract, shared with the pcfweb-book-assets repo's
# README. Lowercase snake_case, at least two characters, and -- the point of
# the whole thing -- no dot, slash, backslash or space, so a stem can never
# name a directory, an extension or a parent.
ASSET_NAME_PATTERN = r"^[a-z0-9][a-z0-9_]*[a-z0-9]$"
ASSET_NAME_RE = re.compile(ASSET_NAME_PATTERN)

# One ZIP per book, holding the EPUB and the PDF. Appended in code and never
# read from the database, a request or Stripe.
ASSET_SUFFIX = ".zip"

# Namespaces the signature, so a token minted here can't be replayed against
# some other TimestampSigner in the app (or vice versa).
DOWNLOAD_TOKEN_SALT = "main.digital.download"


class DigitalAssetError(Exception):
    """A book archive could not be named or located. Never shown to a buyer."""


# django-stubs only knows Django's own settings, so every project-specific one
# needs the same escape hatch payments.py uses for STRIPE_API_KEY. Reading
# them through these three accessors keeps that to one place each.
def asset_root() -> Path:
    return Path(settings.BOOK_ASSET_ROOT).resolve()  # type: ignore[misc]


def link_lifetime_seconds() -> int:
    return int(settings.DIGITAL_DOWNLOAD_MAX_AGE)  # type: ignore[misc]


def link_lifetime_days() -> int:
    return link_lifetime_seconds() // 86400


def site_base_url() -> str:
    return str(settings.SITE_BASE_URL).rstrip("/")  # type: ignore[misc]


def resolve_asset_path(stem: str) -> Path:
    """Turn an admin-editable filename stem into a path that is safe to open.

    `Product.digital_asset_name` is typed into the Django admin, so it is
    hostile input: a stem of "../../etc/passwd" would otherwise become an
    arbitrary file read whose contents get emailed to a customer. Three
    separate things have to hold, and any one of them alone would do:

      1. the stem matches the naming contract, which admits no path syntax;
      2. the extension is appended here rather than accepted from anywhere;
      3. the resolved path still sits directly in the resolved asset root --
         which also catches a symlink in the assets directory pointing out of
         it, something the pattern check cannot see.

    Raises DigitalAssetError rather than returning something questionable.
    Existence is *not* checked here; see open_asset().
    """
    if not isinstance(stem, str) or not ASSET_NAME_RE.match(stem):
        raise DigitalAssetError(
            f"{stem!r} is not a usable digital asset name; it must match "
            f"{ASSET_NAME_PATTERN}")
    root = asset_root()
    path = (root / f"{stem}{ASSET_SUFFIX}").resolve()
    if not path.is_relative_to(root) or path.parent != root:
        # Unreachable through the pattern check above; kept because this is
        # the assertion that actually makes the read safe, and the pattern is
        # one edit away from being loosened.
        raise DigitalAssetError(
            f"{stem!r} resolves to {path}, which is outside the book asset "
            f"directory {root}")
    return path


def open_asset(stem: str):
    """Open a book archive for reading, or raise DigitalAssetError.

    The caller owns closing it (FileResponse does).
    """
    path = resolve_asset_path(stem)
    if not path.is_file():
        raise DigitalAssetError(
            f"the book archive {path.name} is missing from {path.parent}; it "
            "has to be added to the pcfweb-book-assets repository and the "
            "image rebuilt")
    try:
        return path.open("rb")
    except OSError as e:
        raise DigitalAssetError(f"could not read {path.name}: {e}") from e


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=DOWNLOAD_TOKEN_SALT)


def make_download_token(order_pk: int, product_pk: int) -> str:
    """Sign a link to one product of one order.

    Binding both halves is what stops a link from being useful anywhere but
    the order it was issued for; the view re-checks that the order really did
    buy that product, so a forged pairing gets nothing even if it were
    signable.
    """
    return _signer().sign(f"{order_pk}:{product_pk}")


def parse_download_token(token: str) -> tuple:
    """(order_pk, product_pk) from a token, or raise.

    Raises SignatureExpired past DIGITAL_DOWNLOAD_MAX_AGE and BadSignature for
    anything tampered with or malformed -- callers distinguish the two,
    because an expired link is a real customer needing a new one and a bad
    signature is not.
    """
    value = _signer().unsign(token, max_age=link_lifetime_seconds())
    order_part, _, product_part = value.partition(":")
    try:
        return int(order_part), int(product_part)
    except ValueError:
        # Correctly signed but not something this app minted.
        raise BadSignature(f"unusable download token payload {value!r}")


def download_path(order_pk: int, product_pk: int) -> str:
    return reverse('digital-download',
                   kwargs={'token': make_download_token(order_pk, product_pk)})


def download_url(order_pk: int, product_pk: int) -> str:
    """The absolute link emailed to a buyer.

    Absolute from settings rather than request.build_absolute_uri: this is
    built from the Stripe webhook, where the only Host header available
    belongs to Stripe's delivery, not to the site.
    """
    return f"{site_base_url()}{download_path(order_pk, product_pk)}"


__all__ = [
    "ASSET_NAME_PATTERN",
    "BadSignature",
    "DigitalAssetError",
    "SignatureExpired",
    "asset_root",
    "download_url",
    "link_lifetime_days",
    "link_lifetime_seconds",
    "make_download_token",
    "open_asset",
    "parse_download_token",
    "resolve_asset_path",
    "site_base_url",
]
