import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, send_mail
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.db.models.functions import Coalesce, Lower, NullIf
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, linebreaks
from django.utils.safestring import mark_safe

from main.digital import (
    DigitalAssetError, download_url, link_lifetime_days, open_asset,
    site_base_url)
from main.payments import Payments
from main.socials import follow_targets
from main.utils import admin_recipients, normalize_email, smtp_connection
from typing import Any, Dict, List, Optional, Tuple, cast

from easy_thumbnails.files import get_thumbnailer

logger = logging.getLogger(__name__)

# Everything on this site is priced and charged in USD; it is hardcoded at
# every Stripe call site. Orders store it explicitly anyway so a historical
# order still says what it was actually charged in if that ever changes.
DEFAULT_CURRENCY = "usd"


def normalize_email_identity(email: str) -> str:
    """Return the canonical representation used for account identity."""
    return email.strip().casefold()


class EmailIdentity(models.Model):
    """Database-enforced ownership of a normalized signup email address.

    ``auth.User.email`` is not unique.  Keeping the reservation in this small
    table lets signup establish uniqueness before it creates the User without
    requiring a disruptive mid-project custom-user migration.
    """

    normalized_email = models.CharField(max_length=254)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="email_identity")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("normalized_email"), name="unique_normalized_email"),
        ]

    def save(self, *args, **kwargs):
        self.normalized_email = normalize_email_identity(
            self.normalized_email)
        super().save(*args, **kwargs)


class ProductGroup(models.Model):
    """One work, sold here as several SKUs.

    "Distributed Computing 4 Kids (and Executives)" is one book with three
    product rows: a paperback, the Executive Edition and an e-book. Each of
    those is a genuinely separate SKU -- its own ISBN, its own price, its own
    tax code, its own Stripe product -- and none of that changes here. What a
    group changes is only how they are *presented*: the catalogue shows the
    work once instead of three near-identical cards, and each product page
    offers the other formats, the way a retailer's page offers hardcover
    beside paperback beside e-book.

    Deliberately a model of its own rather than a shared slug column on
    Product. The group has one piece of state -- the name the listing shows --
    and a column repeated on every member is a column that can disagree with
    itself; the row that names the work is then whichever one you happen to
    read. A foreign key also makes "these three are the same book" a fact the
    database enforces rather than a string convention.

    Distinct from ``Product.x_links``, which stays what it was: a free-form
    "see also" between rows that need not be the same work at all (a first
    edition and a second, say). A group is the stronger statement -- one
    work, several formats -- and it is the only one of the two that collapses
    a listing. Nothing here supersedes x_links, and a grouped product's
    x_links to rows OUTSIDE its group are still rendered; see
    ``Product.get_other_edition_links``.
    """

    name = models.CharField(
        max_length=250,
        help_text=(
            "The work's title, shown on the single card that stands for the "
            "whole group in listings. Not a format name -- those live on "
            "each member's format_label."
        ),
    )

    def members(self) -> List["Product"]:
        """Every SKU in this group, in the owner's chosen format order.

        ``format_order`` then pk -- ``Product.format_rank()``, the one
        ordering used everywhere a group is rendered: the format chooser on a
        product page, the format summary on a listing card, and the choice of
        which member represents the group in a collapsed listing. Reading it
        from one method means those three can never disagree about which
        format comes first.

        Sorted in Python off ``.all()`` rather than by the database, and that
        is the whole reason this returns a list. A listing prefetches
        ``group__products`` so that N cards cost one extra query between them;
        ``.order_by()`` on a related manager builds a NEW queryset, which
        cannot use that prefetched cache and silently issues a query per card
        instead -- the prefetch still runs, and its results are simply thrown
        away. The sort is over a handful of rows either way.
        """
        return sorted(self.products.all(), key=lambda p: p.format_rank())

    def __str__(self) -> str:
        return f'{self.name}'

    def __repr__(self) -> str:
        return f'<ProductGroup: {self.name}>'


# Create your models here.
class ProductQuerySet(models.QuerySet):
    """Custom QuerySet for Product."""

    def order_by_release_date(self):
        """Publication-date ordering shared by every product listing.

        Newest first (release_date DESC NULLS LAST), with pk ASC as the
        stable tiebreaker.  Keep the three call sites in sync by referencing
        this one helper rather than repeating the expression inline.
        """
        return self.order_by(
            models.F('release_date').desc(nulls_last=True), 'pk')

    def collapse_format_groups(self) -> List["Product"]:
        """One entry per work: grouped SKUs collapse to a single member.

        Returns a list rather than a QuerySet, and that is not laziness about
        SQL. "Keep one row per group, preferring the member the owner ordered
        first, but fall back to whichever members are actually in *this*
        queryset" is a rule about the rows that survived filtering, and the
        fallback half is the reason: a category page, or the
        ``exclude(noorder=True)`` every listing applies, can perfectly well
        remove the preferred member. A DISTINCT ON would then drop the whole
        work off the page rather than showing the format that is still there.

        Ungrouped products -- which is most of the catalogue -- pass through
        untouched and one-for-one.

        Ordering is inherited, never imposed: each group takes the position of
        its first surviving member in whatever order the caller established
        (``order_by_release_date`` at every current call site), so collapsing
        can move a work up the page only by removing its duplicates, never by
        reordering what is left. The member DISPLAYED is a separate question
        from the position, and is decided by ``format_order`` -- so the
        paperback can represent the work while the e-book, seeded first by
        release date, decides where the card sits.
        """
        # select_related keeps the group name a join rather than a query per
        # card; prefetch_related does the same for the format summary each
        # card renders, which otherwise walks back to the group's members.
        queryset = self.select_related("group").prefetch_related(
            "group__products")

        collapsed: List["Product"] = []
        # group id -> index into `collapsed`, so a later member can replace
        # the row standing for its group without moving the group's position.
        positions: Dict[int, int] = {}

        for product in queryset:
            if product.group_id is None:
                collapsed.append(product)
                continue
            position = positions.get(product.group_id)
            if position is None:
                positions[product.group_id] = len(collapsed)
                collapsed.append(product)
            elif product.format_rank() < collapsed[position].format_rank():
                collapsed[position] = product
        return collapsed


class Product(models.Model):
    objects = ProductQuerySet.as_manager()

    description = models.TextField(default="No description.")
    external_product_id = models.CharField(
        max_length=250, blank=True, null=True)
    product_id = models.AutoField(primary_key=True)
    # Deprecated rolling-deploy compatibility column. Follow-up PR removes it
    # after print_isbn has been fully deployed and old pods no longer read it.
    isbn = models.CharField(max_length=20, blank=True, null=True)
    print_isbn = models.CharField(max_length=20, blank=True, null=True)
    ebook_isbn = models.CharField(max_length=20, blank=True, null=True)
    upc = models.CharField(max_length=20, blank=True, null=True)
    mpn = models.CharField(max_length=100, blank=True, null=True)
    kickstarter = models.CharField(max_length=200, blank=True, null=True)
    kindle_link = models.CharField(max_length=200, blank=True, null=True)
    amazon_link = models.CharField(max_length=200, blank=True, null=True)
    default_asin = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text=(
            "Print/catalogue ASIN fallback for Amazon print links; not used "
            "for Kindle."
        ),
    )
    print_asin = models.CharField(max_length=20, blank=True, null=True)
    ebook_asin = models.CharField(max_length=20, blank=True, null=True)
    bookshop_link = models.CharField(max_length=250, blank=True, null=True)
    # A sibling field rather than a reuse of bookshop_link: the print link's
    # label is format-neutral, and letting one field mean "print" on some rows
    # and "e-book" on others would make the button lie on half the catalogue.
    # Only two of the eight titles have a Bookshop e-book listing -- coverage
    # is publisher-gated, so this is set per-row from a verified URL and never
    # derived from an ISBN.
    bookshop_ebook_link = models.CharField(max_length=250, blank=True, null=True)
    # Shown to visitors detected as being in India.
    amazon_in_link = models.CharField(max_length=250, blank=True, null=True)
    flipkart_link = models.CharField(max_length=250, blank=True, null=True)
    preorder_only = models.BooleanField(default=False, null=False)
    noorder = models.BooleanField(default=False, null=False)
    backorder = models.BooleanField(default=False, null=False)
    # Advisory ONLY, and deliberately not a fourth member of the three flags
    # above. Those gate ordering; this one gates a sentence of copy and
    # nothing else. An older edition that still sells is still sold, so this
    # is read by the product page and by nothing in is_purchasable(),
    # get_availability(), buy_text(), stock_description() or the Merchant
    # feed -- setting it cannot remove a buy button or drop a row out of the
    # catalogue. If you ever want "out of date" to also mean "stop selling
    # it", that flag already exists and is called `noorder`; set that one and
    # leave this meaning what it says. Pinned by
    # OutOfDateIsAdvisoryOnlyTest, which asserts the *absence* of any effect.
    #
    # db_default as well as default, for the rolling-deploy reason spelled
    # out on delivery_type below.
    out_of_date = models.BooleanField(default=False, db_default=False)
    stock = models.PositiveIntegerField(
        default=0,
        db_default=0,
        help_text=(
            "Manually gates whether physical books can be purchased here; "
            "it does not cap order quantity or decrement automatically."
        ),
    )
    date_available = models.DateField(null=True, blank=True)
    release_date = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "The date this product was first published/released. "
            "NULLable so an old pod's INSERT that omits it lands "
            "NULL safely during a rolling deploy. "
            "DISTINCT from date_available (when it can be bought): "
            "this is when it actually came out."
        ),
    )
    brand = models.CharField(null=True, blank=True, max_length=200)
    sizes = models.CharField(null=True, blank=True, max_length=200)

    def generate_external_product_id(self):
        external_product_id = Payments.create_product(
            self.name, self.description, self.price, currency="usd", tax_code=self.tax_code)
        return external_product_id

    def save(self, *args, **kwargs):
        if not self.external_product_id:
            self.external_product_id = self.generate_external_product_id()
        super().save(*args, **kwargs)

    def ensure_external_product_id(self) -> str:
        """Create and persist the Stripe product id if it's missing.

        Fixture rows bypass save() (loaddata uses raw saves), so consumers
        that need the Stripe id call this before using it.
        """
        if not self.external_product_id:
            self.save()
        assert self.external_product_id
        return self.external_product_id

    class Modes(models.TextChoices):
        PAYMENT = 'P', 'payment'
        SUBSCRIPTION = 'S', 'subscription'

    class DeliveryTypes(models.TextChoices):
        """How a product reaches the buyer.

        Deliberately a separate axis from Modes: that one is about how Stripe
        bills (one-off vs recurring), this one is about what has to happen
        afterwards. Inferring one from the other is what used to offer media
        mail on a PDF -- see is_physical_good().
        """
        PHYSICAL = "PHYSICAL", "Physical"
        DIGITAL = "DIGITAL", "Digital"
        SERVICE = "SERVICE", "Service"

    class TaxTypes(models.TextChoices):
        # See https://stripe.com/docs/tax/tax-categories
        GOODS = 'txcd_99999999', 'Goods'
        SERVICES = 'txcd_20030000', 'Services'
        HOSTING = 'txcd_10701100', 'Hosting'
        PHONES = 'txcd_34021000', 'Phones'
        BOOKS = 'txcd_35010000', 'Books'  # Physical books
        # Digital Books - downloaded - non-subscription - with permanent
        # rights. Distinct from BOOKS: a downloaded book is taxed differently
        # from a printed one in a lot of US states, so reusing the physical
        # code would file the wrong tax.
        DIGITAL_BOOKS = 'txcd_10302000', 'Digital Books (downloaded)'
        ROUTERS = 'txcd_34040014', 'Routers'
        ELECTRONICS = 'txcd_34020027', 'Consumer Electronics'

    class Categories(models.TextChoices):
        BOOKS = 'B', "Books"
        SERVICES = 'S', "Services"
        ELECTRONICS = 'E', "Electronics"
        OTHER_ELECTRONICS = 'OE', "Other Electronics"
        MERCH = 'M', "Merch"

    name = models.CharField(max_length=250)
    page = models.CharField(
        max_length=250,
        blank=True)
    price = models.IntegerField(default=0)
    # Manufacturer's suggested retail price, in the same units as `price`
    # above: integer cents. A separate column rather than anything derived,
    # because an MSRP is a publisher's number and not a function of ours.
    #
    # NULL means "no MSRP recorded", which is not the same as zero -- an MSRP
    # of 0 would be a real (if odd) claim, whereas most rows simply have no
    # such figure. That distinction is why this is nullable rather than
    # defaulted to 0, and it is also what keeps the strikethrough off every
    # row in the catalogue by default: see show_msrp().
    #
    # Nullable also makes it safe for a rolling deploy -- an INSERT from a
    # pod that has never heard of this column omits it and gets NULL. It is
    # deliberately absent from the OLD_CODE_PRODUCT_COLUMNS lists in
    # test_schema/test_stock for exactly that reason.
    msrp = models.IntegerField(
        null=True,
        blank=True,
        help_text=(
            "Suggested retail price in cents, same units as price. Leave "
            "empty when there is no MSRP; it is shown struck through beside "
            "the price only when it is strictly greater than the price."
        ),
    )
    image = models.ImageField(
        upload_to='data_here',
        blank=True)
    image_name = models.CharField(max_length=250, default="", blank=True)
    tax_code = models.CharField(
        max_length=20,
        choices=TaxTypes.choices,
        default=TaxTypes.GOODS)
    cat = models.CharField(
        max_length=2,
        choices=Categories.choices,
        default=Categories.MERCH)
    mode = models.CharField(
        max_length=1,
        choices=Modes.choices,
        default=Modes.PAYMENT)
    # db_default as well as default, on this and the four below. A rolling
    # deploy migrates while the old image is still serving, and Django's
    # AddField backfills a default and then drops it out of the schema -- so
    # without a database-side default these NOT NULL columns cannot be omitted
    # from an INSERT, and old code omits exactly them. See
    # RollingDeployOldCodeWriteTest.
    delivery_type = models.CharField(
        max_length=20,
        choices=DeliveryTypes.choices,
        default=DeliveryTypes.PHYSICAL,
        db_default=DeliveryTypes.PHYSICAL)

    # "We are licensed to distribute this file ourselves." Not a feature flag:
    # it is the interlock that stops a mis-set delivery_type dropdown from
    # emailing a book somebody else holds the distribution rights to. The
    # O'Reilly titles are Holden's writing but O'Reilly's to hand out, so this
    # defaults to False and they stay False.
    sells_ebook = models.BooleanField(default=False, db_default=False)

    # "This title is on O'Reilly's learning platform." Drives the Safari link
    # in get_alt_links(), which used to be gated on `isbn` being set -- an
    # inference that held only for as long as every book here was an O'Reilly
    # book. A self-published title with an ISBN would have advertised a free
    # trial of a platform it is not on, which is a false claim to a customer
    # rather than merely a dead link. Defaults False so the failure mode of a
    # forgotten flag is a missing link, not an invented one.
    on_oreilly_safari = models.BooleanField(default=False, db_default=False)

    # Pay-what-you-want. Turns Product.price into a *suggestion*: the Stripe
    # Price is minted with custom_unit_amount instead of a fixed unit_amount,
    # and the buyer types their own number (including zero).
    is_pwyw = models.BooleanField(default=False, db_default=False)

    # Filename stem of the downloadable archive, without directory or
    # extension: the file served is <digital_asset_name>.zip under
    # settings.BOOK_ASSET_ROOT. An explicit field rather than something
    # derived from `name`, so renaming a book in the admin cannot silently
    # break fulfilment. Admin-editable, therefore never trusted -- see
    # main.digital.resolve_asset_path.
    digital_asset_name = models.CharField(
        max_length=100, blank=True, db_default="")

    # Related editions and formats of the same work: the print edition, the
    # executive edition and the e-book of one title all point at each other.
    #
    # Symmetrical, which is Django's default for a self-M2M and is the right
    # default here rather than an accident of it: "is another edition of" is
    # a mutual statement, and a one-directional version would let the print
    # page list the e-book while the e-book page listed nothing. It also
    # halves the ways the seeded graph can be wrong -- adding one side adds
    # the other, so the two can never disagree.
    #
    # Purely navigational. get_cross_links() turns these into links to the
    # other product's own page; none of them is a buy affordance, which is
    # why (unlike the e-book links below) they are not gated on
    # is_purchasable(). pk 107 exists precisely to be pointed at while not
    # being for sale, and a "this also exists in hardback" pointer stays true
    # for a format we do not stock.
    # No related_name: Django overrides it to a hidden one for a symmetrical
    # self-M2M anyway, since the forward accessor already reaches both sides.
    x_links: "models.ManyToManyField[Product, Any]" = models.ManyToManyField(
        "self", blank=True)

    # The e-book cross-link, deliberately a second and separate M2M rather
    # than a filtered view over x_links above.
    #
    # x_links answers "what else is this book?"; this one answers "where is
    # the e-book you can buy?", and the two are different questions with
    # different rules. Only this one is directional -- symmetrical=False,
    # because a print row points at an e-book row and the reverse is
    # meaningless: an e-book is not its own e-book edition, and a symmetrical
    # version would make pk 106 render a "get the e-book" button pointing at
    # the paperback. Only this one is gated on is_purchasable(). Collapsing
    # them into one field would mean either the navigation list inherits the
    # purchase gate or the buy button loses it.
    ebook_x_links: "models.ManyToManyField[Product, Any]" = (
        models.ManyToManyField(
            "self", symmetrical=False, blank=True,
            related_name="print_x_links"))

    # The work this SKU is a format of. See ProductGroup for what a group is
    # and what it deliberately is not.
    #
    # NULL means "this product stands alone", which is most of the catalogue
    # and is the correct default for anything added later without a thought
    # about grouping: an ungrouped row lists and renders exactly as it did
    # before groups existed. Nullable also makes the column safe for a rolling
    # deploy, the same argument spelled out on `msrp` -- an INSERT from a pod
    # that has never heard of this column omits it and lands NULL.
    #
    # SET_NULL rather than CASCADE, and the difference is a real one: deleting
    # a group is a statement about presentation ("stop showing these as one
    # work"), and CASCADE would turn it into a statement about the catalogue
    # ("delete the three books"). PROTECT would be defensible; SET_NULL is
    # kinder to the admin and loses nothing that cannot be re-entered.
    group = models.ForeignKey(
        ProductGroup,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products",
        help_text=(
            "Optional. Other formats of the same work; grouped products "
            "share one card in listings and offer each other as format "
            "choices."
        ),
    )

    # What this SKU is, as a format: "Paperback", "Executive Edition",
    # "E-book". Shown on the format chooser, where the row is one of several
    # for the same book and the thing the reader is choosing between is
    # exactly this.
    #
    # Its own field rather than anything derived from `name` or
    # `delivery_type`. Deriving it from the name would mean parsing an em-dash
    # suffix out of admin-entered copy; deriving it from delivery_type can
    # tell PHYSICAL from DIGITAL and therefore cannot tell the paperback from
    # the Executive Edition, which is the one distinction the DC4K chooser
    # exists to draw. Blank falls back to the product's own name -- see
    # get_format_label() -- so a member added without one loses a tidy label
    # rather than rendering an empty button.
    # `default` as well as `db_default`, and not redundantly: db_default
    # alone leaves an UNSAVED instance's attribute holding Django's
    # DatabaseDefault sentinel rather than "". The sentinel is truthy, so
    # `self.format_label or self.name` in get_format_label() would return the
    # sentinel object itself and render it into the page. format_order below
    # declares both for the same reason.
    format_label = models.CharField(
        max_length=80,
        blank=True,
        default="",
        db_default="",
        help_text=(
            "How this format is named on the chooser: \"Paperback\", "
            "\"E-book\". Only meaningful when the product is in a group; "
            "falls back to the product name."
        ),
    )

    # Where this SKU sits among its group's formats, low first. Also decides
    # which member represents the group in a collapsed listing -- see
    # ProductQuerySet.collapse_format_groups -- so the first format is
    # deliberately "the one to show somebody who has not chosen yet" rather
    # than merely the top of a list.
    #
    # An explicit small integer with a db_default, not an ordering inferred
    # from price or from pk. Price ordering would reshuffle the chooser every
    # time a price changed, and pk ordering would make it an accident of which
    # SKU was entered first. Ties fall back to pk, so leaving every member at
    # 0 is a legitimate choice and simply means "creation order".
    format_order = models.IntegerField(
        default=0,
        db_default=0,
        help_text=(
            "Order within the group, lowest first. The lowest-ordered "
            "member is the one a collapsed listing shows."
        ),
    )

    def get_display_price(self) -> str:
        formatted_price = "{0:.2f}".format(self.price / 100)
        if self.preorder_only:
            return f"Pre-order: {formatted_price}"
        else:
            return formatted_price

    def pwyw_suggested_cents(self) -> int:
        """What this row suggests the buyer pay, in cents.

        `price` today, and the one place that equivalence is written down.
        Everything that quotes a suggestion goes through here -- the amount
        input's pre-fill, the chooser's "suggested 12.99" note, the low end
        of a listing card's price range -- so if a suggestion ever stops
        being the price (its own column, a per-format rule) those three move
        together instead of two of them quietly carrying on quoting `price`.
        """
        return self.price

    def pwyw_suggested_amount(self) -> str:
        """The suggested amount as a bare number, for the amount input.

        Its own accessor rather than get_display_price(), for the same reason
        get_msrp_display() is: that method returns human copy and prefixes
        "Pre-order: " on preorder rows, which a number input cannot hold and
        which the running total already parseFloats into NaN.
        """
        return _display_amount(self.pwyw_suggested_cents())

    def pwyw_charged_amount(self) -> str:
        """What the suggested amount would actually be charged at.

        The same number the buyer sees on first paint and with JavaScript
        off. It goes through round_pwyw_amount, so if an owner ever sets a
        suggestion below the band this says $0.00 rather than quoting
        something that will not be billed.
        """
        return "{0:.2f}".format(round_pwyw_amount(self.price) / 100)

    def get_msrp_display(self) -> str:
        """The MSRP as a bare formatted amount, for the strikethrough.

        Bare on purpose, and this is the one thing not to "tidy" into
        get_display_price(): that method returns human copy, prefixing
        "Pre-order: " on preorder rows, and single-product.html feeds it to
        a parseFloat() in the running-total script. Anything that goes near
        that path has to stay a number. This is only ever rendered as static
        markup, but keeping the two formatters separate is what guarantees a
        later edit cannot make MSRP the second string that arrives at
        parseFloat as NaN.

        Only meaningful when show_msrp() is true; callers must ask that
        first, since an unset MSRP has nothing to format.
        """
        return "{0:.2f}".format(cast(int, self.msrp) / 100)

    def show_msrp(self) -> bool:
        """Whether to strike an MSRP through beside the price.

        Strictly greater, never equal: an MSRP that matches the price is not
        a saving, and rendering "39.99 ~~39.99~~" is at best noise and at
        worst reads as a discount that is not being given. Lower is a
        stronger no -- striking through a number *below* what we charge
        advertises the opposite of a deal.

        An unset MSRP (NULL) is simply absent, which is the common case; it
        short-circuits before the comparison so that None never reaches `>`.
        """
        return self.msrp is not None and self.msrp > self.price

    # The advisory sentence for `out_of_date`, named here rather than written
    # inline in the template so the tests assert against the source of truth
    # instead of a second copy of the words -- the same reason
    # AMAZON_EBOOK_LABEL below is a constant. Note what it does not say: it
    # does not say the book is unavailable, because it is still for sale.
    OUT_OF_DATE_NOTICE = (
        "This edition may be out of date. It remains available here for "
        "historical purposes."
    )

    def get_absolute_url(self) -> str:
        # The route is product/<int:pk>, so the kwarg has to be pk.
        return reverse('product', kwargs={'pk': self.pk})

    def get_image_url(self) -> Optional[str]:
        try:
            return self.image.url
        except Exception:
            if self.image_name:
                return static(f"assets/images/{self.image_name}")
            else:
                return None

    # Google accepts at most 10 additional images per offer and drops the
    # whole item on more, so the cap is enforced here rather than trusted to
    # whoever is filling the admin in.
    MAX_ADDITIONAL_IMAGES = 10

    def get_additional_image_urls(self) -> List[str]:
        """Extra pictures, for the feed's additional_image_link.

        Never includes the primary image. Google rejects an offer whose
        additional_image_link repeats its image_link, and the duplicate is
        easy to create by accident: attaching a book's own cover as an extra
        image is exactly what somebody filling in the admin would try first.

        Rows that resolve to nothing are dropped rather than yielding an empty
        <g:additional_image_link>, which Google reads as a malformed URL and
        rejects the item over.
        """
        primary = self.get_image_url()
        urls: List[str] = []
        for extra in self.extra_images.all():
            url = extra.get_image_url()
            if not url or url == primary or url in urls:
                continue
            urls.append(url)
            if len(urls) >= self.MAX_ADDITIONAL_IMAGES:
                break
        return urls

    # The one place the cover thumbnail size is written down. Named rather than
    # inline because it is no longer used only here: the build pre-generates
    # every one of these into STATIC_ROOT (see
    # main/management/commands/pregenerate_thumbnails.py) so that no pod ever
    # has to generate one at request time, and a second copy of the numbers
    # would let the pre-generated file and the requested file drift into
    # different names -- which reintroduces exactly the runtime generation the
    # pre-generation exists to remove, silently.
    THUMB_SIZE = (290, 380)

    @staticmethod
    def static_thumbnailer(static_path: str):
        """Thumbnailer for a source that lives in the static tree.

        Takes a path relative to STATIC_ROOT -- the same thing
        ``{% static_thumbnail %}`` takes -- not a bare cover name, so the
        build-time pre-generator can drive it for the template-declared
        sources (the masthead logo) as well as for product covers.

        Split out of get_thumb() so that pre-generator resolves the source
        exactly the way a request does, off the same STATIC_ROOT-rooted
        storage, instead of reimplementing the path join and generating a file
        under a name nothing ever asks for.
        """
        from static_thumbnails.templatetags.static_thumbnails import (
            static_storage)
        return get_thumbnailer(static_storage, relative_name=static_path)

    def get_thumb(self):
        t = None
        try:
            if self.image_name:
                t = self.static_thumbnailer(
                    f"assets/images/{self.image_name}")
            else:
                t = get_thumbnailer(self.image)
        except Exception as e:
            logger.warning(f"Got exception {e} trying to load thumbnailer.")
            return self.get_image_url()
        try:
            th = t.get_thumbnail({'size': self.THUMB_SIZE})
            return th.url
        except Exception as e:
            logger.warning(f"Error generating thumbnail: {e}")
            return self.get_image_url()

    def __str__(self) -> str:
        return f'{self.name}'

    def __repr__(self) -> str:
        return f'<Product: {self.name}>'

    # One Commission Junction click-through to O'Reilly's platform, not a
    # per-title product page -- which is why on_oreilly_safari is a flag and
    # this is a constant, rather than a per-row URL field like amazon_link.
    # Four rows holding four copies of one affiliate id is four chances for
    # them to drift apart.
    OREILLY_SAFARI_URL = "https://www.tkqlhce.com/click-7645222-14045081"

    # The button's user-visible text, named here rather than written inline in
    # get_alt_links() below so tests can assert against the source of truth
    # instead of a copy. It has already been renamed once -- it read "Buy on
    # Kindle (e-book)" before the storefront naming in #36 -- and each copy of
    # the string is somewhere the next rename can be missed while the suite
    # still passes.
    AMAZON_EBOOK_LABEL = "Buy on Amazon (ebook)"

    def get_alt_links(self, country: Optional[str] = None):
        candidates = []
        if country == "IN":
            candidates += [
                ("Buy on Amazon.in (print)", self.get_amazon_in_link()),
                ("Buy on Flipkart (print)", self.flipkart_link),
            ]
        candidates += [
            ("Buy on Amazon (print)", self.get_amazon_link()),
            ("Buy on Bookshop.org (support local bookstores)",
             self.bookshop_link),
            ("Buy the e-book on Bookshop.org (DRM-free)",
             self.bookshop_ebook_link),
            ("Read on O'Reilly Safari (free trial)",
             # Explicit flag, not an ISBN inference. The DC4K SKUs are
             # self-published and carry real print ISBNs, so keying this off
             # print_isbn would advertise an O'Reilly free trial for a book
             # that is not on the platform. The flag also fails safe: a new
             # title added without it loses a link rather than inventing one.
             self.OREILLY_SAFARI_URL if self.on_oreilly_safari else None),
            # Amazon's e-book store is Kindle, so this is one button, not two:
            # a second "Buy on Amazon (ebook)" alongside a "Buy on Kindle" one
            # would be two labels pointing at the identical /dp/<ebook_asin>
            # URL. Named for the storefront the customer recognises rather than
            # for the file format they get.
            #
            # Gated on data, not on on_oreilly_safari above: the ASIN itself is
            # the per-title claim that the book is on Amazon, so an absent one
            # is already the fail-safe. Adding a publisher flag on top would
            # discard a correct owner-entered URL -- see AmazonEbookLinkTest.
            (self.AMAZON_EBOOK_LABEL, self.get_kindle_link()),
            ("Follow along on Kickstarter", self.kickstarter),
        ]
        return [(label, url) for label, url in candidates if url]

    def get_cross_links(self) -> List[Tuple[str, str]]:
        """Sibling editions and formats, as (name, product page URL) pairs.

        Navigation, not commerce: every URL here is another row's own product
        page on this site, so following one lands the visitor on a page that
        states that product's name, price and availability for itself. That
        is why there is no is_purchasable() gate -- pointing at a page is not
        offering to sell anything, and a title we list only for the history
        (pk 107) is still a true answer to "what other editions exist".

        Ordered by pk so the list a page renders is stable between requests
        and between deploys; M2M querysets are otherwise unordered.
        """
        return [
            (sibling.name, sibling.get_absolute_url())
            # A symmetrical M2M will happily store a row pointing at itself
            # if something adds one, and "also available as: itself" is
            # nonsense rather than merely untidy.
            for sibling in self.x_links.order_by("pk")
            if sibling.pk != self.pk
        ]

    # The prefix for the e-book cross-link button. Says "get", not "add to
    # cart": the button navigates to the e-book's own page rather than
    # putting anything in the cart, and a label promising an add-to-cart that
    # does not happen is the same class of lie as a mislabelled one that does.
    EBOOK_CROSS_LINK_PREFIX = "Get the e-book"

    def get_ebook_cross_links(self) -> List[Tuple[str, str]]:
        """The e-book buy affordance for a print row, as (label, URL) pairs.

        Every URL is the linked e-book's OWN product page, and the label
        names that product. Both halves are deliberate, and the reason is
        that this button renders on a *different* product's page:

        * It is not an add-to-cart. A form on the pk 104 page that posted an
          add-to-cart would either add pk 104 -- selling a paperback as a
          download -- or quietly add pk 106, a product the visitor never
          named. Navigating to pk 106's page instead means the add-to-cart
          the visitor eventually presses is on the page of the thing they
          are buying, with its own name, price and pay-what-you-want notice
          in front of them.
        * The label carries the target's name for the same reason: "Get the
          e-book" alone, on a page titled with the print edition, does not
          tell the visitor which row they are about to land on.

        Gated on the target's is_purchasable(), which get_alt_links() above
        notably is not -- see the module note on pk 107, where an ungated
        Amazon button renders on a page that says the book is not sold here.
        That hole predates this method and is not propagated into it: an
        e-book that is not purchasable produces no button at all rather than
        a button onto a page with no way to buy.

        An e-book in this row's own FORMAT GROUP produces no button either,
        for the same reason get_other_edition_links() filters x_links: the
        format chooser already offers that page, with more said about it --
        its format label, its price column, its pay-what-you-want note -- so
        a second button underneath would offer the identical destination
        twice with less information the second time. The button survives
        exactly where the chooser cannot replace it: an ebook_x_link to a
        row OUTSIDE the group (or on an ungrouped product, where there is no
        chooser at all).
        """
        links = []
        for ebook in self.ebook_x_links.order_by("pk"):
            # Defensive rather than expected: seed_products rejects a
            # self-reference, but the admin will happily create one, and
            # "get the e-book" pointing at the page you are already on is
            # a dead end.
            if ebook.pk == self.pk:
                continue
            if (self.group_id is not None
                    and ebook.group_id == self.group_id):
                continue
            if not ebook.is_purchasable():
                continue
            links.append(
                (f"{self.EBOOK_CROSS_LINK_PREFIX}: {ebook.name}",
                 ebook.get_absolute_url()))
        return links

    # ------------------------------------------------------------------
    # Format groups. See ProductGroup, and collapse_format_groups() above.
    # ------------------------------------------------------------------

    # The chooser's heading and the marker on the row you are already on.
    # Named here rather than written into the template for the same reason
    # AMAZON_EBOOK_LABEL and OUT_OF_DATE_NOTICE are: the tests assert against
    # the source of truth instead of against a second copy of the words.
    FORMAT_CHOOSER_HEADING = "Choose your format"
    FORMAT_CURRENT_MARKER = "You are viewing this format"
    # What the price column says instead of a number, for the two rows where
    # a bare figure would be a false claim: a product we do not sell has no
    # price here, and a pay-what-you-want price is a suggestion.
    FORMAT_NOT_SOLD_HERE = "Not sold here"
    FORMAT_PWYW_PRICE = "Pay what you want"
    # How a pay-what-you-want format is flagged in a listing card's format
    # summary. See listing_format_labels() for why that flag has to be there.
    PWYW_FORMAT_SUFFIX = "pay what you want"

    # The same ordering as format_rank() below, spelled as field names for
    # the places that have to ask the database for it rather than sort in
    # Python -- the admin inline that exists to arrange this very order. Two
    # spellings of one rule, kept adjacent so a third component added to the
    # rank is added here too.
    FORMAT_RANK_ORDERING = ("format_order", "pk")

    def format_rank(self) -> Tuple[int, int]:
        """This SKU's position among its group's formats.

        ``(format_order, pk)`` -- the same key ``ProductGroup.members()``
        sorts by, as a value the collapse in ProductQuerySet can compare
        without a second query. pk breaks ties so the answer is total and
        stable rather than dependent on row order.
        """
        return (self.format_order, self.pk)

    def get_format_label(self) -> str:
        """How this SKU is named on the format chooser.

        Falls back to the product's own name, which is never empty, so a
        group member somebody forgot to label renders a long button rather
        than a blank one. A blank button is unclickable-looking and says
        nothing about where it goes; a long one is merely untidy.
        """
        return self.format_label or self.name

    # Per-instance memo for group_members() below. A class attribute holding
    # an immutable None rather than an __init__ override: assigning through
    # self creates an instance attribute, so no two products can ever share
    # a list. Deliberately NOT a django cached_property -- those store under
    # the method's own name, and this has to stay callable from a template.
    _group_members_cache: Optional[List["Product"]] = None

    def group_members(self) -> List["Product"]:
        """Every SKU of this work, this one included, in format order.

        Empty -- not ``[self]`` -- for an ungrouped product. Callers use the
        emptiness to mean "there is no choice of format to offer here", and a
        one-element list would make every ungrouped page render a chooser
        offering the page you are already on.

        Memoised on the instance, because a grouped product page asks twice
        over -- once for the chooser and once to filter the "other editions"
        list -- and each ask was evaluating ``group.products.all()`` again.
        On a listing the prefetch already serves both; on a product page,
        which has no prefetch, this is what keeps the second ask free. The
        cache lives on one request's instance and dies with it, so it cannot
        serve a stale member list to a later request.
        """
        if self._group_members_cache is None:
            # `self.group`, not `self.group_id`: on a nullable FK the
            # descriptor returns None without a query when the column is
            # NULL, so this costs no more than the id check and it narrows
            # the type for the caller below.
            group = self.group
            self._group_members_cache = (
                [] if group is None else group.members())
        return self._group_members_cache

    def listed_group_members(self) -> List["Product"]:
        """The formats a listing card actually stands for.

        Group members minus the ones that are not sold here. Every
        group-level statement a card makes is computed over THIS list rather
        than over group_members(): the format summary, the price range, the
        published year and the pay-what-you-want notice. That is the point of
        it being one method -- those four appear on the same card inches
        apart, and a reader compares them. "Available in 3 formats" beside a
        range spanning two of them is a self-contradiction that no individual
        method's tests would catch, because each one would still be right
        about its own rule.

        A ``noorder`` row is excluded because the card renders this as
        "Available in N formats" and a format that is not sold here is not
        one of them; its price is also meaningless (pk 107's is literally 0)
        and would drag a range down to 0.00. The chooser on the product page
        does list such a format, which is not a contradiction -- the card
        advertises what can be bought, and the chooser answers what exists,
        saying "Not sold here" beside the one that cannot.

        Empty for an ungrouped product, since group_members() is.
        """
        return [member for member in self.group_members()
                if not member.noorder]

    def get_format_options(self) -> List[Dict[str, Any]]:
        """The format chooser for this product's page.

        One entry per SKU in the group, including this one -- Amazon's
        format strip works the same way, and a chooser that hid the current
        format would leave the reader unable to see what they are looking at
        beside what they are not.

        Navigation, like get_cross_links() and unlike get_ebook_cross_links():
        every URL is another row's own product page, so nothing here is
        purchase-gated. A format we no longer sell is still a true answer to
        "what formats exist", and the entry says so in its own price column
        (FORMAT_NOT_SOLD_HERE) rather than by quietly disappearing.

        What the price column must never do is state a figure that is not
        what the reader would pay:

        * a ``noorder`` row's price is meaningless here (it is 0 on pk 107),
          so it is replaced outright;
        * a pay-what-you-want row's price is a suggestion, and printing
          "12.99" beside two fixed prices would read as a third fixed price.
          It says so, and carries the suggestion as a note.

        Returns dicts rather than Products so the template cannot reach past
        the vetted fields into, say, an add-to-cart on a sibling row -- the
        thing get_ebook_cross_links()' docstring is entirely about.

        An empty list for anything that is not in a group with a sibling: one
        format is not a choice.
        """
        members = self.group_members()
        if len(members) < 2:
            return []

        options = []
        for member in members:
            # The note carries whatever the price column could not say, and
            # it is built the same way for every row so that availability
            # cannot go missing from one kind of format. It used to: a
            # pay-what-you-want row's note was its suggestion and nothing
            # else, so a physical PWYW format that was out of stock or still
            # forthcoming read as available beside fixed-price siblings that
            # said so -- and the reader only found out by following the link.
            notes = []
            if member.noorder:
                # "Not sold here" is the whole story; a stock or pre-order
                # note under it would describe a state it does not have.
                price = self.FORMAT_NOT_SOLD_HERE
            elif member.is_pwyw:
                price = self.FORMAT_PWYW_PRICE
                notes.append(f"suggested {member.pwyw_suggested_amount()}")
                # A fixed price carries "Pre-order: " in the price column
                # itself (see below); "Pay what you want" has nowhere to put
                # it, so this row says it in the note instead.
                if member.preorder_only:
                    notes.append("Pre-order")
            else:
                # get_display_price() already prefixes "Pre-order: " on a
                # preorder row, which is the honest thing to say in a column
                # of prices for formats that ship at different times.
                price = member.get_display_price()
            if not member.noorder and member.is_out_of_stock():
                # Never doubled up with the pre-order note above:
                # is_out_of_stock() is false for a preorder row by
                # definition, so at most one of the two applies.
                notes.append("Out of stock")
            note = " · ".join(notes)
            options.append({
                "label": member.get_format_label(),
                # The SKU's own name as well as its format label. The label
                # is what the reader is choosing between; the name is which
                # row they land on, and "the label names the target" is the
                # rule get_ebook_cross_links() exists to keep.
                "name": member.name,
                "url": member.get_absolute_url(),
                "price": price,
                "note": note,
                "is_current": member.pk == self.pk,
            })
        return options

    def listing_name(self) -> str:
        """The title on this product's card in a listing.

        The work's name for a grouped product, its own name otherwise. A
        collapsed card stands for the whole group, so titling it with the
        member that happens to represent the group would put "— E-book" on
        the catalogue's only card for a book that is mostly sold on paper.
        """
        group = self.group
        if group is None:
            return self.name
        return group.name

    def listing_format_labels(self) -> List[str]:
        """The formats a collapsed card should say it stands for.

        The card links to one member's page, so without this a visitor has no
        way to tell from the catalogue that the other two formats exist at
        all -- which is the one thing collapsing the cards could otherwise
        take away from them.

        A pay-what-you-want format says so here, and that annotation is not
        decoration. Before grouping, the e-book had a card of its own and
        that card carried the "pay what you want" label beside its price;
        collapsing the three DC4K cards into one removes it, and the card
        that remains prices the work as a range. That range opens at the
        e-book's suggested 12.99 and runs to a fixed 34.42, so the label
        cannot simply move onto it: calling the whole span a suggestion is
        the exact false claim the label exists to prevent. The annotation
        goes on the format the suggestion belongs to instead.

        The chooser on the product page needs no such annotation -- its price
        column says "Pay what you want" outright -- which is why this is not
        folded into get_format_label().

        Over listed_group_members(), which is where the "not sold here"
        exclusion and the reasoning for it live -- shared with the range, the
        year and the notice so the four statements on one card cannot come to
        disagree about which formats they are about.

        Empty for anything that is not a group with two or more formats for
        sale, so an ungrouped card renders exactly as it did before groups
        existed.
        """
        listed = self.listed_group_members()
        if len(listed) < 2:
            return []
        return [
            (f"{member.get_format_label()} ({self.PWYW_FORMAT_SUFFIX})"
             if member.is_pwyw else member.get_format_label())
            for member in listed
        ]

    # What sits between the two ends of a listing price range. Named rather
    # than written inline for the usual reason -- the tests assert against
    # the source of truth -- and an en dash rather than a hyphen because a
    # hyphen between two numbers reads as a minus sign.
    PRICE_RANGE_SEPARATOR = " – "

    def listing_release_year(self) -> Optional[int]:
        """The year on this product's card.

        For a grouped card, the work's FIRST publication -- the earliest
        release date among the formats sold here -- rather than the date of
        whichever member the collapse happens to be showing. The member is
        picked by format_order, so without this a group could be titled with
        the work, priced across the work, and then dated by an e-book that
        came out five years after the book did.

        Deliberately not the same date that decides where the card SITS. A
        listing is newest-first and a group takes the position of its newest
        surviving member, because a format released this year is news and
        belongs near the top. What the card SAYS is a different question:
        "Published" is a claim about the book, and a paperback from 2019 does
        not become a 2024 book because its e-book appeared then.

        None when nothing in the group carries a release date, which is what
        the template already renders nothing for.
        """
        years = [member.release_date.year
                 for member in self.listed_group_members()
                 if member.release_date is not None]
        if not years:
            return (self.release_date.year
                    if self.release_date is not None else None)
        return min(years)

    def listing_price_bounds(self) -> Optional[Tuple[int, int]]:
        """The cheapest and dearest a grouped card stands for, in cents.

        None when there is no range to speak of: an ungrouped product, a
        group of one, or a group with nothing sellable in it. Those cases
        fall back to the product's own price -- see listing_price_display().

        Over listed_group_members(), so the range spans exactly the formats
        the summary beside it counts -- and so a "group" whose only sellable
        format is one row has no range at all, which is the same answer the
        summary gives by rendering nothing.

        A pay-what-you-want member contributes its SUGGESTED amount, through
        pwyw_suggested_cents() rather than by reading `price` directly: the
        chooser's "suggested 12.99" note comes from the same accessor, so the
        card's range and the page's note cannot come to quote different
        numbers for one format. That is the owner's call and it is what makes
        the low end of the DC4K range 12.99 rather than 0.00; a range
        starting at 0.00 would be true of the e-book alone and read as a
        claim about the book. What keeps it honest is the format summary
        rendered beside it, which names that format and flags it -- see
        listing_format_labels().
        """
        listed = self.listed_group_members()
        if len(listed) < 2:
            return None
        prices = [
            member.pwyw_suggested_cents() if member.is_pwyw else member.price
            for member in listed
        ]
        return (min(prices), max(prices))

    def listing_price_display(self) -> str:
        """The price on this product's card in a listing.

        A range over the formats a grouped card stands for -- "12.99 – 34.42"
        -- because that card no longer belongs to any one of them. Quoting a
        single member's price under a title naming the work says the book
        costs 20.00 when one format is cheaper and another dearer, and the
        member being quoted is an implementation detail of the collapse
        (format_order) rather than anything the visitor chose.

        Bare numbers, because a range cannot carry get_display_price()'s
        "Pre-order: " prefix: prefixing the pair would claim both ends ship
        later when it may be true of only one, and prefixing one end of a
        range is not a thing a price can say. The card links to a product
        page that states that format's availability for itself, and the
        chooser there states the others'.

        Everything else falls through to get_display_price() unchanged --
        every ungrouped product in the catalogue, plus a group whose formats
        happen to cost the same, which has a price rather than a range.
        """
        bounds = self.listing_price_bounds()
        if bounds is None:
            return self.get_display_price()
        low, high = bounds
        if low == high:
            # One price, not a range: the formats agree on what the book
            # costs. The figure is safe to state, but get_display_price()'s
            # "Pre-order: " prefix is a claim about AVAILABILITY, not about
            # the price, and availability is per-format -- so it survives
            # only when every format the card stands for is a pre-order.
            # Otherwise the card would tell a visitor the work can only be
            # pre-ordered while one of its formats ships today, which is the
            # same misstatement the range below refuses to make.
            #
            # `not self.noorder` as well, for the case this row is itself
            # the delisted one the bounds left out: its own price is not the
            # figure the card is quoting, so it cannot answer for it.
            listed = self.listed_group_members()
            if not self.noorder and all(
                    member.preorder_only for member in listed):
                return self.get_display_price()
            return _display_amount(low)
        return (f"{_display_amount(low)}{self.PRICE_RANGE_SEPARATOR}"
                f"{_display_amount(high)}")

    def listing_price_is_a_range(self) -> bool:
        """Whether this product's card shows a span rather than a price.

        The single question both listing_price_display() above and
        listing_shows_pwyw_notice() below turn on, asked once so the notice
        cannot end up describing a number the card is not showing.
        """
        bounds = self.listing_price_bounds()
        return bounds is not None and bounds[0] != bounds[1]

    def listing_shows_pwyw_notice(self) -> bool:
        """Whether the notice under the card's price would be true of it.

        The notice says the number above it is a suggestion and that nothing
        at all is a valid amount. On an ungrouped pay-what-you-want card --
        what every such card was before format groups existed -- that is
        simply true, and the notice stays.

        On a grouped card it has to be true of everything the card stands
        for, and there are two ways it can fail:

        * the price is a RANGE, mostly other rows' fixed prices -- calling
          the span a suggestion misprices the fixed formats;
        * the range degenerates to one figure but a sibling charges it as a
          FIXED price -- "12.99, or nothing at all" on a card that also
          stands for a 12.99-fixed paperback invites a reader to conclude
          the paperback can be had for nothing.

        So the notice renders on a grouped card only when every format the
        card stands for is itself pay-what-you-want at that one figure. In
        every other grouped case the format summary carries the flag
        instead, on the format it is true of.
        """
        if not self.is_pwyw:
            return False
        listed = self.listed_group_members()
        if len(listed) < 2:
            return True
        return (not self.listing_price_is_a_range()
                and all(member.is_pwyw for member in listed))

    def get_other_edition_links(self) -> List[Tuple[str, str]]:
        """get_cross_links(), minus anything the format chooser already shows.

        Both lists are navigation to sibling product pages, so rendering both
        unfiltered puts every format of a grouped work on its own page twice.
        Which one wins is not arbitrary: the chooser states each sibling's
        format and price, and this list states only a name, so the chooser is
        the better of the two wherever they overlap.

        What survives is the case the two fields do not share: an x_link to a
        row that is NOT the same work -- a different edition, a sequel, a
        related title. Those still have nowhere else to render, which is why
        this filters the list rather than the template dropping it whenever a
        chooser is present.
        """
        if self.group_id is None:
            return self.get_cross_links()
        # The members' URLs directly, not via get_format_options(): the page
        # already computes the options once for the chooser, and this only
        # needs to know which pages the chooser occupies -- not their price
        # columns and stock notes over again. Members are filtered on pk, so
        # this stays correct even when a two-member gate elsewhere means no
        # chooser actually renders: the sibling still is the same work, and
        # "other editions" is for works that are not this one.
        group_urls = {member.get_absolute_url()
                      for member in self.group_members()}
        return [(name, url) for name, url in self.get_cross_links()
                if url not in group_urls]

    @staticmethod
    def _amazon_url(domain: str, asin: Optional[str]) -> Optional[str]:
        if not asin:
            return None
        return f"https://www.{domain}/dp/{asin}"

    def _print_asin(self) -> Optional[str]:
        return self.print_asin or self.default_asin

    def get_amazon_link(self) -> Optional[str]:
        # Explicit curated links always win. Otherwise use the format-specific
        # ASIN first, with default_asin only as the catalogue-level fallback.
        return self.amazon_link or self._amazon_url(
            "amazon.com", self._print_asin())

    def get_amazon_in_link(self) -> Optional[str]:
        # amazon.in is a print-store variant, so it uses the same print ASIN
        # resolution as amazon.com before changing only the domain.
        return (
            self.amazon_in_link
            or self._amazon_url("amazon.in", self._print_asin())
        )

    def get_kindle_link(self) -> Optional[str]:
        # Kindle must never fall back to default_asin: default_asin may be a
        # print/catalogue ASIN, which would create an e-book link to paperback.
        return self.kindle_link or self._amazon_url("amazon.com", self.ebook_asin)

    def is_physical_good(self) -> bool:
        """Whether this needs a box, a stamp and an address.

        Used to decide whether checkout asks for a shipping address and offers
        shipping rates. It used to be inferred from mode/category, which was
        only ever right by accident: it made every one-off non-service product
        physical, so the first downloadable product would have demanded a
        mailing address and offered the customer media mail for a PDF.
        """
        return self.delivery_type == Product.DeliveryTypes.PHYSICAL

    def is_digitally_fulfilled(self) -> bool:
        """Whether this site emails the buyer the file itself.

        Both halves are required, and the second one is a legal guard rather
        than a convenience: delivery_type says the product is a download,
        sells_ebook says we hold the right to distribute it. A product marked
        DIGITAL by mistake in the admin still delivers nothing.
        """
        return (self.delivery_type == Product.DeliveryTypes.DIGITAL
                and self.sells_ebook)

    def is_out_of_stock(self) -> bool:
        # Stock is intentionally scoped to physical books. delivery_type has
        # since landed, and DIGITAL products are exempt without a clause of
        # their own: is_physical_good() reads delivery_type directly, so it is
        # already False for a download and short-circuits the rest. That is
        # what keeps a stock count of 0 from blocking emailed ebook
        # fulfilment -- an e-book has no unit count to run out of. The
        # exemption is load-bearing rather than incidental, so it is pinned by
        # test_stock.DigitalStockExemptionTest; do not reintroduce a category-
        # or mode-based inference in is_physical_good() without reading that
        # test first.
        return (
            self.is_physical_good()
            and self.cat == Product.Categories.BOOKS
            and not self.preorder_only
            and not self.backorder
            and cast(int, self.stock) == 0
        )

    def is_purchasable(self) -> bool:
        return not self.noorder and not self.is_out_of_stock()

    SIGNED_ON_REQUEST_NOTE = "All of Holden's books are available signed on request"
    DIGITAL_DELIVERY_NOTE = (
        "Delivered by email as a DRM-free ZIP containing both the EPUB and "
        "the PDF."
    )

    def get_display_text(self):
        """Product copy for the HTML product page, as escaped markup.

        Returns a SafeString so the template does not need `autoescape off`
        around it. Blank lines in the description become paragraph breaks;
        the description itself is escaped, so a stray angle bracket in
        admin-entered copy renders as text instead of as live HTML.
        """
        formatted = mark_safe(linebreaks(self.description, autoescape=True))
        if self.print_isbn:
            return format_html(
                "{}<p>{}</p>", formatted, self.SIGNED_ON_REQUEST_NOTE)
        if self.is_digitally_fulfilled():
            return format_html(
                "{}<p>{}</p>", formatted, self.DIGITAL_DELIVERY_NOTE)
        return format_html("{}", formatted)

    def get_feed_description(self) -> str:
        """The same copy as plain text, for the Google product feed.

        The feed is XML, so markup from get_display_text() would arrive at
        Google as escaped angle brackets and show up literally in the listing.
        """
        if self.print_isbn:
            return f"{self.description}\n\n{self.SIGNED_ON_REQUEST_NOTE}"
        if self.is_digitally_fulfilled():
            return f"{self.description}\n\n{self.DIGITAL_DELIVERY_NOTE}"
        return self.description

    def get_feed_price(self) -> str:
        """Bare numeric price for the feed's <g:price>.

        Not get_display_price(): that prefixes "Pre-order: " for preorder
        products, which is fine on the page and invalid in the feed -- Google
        rejects a price it cannot parse.
        """
        return "{0:.2f}".format(self.price / 100)

    def ships_free_in_feed(self) -> bool:
        """Whether the feed may advertise $0 shipping on this item.

        Keyed off this product's own price, not off a cart, because that is
        what a feed entry can honestly claim: Google's per-item <g:shipping>
        describes shipping for this item, and an item priced at or above the
        threshold does reach it on its own. A cheaper item can still ship free
        in a big enough basket, but the feed has no way to say "over $40 of
        anything" -- and claiming free shipping on a $12 book would be a price
        Google shows and checkout does not honour, which is the mismatch
        Merchant Center suspends accounts over.
        """
        from django.conf import settings

        if not getattr(settings, "FREE_SHIPPING_ENABLED", True):
            return False
        # A pay-what-you-want price is the owner's suggestion, not what the
        # buyer will pay, so it can never earn the claim. Today this is
        # belt-and-braces -- PWYW exists only on digital products, which get
        # no <g:shipping> rows at all -- but that is a catalogue policy, not
        # a schema rule, and the first physical PWYW row would otherwise
        # advertise free shipping checkout does not honour.
        if self.is_pwyw:
            return False
        threshold = getattr(settings, "FREE_SHIPPING_THRESHOLD", 0)
        return self.price >= threshold

    def get_gtin(self):
        # Each Product row is one offer; prefer the print ISBN because the
        # current book rows are print offers, then use an e-book ISBN for
        # digital rows, and only fall back to UPC for non-book products.
        return self.print_isbn or self.ebook_isbn or self.upc

    def get_availability(self):
        if self.preorder_only:
            return "preorder"
        elif self.backorder:
            return "backorder"
        elif self.is_out_of_stock():
            return "out_of_stock"
        else:
            return "in_stock"

    def buy_text(self):
        if self.preorder_only:
            return "Pre-Order"
        elif self.backorder:
            return "Back Order"
        elif self.is_out_of_stock():
            return "Out of Stock"
        else:
            return "Add to Cart"

    def stock_description(self):
        # Preserve the pre-stock behavior if both flags are set:
        # availability/buy_text prefer preorder, but this marker prefers
        # backorder. That ordering is a pre-existing inconsistency.
        if self.backorder:
            return "***Back Order Only***"
        elif self.preorder_only:
            return "***PreOrder Only***"
        elif self.is_out_of_stock():
            return "***Out of Stock***"
        else:
            return ""

    def get_brand(self):
        if self.brand:
            return self.brand
        elif self.cat == Product.Categories.BOOKS:
            return "O'Reilly"
        else:
            return "Pigs Can Fly Labs"

    def get_sizes(self):
        return self.sizes.split(",") if self.sizes else [None]

    def get_mpn(self):
        return self.mpn or f"PCF{self.pk}"


class ProductImage(models.Model):
    """One more picture of a product, beyond the primary one on Product.

    The primary image stays where it was -- Product.image/image_name -- rather
    than becoming the first row here. That keeps every existing caller,
    template and thumbnail path working unchanged, and it keeps the "which one
    is the main image" question answered by a column instead of by an
    ordering: a product whose rows got reordered would otherwise silently
    change what the catalogue card shows.

    Both storage mechanisms Product already supports are supported here for
    the same reasons they exist there. `image_name` points into the static
    tree, which is where every real picture on this site lives (committed to
    the sibling pcfweb-assets checkout); `image` is an upload into MEDIA_ROOT,
    which no deployment mounts a volume over, so an uploaded file is gone on
    the next restart. Prefer image_name for anything that matters -- see the
    Image assets section of the README.
    """

    product = models.ForeignKey(
        "Product", on_delete=models.CASCADE, related_name="extra_images")
    image = models.ImageField(upload_to="data_here", blank=True)
    image_name = models.CharField(
        max_length=250,
        default="",
        blank=True,
        help_text=(
            "Path under static assets/images/, e.g. "
            "book_covers/high_performance_spark_2ed.jpg. Preferred over an "
            "upload: uploads do not survive a pod restart."
        ),
    )
    alt_text = models.CharField(
        max_length=250,
        default="",
        blank=True,
        help_text="Describes the picture for screen readers.",
    )
    # Explicit rather than relying on pk order, so an image added later can be
    # placed second without deleting and re-adding the ones after it. Google
    # reads additional_image_link in feed order and shows the earliest first.
    position = models.PositiveIntegerField(
        default=0,
        db_default=0,
        help_text="Lowest first. Ties break on id, so order is never random.")

    class Meta:
        ordering = ["position", "pk"]

    def get_image_url(self) -> Optional[str]:
        """Where this picture is served from, or None if it is not there.

        Mirrors Product.get_image_url() deliberately, including the order of
        the two mechanisms, so an extra image resolves exactly the way the
        primary one does.
        """
        try:
            return self.image.url
        except Exception:
            if self.image_name:
                return static(f"assets/images/{self.image_name}")
            return None

    def __str__(self) -> str:
        return f"{self.product.name}: {self.image_name or self.image}"

    def __repr__(self) -> str:
        return f"<ProductImage: {self.product.name} #{self.position}>"


class Cart(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    cart_id = models.AutoField(primary_key=True)
    products: "models.ManyToManyField[CartProduct, Any]" = models.ManyToManyField(
        'CartProduct', related_name='cart_products')

    def pwyw_merge_notice_rows(self) -> "models.QuerySet":
        """Lines a merge repriced that the buyer has not been shown yet.

        Union of the FK and the M2M, for the same reason _merge_cart takes
        one: a row can be attached by either, and a hold that missed half the
        rows would be a hold that could be walked around.
        """
        return CartProduct.objects.filter(
            Q(cart=self) | Q(cart_products=self),
            pwyw_amount_merged=True).distinct().select_related("product")

    def pwyw_merge_notice_names(self) -> List[str]:
        """Names of those lines, read live off the products themselves."""
        return [row.product.name for row in self.pwyw_merge_notice_rows()]

    def clear_pwyw_merge_notice(self) -> None:
        """Drop the hold. Only ever called once the cart has been rendered."""
        pks = list(self.pwyw_merge_notice_rows().values_list("pk", flat=True))
        if pks:
            # By pk rather than by the union filter above: UPDATE with a join
            # is not portable, and the pks are already in hand.
            CartProduct.objects.filter(pk__in=pks).update(
                pwyw_amount_merged=False)

    def billable_total(self, cart_products=None) -> int:
        """What the buyer will actually be charged for the lines, in cents.

        A noorder line contributes nothing. Public add-to-cart refuses these
        and checkout re-checks, so one is only in a cart because it was added
        before the flag was set -- but it still cannot be bought, and counting
        it would inflate a total the customer is never charged. pk 107 happens
        to be priced 0, so this matters for the general case rather than for
        the row that prompted it: an admin flagging an existing priced product
        noorder would otherwise leave its price silently in the sum.

        A pay-what-you-want line contributes the amount the buyer named, not
        the owner's suggestion, because effective_unit_amount() is what
        checkout bills.

        *cart_products* lets a caller that has already fetched the rows pass
        them in rather than paying for a second query. It is the same sum
        either way -- the argument is not a filter, and a caller handing in a
        narrowed list would get a total that is not this cart's.
        """
        rows = self.products.select_related(
            "product") if cart_products is None else cart_products
        return sum(row.total_price() for row in rows
                   if not row.product.noorder)

    def qualifies_for_free_shipping(self, cart_products=None) -> bool:
        """Whether this cart has earned the free shipping rate.

        The single definition of the offer: checkout builds the Stripe session
        from it and the cart page advertises it from it, so the page cannot
        promise something the session will not honour. Both read the same
        settings, and a cart with nothing physical in it is not asked -- see
        Payments.checkout, which only offers shipping at all when something
        has to be posted.
        """
        from django.conf import settings

        if not getattr(settings, "FREE_SHIPPING_ENABLED", True):
            return False
        threshold = getattr(settings, "FREE_SHIPPING_THRESHOLD", 0)
        return self.billable_total(cart_products) >= threshold

    def free_shipping_shortfall(self, cart_products=None) -> int:
        """Cents still to add before shipping is free; 0 once it already is.

        Never negative, so the cart page can render it without deciding
        whether a negative number means "qualified" or "bug".
        """
        from django.conf import settings

        if not getattr(settings, "FREE_SHIPPING_ENABLED", True):
            return 0
        threshold = getattr(settings, "FREE_SHIPPING_THRESHOLD", 0)
        return max(0, threshold - self.billable_total(cart_products))

    def clear(self):
        """Empty the cart.

        Dropping the M2M links alone leaves the CartProduct rows behind
        forever, so delete the rows too -- both the ones linked through the
        M2M and any that only carry the cart FK.
        """
        cart_product_ids = set(self.products.values_list('pk', flat=True))
        cart_product_ids |= set(
            CartProduct.objects.filter(cart=self).values_list('pk', flat=True))
        self.products.clear()
        CartProduct.objects.filter(pk__in=cart_product_ids).delete()

    def __str__(self) -> str:
        if self.user is not None:
            return f'{self.user.username}'
        else:
            return f'<Cart: dynamic {self.cart_id}>'

    def __repr__(self) -> str:
        if self.user is not None:
            return f'<Cart: {self.user.username}>'
        else:
            return f'<Cart: dynamic {self.cart_id}>'


class CartProduct(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveBigIntegerField(default=1)
    price_id = models.CharField(max_length=250, null=True)
    # What the buyer chose to pay for one of these, in cents.
    #
    # Null on every ordinary line, and on a pay-what-you-want line nobody has
    # named an amount for yet -- there Product.price, the owner's suggestion,
    # stands in. This column is server-owned: the only writer is
    # set_chosen_amount() below, which only ever stores what
    # parse_pwyw_amount() returned, and checkout reads the amount back off
    # this row rather than off anything the customer sends it.
    chosen_amount = models.IntegerField(null=True, blank=True)
    # Set when a cart merge replaced this line's chosen amount, and cleared
    # only once the buyer has actually been served the rendered cart.
    #
    # It lives here, on the row, rather than in the session, because that is
    # where the fact lives: this line's price changed and the person paying
    # for it has not seen the new one. A session is transient -- logout()
    # flushes it, login() rotates its key, and a bodiless request can be made
    # to touch it -- so a session-backed hold could be separated from the cart
    # it describes, and every bypass found in review was some route to doing
    # exactly that. The cart is persistent; so is this.
    pwyw_amount_merged = models.BooleanField(default=False, db_default=False)

    class Meta:
        # Without this, two concurrent adds of the same product race into two
        # rows, and every later lookup of that (cart, product) blows up with
        # MultipleObjectsReturned.
        constraints = [
            models.UniqueConstraint(
                fields=['cart', 'product'], name='unique_cart_product'),
        ]

    def effective_unit_amount(self) -> int:
        """What one of these costs, in cents.

        The amount the buyer chose where they chose one, the owner's price
        otherwise. This is the single answer to "what is this line worth" --
        the Stripe Price, the order snapshot and the cart total all come
        through here, so none of them can disagree with the others.

        Deliberately not routed through get_display_price(), which returns
        presentation strings like "Pre-order: 30.00" rather than a number.
        """
        if self.product.is_pwyw:
            amount = (self.chosen_amount if self.chosen_amount is not None
                      else self.product.price)
            # The band is applied here, not only where the buyer's entry is
            # parsed, so that it holds however the amount arrived. The
            # fallback is the owner's suggested price, which is edited in the
            # admin and never goes through parse_pwyw_amount -- a
            # pay-what-you-want product priced at 25c would otherwise mint a
            # Price Stripe refuses to put in a session (amount_too_small,
            # confirmed against the live test API), and every buyer of it
            # would hit a dead checkout. See PWYW_ROUND_DOWN_BELOW.
            return round_pwyw_amount(amount)
        return self.product.price

    def set_chosen_amount(self, cents: Optional[int]) -> None:
        """Record a validated pay-what-you-want amount and re-price the line.

        `cents` must already have been through parse_pwyw_amount(); None puts
        the line back on the owner's suggestion.

        A Stripe Price is immutable, so a new amount needs a new Price. Doing
        that here rather than at the call site means no caller can change the
        amount and leave the old one behind for checkout to bill.
        """
        self.chosen_amount = cents
        self.price_id = None
        self.save()

    def generate_price_id(self):
        external_product_id = self.product.ensure_external_product_id()
        amount = self.effective_unit_amount()
        if self.product.mode == Product.Modes.PAYMENT:
            price_id = Payments.create_price(
                external_product_id, amount, currency="usd")
        else:
            price_id = Payments.create_price(
                external_product_id, amount, currency="usd", interval="year")
        return price_id

    def refresh_pwyw_price(self) -> None:
        """Re-mint this line's Price from the amount this row holds, now.

        Called once per line at session-creation time. Three jobs:

        1. It is the point where the billed amount is taken from the database
           rather than from anything the customer sent. Whatever was posted to
           /checkout, the Price handed to Stripe is minted here, from
           chosen_amount as stored, so a tampered amount cannot undercut the row.

        2. It retires the Prices that pay-what-you-want lines carried before
           this change. Those were minted with custom_unit_amount, which Stripe
           refuses to put in a session alongside a second line item, a quantity
           above one, an adjustable_quantity or a discount -- exactly the four
           things this change is here to allow. A cart that was filled before
           the deploy would otherwise fail at checkout.

        3. It retires Prices minted without tax_behavior. Stripe Price tax
           behavior is immutable, so non-PWYW cart rows added before the fix
           keep failing under automatic tax unless checkout mints a replacement.
        """
        from django.conf import settings

        if self.product.is_pwyw:
            self.price_id = self.generate_price_id()
            self.save(update_fields=["price_id"])
            return

        automatic_tax_enabled = bool(getattr(settings, "STRIPE_AUTOMATIC_TAX", True))
        if automatic_tax_enabled:
            self.price_id = self.generate_price_id()
            self.save(update_fields=["price_id"])

    def save(self, *args, **kwargs):
        if not self.price_id:
            self.price_id = self.generate_price_id()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                # Minting a price and then not writing it would silently
                # abandon the Stripe object and re-mint on the next save.
                kwargs["update_fields"] = set(update_fields) | {"price_id"}
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f'{self.product.name}'

    def __repr__(self) -> str:
        return f'<CartProduct: {self.product.name}>'

    def total_price(self):
        return (self.effective_unit_amount() * self.quantity)

    def unit_display_price(self) -> str:
        """What one of these will be billed at, as a bare number.

        Bare because the cart renders it into a number input the buyer can
        edit. Not get_display_price(), which returns copy like
        "Pre-order: 30.00" and does not know about the chosen amount.
        """
        return "{0:.2f}".format(self.effective_unit_amount() / 100)

    def total_display_price(self):
        return "{0:.2f}".format(self.total_price() / 100)


def _display_amount(cents: Optional[int]) -> str:
    if cents is None:
        return "-"
    return "{0:.2f}".format(cents / 100)


# The most a buyer may choose to pay for one pay-what-you-want item, in cents.
# There is no floor: paying nothing is a valid amount, on the owner's
# instruction, and the copy on the product page says so. This ceiling is not a
# judgement about generosity -- it is the guard that keeps a hostile or
# fat-fingered amount from becoming a real charge. Stripe's own per-charge USD
# maximum is 999_999_99, so anything below that is a local policy choice;
# $10,000 is far beyond what anything here is worth and still low enough that
# an extra keystroke is refused rather than billed.
MAX_PWYW_AMOUNT = 1_000_000

# Under this many cents, a pay-what-you-want amount is rounded down to nothing
# rather than charged. The owner's rule, and the single place the threshold is
# written down.
#
# Why rounding rather than a minimum, because it is not obvious and it matters:
# Stripe refuses to charge less than $0.50 USD at all (amount_too_small), but
# accepts a total of exactly 0 -- both confirmed against the live test API.
# Rounding the whole 1..99c band down to 0 puts the forbidden 1..49c band out
# of reach *by construction*. No session is ever created for an amount Stripe
# would reject, so there is no error path to design and no buyer to bounce.
# Any implementation that leaves 1..49c reachable puts that failure mode back.
#
# This is a rounding rule, not a parser. It runs only after parse_pwyw_amount
# has accepted the input: garbage is still refused, never coerced to zero.
PWYW_ROUND_DOWN_BELOW = 100

_NOT_A_NUMBER = ("That is not an amount. Enter dollars and cents, "
                 "for example 12.99.")


class PwywAmountError(ValueError):
    """A pay-what-you-want amount that must not be stored or billed."""


def round_pwyw_amount(cents: int) -> int:
    """Apply the owner's round-down band to an already-validated amount.

    Anything in 1..99c becomes 0; a dollar and up is charged as entered. See
    PWYW_ROUND_DOWN_BELOW for why the band exists and where its edge is.

    Separate from parse_pwyw_amount so the buyer can be shown what they will
    actually be charged before they commit to it, using the same rule that
    will charge them.
    """
    return 0 if cents < PWYW_ROUND_DOWN_BELOW else cents


def parse_pwyw_amount(raw: Any) -> int:
    """Turn a buyer-supplied dollar amount into integer cents.

    Every branch here is a rejection rather than a coercion, because this
    value arrives from a form field on a public page: it is hostile input
    until it has been through this function, and the only thing downstream
    ever sees is the int it returns.

    Decimal rather than float on purpose. float("0.1") * 100 is 10.000000000
    000002, so a float path would either bill a cent off or need a rounding
    rule that quietly turns a rejected amount into an accepted one.
    """
    if raw is None:
        raise PwywAmountError("Enter an amount. 0 is a valid amount.")
    text = str(raw).strip().lstrip("$").strip()
    if not text:
        raise PwywAmountError("Enter an amount. 0 is a valid amount.")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise PwywAmountError(_NOT_A_NUMBER) from None
    # Decimal parses "NaN" and "Infinity" without complaint, and both slide
    # through the comparisons below -- NaN because every comparison against it
    # is False, Infinity because it really is greater than the ceiling but
    # int() on it raises rather than returning something billable.
    if not amount.is_finite():
        raise PwywAmountError(_NOT_A_NUMBER)
    if amount < 0:
        raise PwywAmountError(
            "An amount cannot be negative. The lowest you can pay is 0.")
    scaled = amount.scaleb(2)
    if scaled != scaled.to_integral_value():
        raise PwywAmountError(
            "Amounts go down to the cent, so at most two decimal places.")
    cents = int(scaled)
    if cents > MAX_PWYW_AMOUNT:
        raise PwywAmountError(
            f"{_display_amount(MAX_PWYW_AMOUNT)} is the most that can be "
            "taken in one go. Get in touch if you really meant it.")
    # Rounding is the last step, after every refusal above. A negative, a
    # fraction of a cent, a word or an absurd number is still an error -- it
    # does not become a free download.
    return round_pwyw_amount(cents)


# How long a physical order takes to reach a buyer, in days. Published to
# Google in two places that must not drift apart: the product feed's
# <g:min_handling_time>/<g:max_handling_time> rows, and the estimated delivery
# date the Customer Reviews opt-in reports. Google compares the second against
# when the survey may be sent, so a number invented separately in each place
# would have us promising one delivery window and surveying against another.
MIN_HANDLING_DAYS = 3
MAX_HANDLING_DAYS = 21
# The slowest transit any published shipping row claims (CA economy, 21 days).
# The estimate below deliberately uses the slow end: a survey that arrives
# before the parcel does asks the customer to rate a delivery that has not
# happened.
MAX_TRANSIT_DAYS = 21


class Order(models.Model):
    """A purchase.

    Written as PENDING at checkout time, *before* the customer leaves for
    Stripe, because by the time a webhook could run the cart is gone: the
    success page empties it, and an anonymous cart is session-scoped so a
    server-to-server callback has no way to find it. The line items are
    therefore snapshotted here (see OrderItem) rather than looked up later.

    Only the Stripe webhook moves an order to PAID. The browser redirect to
    /checkout/success proves nothing -- anyone can request that URL.
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending payment'
        PAID = 'PAID', 'Paid'
        FULFILLED = 'FULFILLED', 'Fulfilled'
        CANCELLED = 'CANCELLED', 'Cancelled'

    # Null until Session.create() comes back with an id; unique so a webhook
    # can never attach the same Stripe session to two orders.
    stripe_session_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True)
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='orders')
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
        db_index=True)

    # As reported by Stripe on the paid session; blank while PENDING.
    customer_email = models.EmailField(blank=True)
    customer_name = models.CharField(max_length=250, blank=True)

    shipping_name = models.CharField(max_length=250, blank=True)
    shipping_line1 = models.CharField(max_length=250, blank=True)
    shipping_line2 = models.CharField(max_length=250, blank=True)
    shipping_city = models.CharField(max_length=250, blank=True)
    shipping_state = models.CharField(max_length=250, blank=True)
    shipping_postal_code = models.CharField(max_length=50, blank=True)
    shipping_country = models.CharField(max_length=2, blank=True)

    billing_name = models.CharField(max_length=250, blank=True)
    billing_line1 = models.CharField(max_length=250, blank=True)
    billing_line2 = models.CharField(max_length=250, blank=True)
    billing_city = models.CharField(max_length=250, blank=True)
    billing_state = models.CharField(max_length=250, blank=True)
    billing_postal_code = models.CharField(max_length=50, blank=True)
    billing_country = models.CharField(max_length=2, blank=True)

    # All in the smallest currency unit (cents), like everything else here.
    # amount_total starts as the cart snapshot's subtotal and is overwritten
    # with Stripe's authoritative number once the payment lands.
    amount_total = models.IntegerField(default=0)
    # Stripe's pre-tax, pre-shipping line-item total. Compared against the
    # snapshot to detect a customer-adjusted quantity, see quantities_match().
    amount_subtotal = models.IntegerField(null=True, blank=True)
    amount_tax = models.IntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    # Whether the owner actually got told about this order. A send failure
    # must not fail the webhook (Stripe would retry for days), so it is
    # recorded here instead of being silently swallowed.
    notified_at = models.DateTimeField(null=True, blank=True)
    notification_error = models.TextField(blank=True)

    # Whether the buyer actually got their download links. Kept separate from
    # the owner-notification pair above because the two fail independently and
    # for different reasons -- a missing book archive is not an SMTP problem --
    # and the owner needs to know which one to redo by hand.
    #
    # digital_delivery_sent_at is nullable, so it needs nothing extra; the
    # error column is NOT NULL and carries a db_default so an old pod's
    # checkout INSERT -- which names neither -- still succeeds while a deploy
    # rolls. See RollingDeployOldCodeWriteTest.
    digital_delivery_sent_at = models.DateTimeField(null=True, blank=True)
    digital_delivery_error = models.TextField(blank=True, db_default="")

    # When the snapshotted line items were checked against what Stripe
    # actually billed. Null means that check did not happen (or failed), so
    # the item quantities below are the pre-checkout cart's, not the
    # customer's final ones -- see reconcile_line_items().
    reconciled_at = models.DateTimeField(null=True, blank=True)
    # Anything a human should look at: the whole lookup failing, or an
    # individual line that could not be matched up. Set with reconciled_at
    # null means we fell back to the snapshot entirely; set with
    # reconciled_at populated means we reconciled but something was odd.
    reconciliation_error = models.TextField(blank=True)

    # Held by whichever worker is currently running fulfilment for this order,
    # so the four Gunicorn workers cannot each start it at once. The markers
    # above are all written *after* their side effect, so on their own they
    # only serialise deliveries that do not overlap: two workers checking
    # notified_at while a third is mid-send all see null and all send. This
    # column is claimed with a single conditional UPDATE, which is atomic on
    # both Postgres and SQLite, so exactly one worker can hold it.
    #
    # It is a lease, not a flag: a worker that dies mid-fulfilment cannot
    # release it, and a permanently claimed order would never be repaired --
    # which is the very failure this whole retry path exists to fix. A claim
    # older than FULFILMENT_LEASE is therefore reclaimable. Nullable, so an
    # old pod's checkout INSERT during a rolling deploy still succeeds.
    fulfilment_claimed_at = models.DateTimeField(null=True, blank=True)

    # Whether the buyer received their receipt email. Kept separately from
    # the notification and digital-delivery markers because it is best-effort:
    # a receipt failure must never block delivery or owner notification.
    # Nullable (no db_default needed on the timestamp), and the error column
    # carries a db_default so an old pod's checkout INSERT still succeeds.
    receipt_sent_at = models.DateTimeField(null=True, blank=True)
    receipt_error = models.TextField(blank=True, db_default="")

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'Order #{self.pk} ({self.get_status_display()})'

    def __repr__(self) -> str:
        return f'<Order: #{self.pk} {self.status}>'

    @classmethod
    def create_from_cart(cls, cart: Cart, user: Optional[User] = None) -> "Order":
        """Snapshot a cart into a new PENDING order.

        Reads the cart through the same M2M that Payments.checkout() bills
        from, so the snapshot and the Stripe line items come from one list.
        """
        cart_products = list(cart.products.select_related('product'))
        order = cls.objects.create(
            user=user,
            status=cls.Status.PENDING,
            currency=DEFAULT_CURRENCY,
            amount_total=sum(cp.total_price() for cp in cart_products),
        )
        OrderItem.objects.bulk_create([
            OrderItem(
                order=order,
                product=cp.product,
                product_name=cp.product.name,
                # What this line is actually being billed at, which on a
                # pay-what-you-want row is the buyer's amount and not the
                # owner's suggestion. Snapshotting Product.price here was why
                # a receipt for an order paid at nothing still showed a 12.99
                # line above a 0.00 total.
                unit_amount=cp.effective_unit_amount(),
                currency=DEFAULT_CURRENCY,
                quantity=cp.quantity,
                snapshot_quantity=cp.quantity,
                price_id=cp.price_id or "",
            ) for cp in cart_products
        ])
        return order

    def snapshot_subtotal(self) -> int:
        """Pre-tax, pre-shipping total of the current line quantities.

        After reconciliation these are the quantities Stripe billed; before
        it, they are the cart's.
        """
        return sum(item.total_amount() for item in self.items.all())

    def original_subtotal(self) -> int:
        """The same total, using the quantities as they were at checkout."""
        return sum(item.original_amount() for item in self.items.all())

    def adjusted_items(self) -> List["OrderItem"]:
        """Lines the customer changed on Stripe's hosted page."""
        return [item for item in self.items.all() if item.quantity_adjusted()]

    def quantities_are_authoritative(self) -> bool:
        """Whether the line quantities came from Stripe rather than the cart."""
        return self.reconciled_at is not None

    def quantities_match(self) -> bool:
        """Whether Stripe's subtotal agrees with the line items we hold.

        Only meaningful as a fallback signal: when reconciliation could not
        run, this is the one hint that the customer changed something.
        """
        if self.amount_subtotal is None:
            return True
        return self.amount_subtotal == self.snapshot_subtotal()

    def total_display_price(self) -> str:
        return _display_amount(self.amount_total)

    def shipping_address_lines(self) -> List[str]:
        city_line = " ".join(
            part for part in [
                ", ".join(p for p in [self.shipping_city,
                          self.shipping_state] if p),
                self.shipping_postal_code,
            ] if part)
        return [line for line in [
            self.shipping_name,
            self.shipping_line1,
            self.shipping_line2,
            city_line,
            self.shipping_country,
        ] if line]

    def notification_subject(self) -> str:
        return f"[pigscanfly] New paid order #{self.pk}"

    def fulfilment_caveats(self) -> List[str]:
        """Anything that makes the item list above less than trustworthy.

        This email *is* the pick/pack instruction, so when the quantities are
        known to possibly be wrong that has to be impossible to miss.
        """
        if self.quantities_are_authoritative():
            if self.reconciliation_error:
                # Reconciled, but something did not line up cleanly.
                return ["",
                        "Note: the quantities above came from Stripe, but not "
                        "everything matched up cleanly:",
                        f"  {self.reconciliation_error}"]
            return []
        # Reconciliation did not happen, so the quantities are the cart's.
        if not self.quantities_match():
            return [
                "",
                "*** WARNING: DO NOT SHIP FROM THE LIST ABOVE WITHOUT "
                "CHECKING. Stripe's line-item subtotal "
                f"({_display_amount(self.amount_subtotal)}) does not match it "
                f"({_display_amount(self.snapshot_subtotal())}), and the "
                "quantities could not be re-read from Stripe, so the customer "
                "very likely changed something at checkout. Open the session "
                "in the Stripe Dashboard to see what was actually bought. ***",
                f"  ({self.reconciliation_error})"
                if self.reconciliation_error else "",
            ]
        return [
            "",
            "Note: the quantities above could not be re-read from Stripe, so "
            "they are the ones from the cart. The totals agree, so they are "
            "very probably right.",
            f"  ({self.reconciliation_error})"
            if self.reconciliation_error else "",
        ]

    def notification_body(self) -> str:
        lines = [
            f"Order #{self.pk} was paid and is ready to fulfil.",
            "",
            f"Status:   {self.get_status_display()}",
            f"Placed:   {self.created_at:%Y-%m-%d %H:%M %Z}",
            f"Customer: {self.customer_name or '(no name)'} "
            f"<{self.customer_email or 'no email reported'}>",
            f"Stripe session: {self.stripe_session_id or '(none)'}",
            "",
            "Items",
            "-----",
        ]
        for item in self.items.all():
            line = (f"  {item.quantity} x {item.product_name} "
                    f"@ {_display_amount(item.unit_amount)} "
                    f"= {_display_amount(item.total_amount())}")
            if item.quantity_adjusted():
                line += (f"   [adjusted at checkout, was "
                         f"{item.snapshot_quantity}]")
            if item.product is not None and item.product.is_pwyw:
                # The amount above is now what the buyer was actually billed,
                # snapshotted before the session was created -- not the
                # owner's suggestion. Still worth flagging: it explains a line
                # that does not match the catalogue price, and a zero one.
                # "paid" rather than "chose" because an amount under a dollar
                # is rounded down, so the two can differ; see
                # PWYW_ROUND_DOWN_BELOW.
                line += "   [pay-what-you-want: the amount the buyer paid]"
            lines.append(line)
        lines += self.fulfilment_caveats()
        lines += [
            "",
            f"Tax:   {_display_amount(self.amount_tax)}",
            f"Total: {_display_amount(self.amount_total)} "
            f"{self.currency.upper()}",
            "",
            "Ship to",
            "-------",
        ]
        address = self.shipping_address_lines()
        lines += [f"  {line}" for line in address] or [
            "  (no shipping address collected -- digital/service order?)"]
        lines += self.digital_delivery_report()
        lines += [
            "",
            "Mark the order FULFILLED in the admin once it has shipped.",
        ]
        return "\n".join(lines)

    def digital_delivery_report(self) -> List[str]:
        """What the buyer was, or was not, sent.

        Delivery runs just before this email, so its outcome is known here.
        A failure has to be loud: nothing else will ever tell the owner that
        somebody paid for a book and did not get it.
        """
        if not self.digital_items():
            return []
        lines = ["", "Digital delivery", "----------------"]
        if self.digital_delivery_sent_at:
            lines.append(
                f"  Download links emailed to {self.customer_email} at "
                f"{self.digital_delivery_sent_at:%Y-%m-%d %H:%M %Z}.")
            if self.digital_delivery_error:
                # Something on this order was sent, so the line above is true
                # -- but it is not the whole story and must not read as "all
                # done".
                lines.append(
                    "  *** NOT EVERYTHING ON THIS ORDER WAS DELIVERED. ***")
        else:
            lines.append(
                "  *** NOT DELIVERED. This order includes a download and the "
                "customer has not been sent it. Fix the cause below and "
                "resend by hand. ***")
        if self.digital_delivery_error:
            lines.append(f"  {self.digital_delivery_error}")
        return lines

    def notification_recipients(self) -> List[str]:
        return admin_recipients()

    # One page is plenty: a line is a distinct product, and the store sells
    # nowhere near this many different things.
    LINE_ITEM_PAGE_SIZE = 100

    def reconcile_line_items(self) -> bool:
        """Replace the cart's quantities with the ones Stripe actually billed.

        Checkout enables adjustable_quantity, so the customer can change what
        they are buying on Stripe's hosted page after the snapshot is written.
        The snapshot is what the owner's notification email tells them to pick
        and pack, so leaving it knowingly stale means shipping the wrong thing
        whenever they miss the warning. One extra API call is cheaper than
        that.

        Never raises and never retries: losing a paid order because a
        secondary lookup timed out would be far worse than an approximate
        quantity. A failure leaves the snapshot in place, records why, and
        leaves reconciled_at null so the email says the list is unverified.
        """
        if not self.stripe_session_id:
            self._record_reconciliation_failure(
                "no Stripe session id on the order")
            return False
        try:
            page = Payments.list_line_items(
                self.stripe_session_id, limit=self.LINE_ITEM_PAGE_SIZE)
            billed, truncated = self._billed_quantities(page)
            if truncated:
                # Refuse before writing anything. Applying page one alone
                # would zero every line that lives on page two -- silently
                # dropping real items from the pick list, with only a caveat
                # buried in the email to say so. Declared ignorance beats a
                # partial truth the owner might act on.
                self._record_reconciliation_failure(
                    f"Stripe reported more than {self.LINE_ITEM_PAGE_SIZE} "
                    "line items, which this does not page through; the "
                    "quantities were left at the cart's")
                return False
            # The quantities and the "these came from Stripe" marker are one
            # fact, so they land together or not at all: a crash between them
            # would leave Stripe's quantities on the items with reconciled_at
            # still null, and the admin disagreeing with reality.
            #
            # This opens well after the PENDING -> PAID transaction has
            # committed (see StripeWebhookView.handle_paid), so it does not
            # extend that row lock.
            with transaction.atomic():
                problems = self._apply_billed_quantities(billed)
                Order.objects.filter(pk=self.pk).update(
                    reconciled_at=timezone.now(),
                    reconciliation_error="; ".join(problems)[:2000])
        except Exception as e:
            logger.exception(
                "Order #%s: could not reconcile line items against Stripe.",
                self.pk)
            self._record_reconciliation_failure(f"{type(e).__name__}: {e}")
            return False
        self.refresh_from_db()
        return True

    @staticmethod
    def _billed_quantities(page) -> Tuple[Dict[str, int], bool]:
        """price id -> quantity billed, from a Stripe line-item page."""
        def field(obj, key):
            if isinstance(obj, dict):
                return obj.get(key)
            return getattr(obj, key, None)

        billed: Dict[str, int] = {}
        for line in field(page, "data") or []:
            price_id = field(field(line, "price") or {}, "id")
            quantity = field(line, "quantity")
            if not price_id or quantity is None:
                continue
            billed[price_id] = billed.get(price_id, 0) + int(quantity)
        return billed, bool(field(page, "has_more"))

    def _apply_billed_quantities(self, billed: Dict[str, int]) -> List[str]:
        """Write the billed quantities onto the lines, joined on price id.

        Returns the list of things that did not line up, for a human to read;
        an unmatched line is recorded rather than raised.
        """
        problems: List[str] = []
        items = list(self.items.all())
        price_counts: Dict[str, int] = {}
        for item in items:
            if item.price_id:
                price_counts[item.price_id] = price_counts.get(
                    item.price_id, 0) + 1

        changed = []
        for item in items:
            if not item.price_id:
                problems.append(
                    f"{item.product_name!r} carries no Stripe price id, so "
                    "its quantity could not be checked")
                continue
            if price_counts[item.price_id] > 1:
                problems.append(
                    f"price {item.price_id} covers more than one line, so "
                    f"{item.product_name!r} could not be matched")
                continue
            if item.price_id not in billed:
                # Stripe's list is the complete set of what was billed, so an
                # absent line means the customer took it out at checkout.
                # Zero is the honest quantity; snapshot_quantity still records
                # what they had put in.
                problems.append(
                    f"Stripe did not bill for {item.product_name!r}; treating "
                    "it as removed at checkout")
            quantity = billed.get(item.price_id, 0)
            if quantity != item.quantity:
                item.quantity = quantity
                changed.append(item)

        for price_id, quantity in billed.items():
            if price_id not in price_counts:
                problems.append(
                    f"Stripe billed {quantity} of price {price_id}, which "
                    "matches no line on this order")

        if changed:
            OrderItem.objects.bulk_update(changed, ["quantity"])
        return problems

    def _record_reconciliation_failure(self, message: str) -> None:
        try:
            Order.objects.filter(pk=self.pk).update(
                reconciliation_error=message[:2000])
        except Exception:
            logger.exception(
                "Order #%s: could not record the reconciliation failure.",
                self.pk)
        self.reconciliation_error = message[:2000]

    def notify_owner(self) -> bool:
        """Email the owner so they can pick, pack and ship this order.

        Never raises: the caller is a Stripe webhook, and a non-2xx there
        makes Stripe retry for up to three days -- which would mean duplicate
        mail on top of the original failure. A failure is recorded on the row
        instead, so it is visible in the admin rather than silent.
        """
        recipients = self.notification_recipients()
        if not recipients:
            message = ("No ADMINS configured, so nobody was told about this "
                       "order. Set the ORDER_NOTIFICATION_EMAIL env var.")
            logger.error("Order #%s: %s", self.pk, message)
            self._record_notification_failure(message)
            return False
        try:
            send_mail(
                self.notification_subject(),
                self.notification_body(),
                settings.DEFAULT_FROM_EMAIL,
                recipients,
                fail_silently=False,
            )
        except Exception as e:
            logger.exception(
                "Order #%s: failed to send the owner notification.", self.pk)
            self._record_notification_failure(f"{type(e).__name__}: {e}")
            return False
        Order.objects.filter(pk=self.pk).update(
            notified_at=timezone.now(), notification_error="")
        self.notified_at = timezone.now()
        self.notification_error = ""
        return True

    def digital_items(self) -> List["OrderItem"]:
        """Lines the customer is expecting a download for.

        Everything marked DIGITAL, *including* the ones the rights interlock
        will refuse to send. Those are precisely the ones somebody has to be
        told about, so they must not be filtered out this early -- an
        interlock that fires silently is worse than no interlock, because the
        customer has paid and nobody knows they got nothing.
        """
        return [item for item in self.items.select_related('product')
                if item.product is not None
                and item.product.delivery_type == Product.DeliveryTypes.DIGITAL]

    @property
    def has_digital_items(self) -> bool:
        """Whether this order includes any downloadable line."""
        return bool(self.digital_items())

    @property
    def has_physical_items(self) -> bool:
        """Whether this order includes any physical item to ship."""
        return any(
            item.product is not None and item.product.is_physical_good()
            for item in self.items.select_related('product'))

    def estimated_delivery_date(self):
        """When this order should have arrived, as a date.

        Reported to Google Customer Reviews, which holds the survey until
        after this date. Built from the same handling and transit numbers the
        product feed publishes, and from the slow end of them: a survey that
        arrives before the parcel asks the customer to rate a delivery that
        has not happened, which is worse than one that arrives late.

        A digital order is delivered the moment it is paid, so it estimates
        the day it was placed rather than three weeks out -- otherwise every
        e-book buyer would be surveyed a month after they had already read
        the thing.

        Counted from paid_at when the payment has landed, and from created_at
        otherwise, so a delayed payment method does not date the estimate from
        before the order was really placed.
        """
        placed = self.paid_at or self.created_at
        if placed is None:
            return None
        placed_date = timezone.localtime(placed).date()
        if not self.has_physical_items:
            return placed_date
        return placed_date + timedelta(
            days=MAX_HANDLING_DAYS + MAX_TRANSIT_DAYS)

    def review_gtins(self) -> List[str]:
        """GTINs of the ordered products, for the Customer Reviews opt-in.

        Optional in Google's module, and skipped for any line whose product
        has been deleted or carries no GTIN -- the field is a list of
        identifiers, and an empty string in it is not one.
        """
        gtins = []
        for item in self.items.select_related("product"):
            if item.product is None:
                continue
            gtin = item.product.get_gtin()
            if gtin and gtin not in gtins:
                gtins.append(str(gtin))
        return gtins

    def delivery_country(self) -> str:
        """Where this went, as a two-letter code.

        Falls back to the billing country because a digital order collects no
        shipping address at all, and Customer Reviews requires a country.
        """
        return self.shipping_country or self.billing_country

    def deliverable_digital_items(self) -> List["OrderItem"]:
        """Digital lines this site is licensed to deliver itself.

        Reads the interlock live off the Product rather than from the
        snapshot: revoked distribution rights must stop delivery immediately,
        including on a webhook re-delivery for an old order.
        """
        return [item for item in self.digital_items()
                if item.product is not None
                and item.product.is_digitally_fulfilled()]

    def withheld_digital_items(self) -> List["OrderItem"]:
        """Digital lines the rights interlock refuses to send."""
        return [item for item in self.digital_items()
                if item.product is not None
                and not item.product.is_digitally_fulfilled()]

    def digital_delivery_subject(self) -> str:
        return "Your download from Pigs Can Fly Labs"

    def digital_delivery_body(self, links: List[Tuple[str, str]]) -> str:
        lines = [
            "Thank you! Here "
            f"{'is your download' if len(links) == 1 else 'are your downloads'}"
            f" from order #{self.pk}.",
            "",
        ]
        for name, url in links:
            lines += [f"{name}", f"  {url}", ""]
        lines += [
            f"{'That link' if len(links) == 1 else 'Those links'} "
            f"{'works' if len(links) == 1 else 'work'} for "
            f"{link_lifetime_days()} days. Each download "
            "is a ZIP holding both the EPUB and the PDF -- no DRM, yours to "
            "keep, so do save a copy somewhere.",
            "",
            "If the link has expired or something is wrong with the file, "
            f"reply to this mail or write to {settings.DEFAULT_FROM_EMAIL} "
            "and we will send a fresh one.",
        ]
        return "\n".join(lines)

    def deliver_digital_goods(self) -> bool:
        """Email the buyer a signed, expiring link per digital line.

        A link rather than an attachment: an illustrated book runs to tens of
        megabytes, Gmail refuses attachments over 25MB, and this is called
        from inside a Stripe webhook that has to answer quickly.

        Never raises, for the reason notify_owner() does not: the payment has
        already been recorded and a non-2xx would have Stripe redelivering for
        three days. Everything that can go wrong -- no address, a bad stem, a
        missing archive, a dead mail server -- is recorded on the row for the
        owner to resend from, and the order stays PAID.

        Returns True only if every digital line was sent cleanly.
        """
        try:
            return self._deliver_digital_goods()
        except Exception as e:
            logger.exception(
                "Order #%s: digital delivery failed unexpectedly.", self.pk)
            self._record_digital_delivery_failure(f"{type(e).__name__}: {e}")
            return False

    # Said to the owner, and only to the owner, when the rights interlock
    # stops a delivery. It has to name the product and say what to do: this
    # is a customer who has paid for a download and will not be getting one.
    WITHHELD_MESSAGE = (
        "{name!r} is marked as a digital product but sells_ebook is not set, "
        "so this site is not recorded as licensed to distribute it. Nothing "
        "was sent and the customer has paid. Set sells_ebook if the rights "
        "are in place and resend, or refund the order.")

    def _deliver_digital_goods(self) -> bool:
        items = self.digital_items()
        if not items:
            # Nothing downloadable on this order, which is the common case.
            # Nothing to record: this is silence about a non-event, as
            # distinct from the withheld case below.
            return False

        problems: List[str] = []
        for item in self.withheld_digital_items():
            # The interlock did its job; now it has to say so. Leaving this
            # to look like "no digital items" is how a paid order quietly
            # delivers nothing.
            message = self.WITHHELD_MESSAGE.format(name=item.product_name)
            logger.error("Order #%s: %s", self.pk, message)
            problems.append(message)

        deliverable = self.deliverable_digital_items()
        if not deliverable:
            self._record_digital_delivery_failure("; ".join(problems))
            return False

        if not self.customer_email:
            problems.append(
                "Stripe reported no customer email for this order, so the "
                "download link could not be sent. Get an address from the "
                "Stripe Dashboard and resend it by hand.")
            self._record_digital_delivery_failure("; ".join(problems))
            return False

        links: List[Tuple[str, str]] = []
        for item in deliverable:
            product = item.product
            assert product is not None  # deliverable_digital_items() guarantees it
            try:
                # Resolve and open now, so a missing or unnameable archive is
                # caught here rather than emailed out as a link that 404s.
                open_asset(product.digital_asset_name).close()
            except DigitalAssetError as e:
                # Logged as well as recorded: this is somebody having paid and
                # not received, so it should surface in the pod logs rather
                # than only on a row nobody is watching.
                logger.error(
                    "Order #%s: cannot deliver %r: %s",
                    self.pk, item.product_name, e)
                problems.append(f"{item.product_name!r}: {e}")
                continue
            links.append((item.product_name,
                          download_url(self.pk, product.pk)))

        if not links:
            self._record_digital_delivery_failure("; ".join(problems))
            return False
        try:
            send_mail(
                self.digital_delivery_subject(),
                self.digital_delivery_body(links),
                settings.DEFAULT_FROM_EMAIL,
                [self.customer_email],
                fail_silently=False,
            )
        except Exception as e:
            logger.exception(
                "Order #%s: failed to send the download email.", self.pk)
            problems.append(
                f"the download email could not be sent: {type(e).__name__}: {e}")
            self._record_digital_delivery_failure("; ".join(problems))
            return False

        # A targeted UPDATE for the same reason as the notification fields:
        # this runs after the paid transition committed.
        error = "; ".join(problems)[:2000]
        Order.objects.filter(pk=self.pk).update(
            digital_delivery_sent_at=timezone.now(),
            digital_delivery_error=error)
        self.digital_delivery_sent_at = timezone.now()
        self.digital_delivery_error = error
        return not problems

    def _record_digital_delivery_failure(self, message: str) -> None:
        try:
            Order.objects.filter(pk=self.pk).update(
                digital_delivery_error=message[:2000])
        except Exception:
            logger.exception(
                "Order #%s: could not record the digital delivery failure.",
                self.pk)
        self.digital_delivery_error = message[:2000]

    def _record_notification_failure(self, message: str) -> None:
        # A targeted UPDATE, not save(): this runs after the paid transition
        # has committed and must not write back any other stale field.
        try:
            Order.objects.filter(pk=self.pk).update(
                notification_error=message[:2000])
        except Exception:
            logger.exception(
                "Order #%s: could not even record the notification failure.",
                self.pk)
        self.notification_error = message[:2000]

    # ---- buyer receipt ----

    def receipt_subject(self) -> str:
        return f"Your receipt for order #{self.pk} \u2014 Pigs Can Fly Labs"

    def receipt_body(self) -> str:
        lines = [
            f"Order #{self.pk}",
            f"Date: {self.created_at:%B %-d, %Y}",
            "",
            "Items",
            "-----",
        ]
        physical_lines = []
        for item in self.items.all():
            line = (
                f"  {item.quantity} x {item.product_name}"
                f" @ {_display_amount(item.unit_amount)}"
                f" = {_display_amount(item.total_amount())}"
            )
            if item.product is not None and item.product.is_pwyw:
                line += "   (pay-what-you-want: the amount you chose)"
            lines.append(line)
            if item.product is not None and item.product.is_physical_good():
                physical_lines.append(item)

        lines += [
            "",
            f"Subtotal:  {_display_amount(self.amount_subtotal)}",
        ]
        if self.amount_tax:
            lines += [
                f"Tax:       {_display_amount(self.amount_tax)}",
                "",
                "Prices on the site are shown without tax. Tax is added at "
                "checkout, so the total below is higher than the product "
                "page displayed.",
            ]
        lines += [
            f"Total:     {_display_amount(self.amount_total)}"
            f" {self.currency.upper()}",
            "",
        ]

        # What happens next
        lines += ["What happens next", "-----------------"]

        digital = self.digital_items()
        if digital:
            lines += [
                "This order includes digital goods. A download link email "
                f"{'has been' if self.digital_delivery_sent_at else 'will be'}"
                " sent to you separately. Each link is good for "
                f"{link_lifetime_days()} days. Save the files somewhere "
                "safe \u2014 no DRM, yours to keep.",
                "",
            ]

        if physical_lines:
            lines += [
                "This order includes physical goods. Shipping times for "
                "physical goods are currently long \u2014 please expect "
                "delays. You will receive a shipping confirmation when "
                "your order is on its way.",
                "",
                "Shipping to",
                "------------",
            ]
            address = self.shipping_address_lines()
            lines += [f"  {line}" for line in address] or [
                "  (no shipping address on file)"]
            lines.append("")

        lines += [
            "If something is wrong with your order, reply to this mail or "
            f"write to {settings.DEFAULT_FROM_EMAIL} and include your "
            "order number.",
        ]
        lines += self.stay_in_touch_lines()
        return "\n".join(lines)

    def stay_in_touch_lines(self) -> List[str]:
        """The mailing list, the Discord and the socials, as plain text.

        The receipt is the second half of the checkout success page: the buyer
        who closed the tab before reading it still gets this, grouped by whose
        account it is the same way the page groups them -- Holden, the
        company, Liberated Bread. Absolute URLs throughout: a receipt is built
        inside a Stripe webhook, where there is no request whose host could be
        borrowed.

        No tracking parameters and nothing pre-subscribed. The list here is a
        link to the same double opt-in signup as everywhere else: having
        bought something is not consent to be mailed about the next thing.
        """
        lines = [
            "",
            "Stay in touch",
            "-------------",
            "New books, new editions and the occasional discount: "
            f"{absolute_site_url(reverse('subscribe'))}",
        ]
        for target in follow_targets():
            # The name only. The blurb under each name on the checkout page is
            # copy for a page somebody chose to be on; a receipt earns its
            # place in an inbox by being a receipt, and the grouping alone
            # does the job the name is here for -- saying whose account the
            # URL under it is, which a flat list of links cannot.
            lines.append("")
            lines.append(target.name)
            if target.site:
                lines.append(f"  Website: {target.site}")
            if target.discord:
                lines.append(
                    f"  Discord: {absolute_site_url(reverse('discord'))}")
            lines += [f"  {link.label}: {link.url}" for link in target.links]
        lines += [
            "",
            "And if you have a minute: what made you buy this? Reply to this "
            "mail and tell us. It is the only way we find out, and it is what "
            "decides what gets written next.",
        ]
        return lines

    def send_receipt(self) -> bool:
        if not self.customer_email:
            message = (
                "no customer email on the order, so the receipt could "
                "not be sent")
            logger.warning("Order #%s: %s", self.pk, message)
            self._record_receipt_failure(message)
            return False
        try:
            send_mail(
                self.receipt_subject(),
                self.receipt_body(),
                settings.DEFAULT_FROM_EMAIL,
                [self.customer_email],
                fail_silently=False,
            )
        except Exception as e:
            logger.exception(
                "Order #%s: failed to send the buyer receipt.", self.pk)
            self._record_receipt_failure(f"{type(e).__name__}: {e}")
            return False
        Order.objects.filter(pk=self.pk).update(
            receipt_sent_at=timezone.now(), receipt_error="")
        self.receipt_sent_at = timezone.now()
        self.receipt_error = ""
        return True

    def _record_receipt_failure(self, message: str) -> None:
        try:
            Order.objects.filter(pk=self.pk).update(
                receipt_error=message[:2000])
        except Exception:
            logger.exception(
                "Order #%s: could not record the receipt failure.",
                self.pk)
        self.receipt_error = message[:2000]


class OrderItem(models.Model):
    """One line of an order, snapshotted at checkout time.

    Nothing here may be looked up live at fulfilment time: Product.price is
    edited in place, and a fresh Stripe Price is minted per CartProduct row,
    so both drift away from what was actually charged.
    """

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name='items')
    # Kept for convenience only -- deleting a product must not erase the
    # record of having sold it, hence SET_NULL and the name/price copies.
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True)
    product_name = models.CharField(max_length=250)
    unit_amount = models.IntegerField()
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    # What was actually bought. Starts as the cart quantity and is replaced by
    # Stripe's billed quantity once the webhook reconciles the order, because
    # checkout enables adjustable_quantity and the customer can change it.
    quantity = models.PositiveBigIntegerField(default=1)
    # What was in the cart when checkout started. Never overwritten, so an
    # adjustment stays auditable instead of vanishing.
    snapshot_quantity = models.PositiveBigIntegerField(default=1)
    price_id = models.CharField(max_length=250, blank=True)

    def total_amount(self) -> int:
        return self.unit_amount * self.quantity

    def original_amount(self) -> int:
        return self.unit_amount * self.snapshot_quantity

    def quantity_adjusted(self) -> bool:
        return self.quantity != self.snapshot_quantity

    def total_display_price(self) -> str:
        return _display_amount(self.total_amount())

    def unit_display_price(self) -> str:
        return _display_amount(self.unit_amount)

    def __str__(self) -> str:
        return f'{self.quantity} x {self.product_name}'

    def __repr__(self) -> str:
        return f'<OrderItem: {self.quantity} x {self.product_name}>'


class PurchaseFeedback(models.Model):
    """Why somebody bought, in their own words.

    Asked once, on the checkout success page, beside the mailing list signup
    and the socials. Nothing about fulfilment reads this: it is a note from
    the buyer, so the row *is* the record and the mail to the owner is
    best-effort on top of it.

    Hung off the Order rather than off a person, because most buyers have no
    account here -- the order number is the one name both sides of the
    conversation can use. CASCADE for the same reason: with the order gone
    there is no longer a "this" that the answer is about.
    """

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name='feedback')
    reason = models.TextField()
    # "You can quote me on that." Off unless they ticked it, and it records
    # permission rather than a preference: a note sent to us privately is not
    # a testimonial, and nothing in the codebase may treat one as public
    # without this being True.
    may_quote = models.BooleanField(default=False)
    # What to call them if they said we may quote them. Their own answer, not
    # the name Stripe put on the order: a buyer willing to be quoted is not
    # thereby willing to be quoted under their full legal name.
    quote_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Longest answer kept. Enforced on the form, so an over-long submission is
    # cut before it is written rather than rejected -- somebody who typed two
    # pages should not lose them to a validation error. Generous, because a
    # paragraph or two is exactly the thing worth reading; the ceiling only
    # exists so an open endpoint cannot be used to write a megabyte per POST.
    MAX_REASON_LENGTH = 2000

    # Notes one order may carry. A second thought later is welcome; this is
    # the ceiling that stops one session id being a way to fill the table.
    MAX_PER_ORDER = 5

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'Feedback on order #{self.order_id}'

    def __repr__(self) -> str:
        return f'<PurchaseFeedback: order #{self.order_id}>'

    @classmethod
    def has_room(cls, order: Order) -> bool:
        """Whether this order is allowed another note."""
        return cls.objects.filter(order=order).count() < cls.MAX_PER_ORDER

    def quote_permission(self) -> str:
        """The "may we quote you" answer, spelled out.

        Read by the owner deciding whether a sentence can go on the site, so
        it says what was actually granted rather than True/False: permission
        to be quoted without a name is a different permission from both of
        the other two answers.
        """
        if not self.may_quote:
            return "no"
        if self.quote_name:
            return f"yes, as {self.quote_name}"
        return "yes, but they did not say what to call them"

    def notify_owner(self) -> bool:
        """Mail the note to whoever gets order notifications.

        Never raises. This runs inside a page the buyer is looking at, and the
        row is already saved: a send failure costs a notification, not the
        answer, and the admin lists these anyway.
        """
        recipients = admin_recipients()
        if not recipients:
            logger.warning(
                "Order #%s: nobody to send purchase feedback to. Set the "
                "ORDER_NOTIFICATION_EMAIL env var.", self.order_id)
            return False
        body = "\n".join([
            f"Order #{self.order_id}",
            f"May we quote it: {self.quote_permission()}",
            "",
            self.reason,
        ])
        try:
            send_mail(
                f"[pigscanfly] Why they bought — order #{self.order_id}",
                body,
                settings.DEFAULT_FROM_EMAIL,
                recipients,
                fail_silently=False,
            )
        except Exception:
            logger.exception(
                "Order #%s: failed to mail the purchase feedback on. It is "
                "still in the admin.", self.order_id)
            return False
        return True


# The list every signup that does not name one lands on, and the list for
# people who want everything. Both live here rather than in main.mailing
# because the send layer below needs the second one, and mailing.py imports
# this module -- so this is the end of that dependency that can hold them.
DEFAULT_INTEREST_SLUG = "general"
ALL_INTEREST_SLUG = "all"


def absolute_site_url(path: str, request=None) -> str:
    """Absolute URL for a link that gets followed out of an email client.

    A relative path is useless there, and the request is not always around
    (a shell, a management command), hence the configured base URL as the
    fallback.
    """
    if request is not None:
        return request.build_absolute_uri(path)
    # site_base_url() reads SITE_BASE_URL, not a mailing-list-specific setting:
    # the site has one absolute base, and the emailed download links use it too.
    return f"{site_base_url()}{path}"


def mailing_list_from_email() -> str:
    return (getattr(settings, "MAILING_LIST_FROM_EMAIL", "")
            or settings.DEFAULT_FROM_EMAIL)


def send_batch_size() -> int:
    """Recipients per batch.

    An accessor rather than two reads of the setting: the send page shows this
    number on its button and send_batch() acts on it, and those disagreeing
    would make the button lie about what it does.

    getattr because django-stubs cannot see settings this project adds; the
    default here is the only one, and settings.py sets the value anyway.
    """
    return getattr(settings, "MAILING_LIST_SEND_BATCH_SIZE", 100)


class SuppressedAddress(models.Model):
    """An address that must not be imported onto a list, ever.

    The opposite of a mailing list: people who told us to stop, whose mail
    bounced for good, or who complained. Consulted on every import, because
    the files an import comes out of -- a Mailchimp export, a spreadsheet
    somebody kept by hand -- have no idea what has happened since they were
    written, and re-adding one of these addresses is the mistake that gets a
    domain blocked rather than merely complained about.

    Kept separate from the unsubscribed flag on a subscription, which only
    says "not this list": this says "not at all".
    """

    email = models.EmailField(max_length=254, unique=True)
    reason = models.CharField(
        max_length=200, blank=True,
        help_text="For whoever reads this later: bounced, complained, asked "
                  "to be removed, and so on.")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="suppressed_addresses")

    class Meta:
        ordering = ["email"]
        verbose_name = "suppressed address"
        verbose_name_plural = "suppressed addresses (never email)"

    def __str__(self) -> str:
        return self.email

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    @classmethod
    def matching(cls, emails) -> set:
        """Which of these addresses are suppressed, lower-cased.

        One query for the whole import rather than one per row.
        """
        wanted = {normalize_email(email) for email in emails}
        if not wanted:
            return set()
        # Lower-cased on both sides. save() normalises, but a row written by
        # bulk_create, loaddata or raw SQL does not go through it, and a
        # suppressed address this misses is one an import happily adds.
        return set(cls.objects.annotate(
            normalized=Lower("email")).filter(
                normalized__in=wanted).values_list("normalized", flat=True))


class MailingListMessage(models.Model):
    """A mailing, and the record of who it actually reached.

    The subscribers themselves are django-newsletter's: one Newsletter is one
    interest area, and its Subscription rows are the addresses, along with the
    double opt-in and unsubscribe flows that come with them. This model exists
    for the one thing that app cannot do -- send a single mailing across
    several of those lists, once per person -- which is why it is a send
    record and nothing more.

    Sending happens in batches with one Delivery row per recipient, and a
    recipient with a Delivery row is never picked up again. That is what makes
    a send resumable: the admin sends a batch per click, and a killed worker,
    a browser reload or a second click continues where it stopped instead of
    mailing the whole list a second time.
    """

    class Status(models.TextChoices):
        DRAFT = 'D', 'draft'
        SENDING = 'G', 'sending'
        SENT = 'S', 'sent'

    subject = models.CharField(max_length=200)
    body = models.TextField(
        help_text=(
            "Plain text. {{ name }}, {{ email }} and {{ unsubscribe_url }} "
            "are substituted per recipient; an unsubscribe link is appended "
            "if you leave it out."))
    # Empty means everyone. Somebody subscribed to several of the selected
    # lists is still mailed once -- see recipients().
    interests: "models.ManyToManyField[Any, Any]" = models.ManyToManyField(
        "newsletter.Newsletter", blank=True, related_name="mailings",
        help_text="Which lists to send to. Leave empty to send to every "
                  "confirmed subscriber of every list.")
    status = models.CharField(
        max_length=1, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="mailing_list_messages")
    # When the first batch went out. This closes the audience: see
    # recipients(). Without it the list is recomputed per batch, so somebody
    # subscribing mid-send gets a mailing that predates them -- and a finished
    # mailing quietly becomes unfinished again every time anybody signs up.
    send_started_at = models.DateTimeField(null=True, blank=True)
    # When the last batch that finished the list went out.
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.subject

    def clean(self):
        """Reject a body that will not render before it is a send problem.

        Without this a stray {% or a mistyped tag is only discovered per
        recipient, at send time, as every single delivery failing.
        """
        from django.template import Template, TemplateSyntaxError

        try:
            Template(self.body)
        except TemplateSyntaxError as e:
            raise ValidationError({"body": f"That will not render: {e}"})

    def recipients(self) -> "models.QuerySet":
        """Every confirmed subscriber this mailing is addressed to.

        The lists it names, plus everyone on the All list -- see below. No
        lists at all means every confirmed subscriber.

        Annotated with the address to use, because a subscription is either an
        address or a site account and only one of the two columns is filled in;
        resolving it in SQL is what lets the exclusion below dedupe either
        kind.

        Once the first batch has gone out the audience is closed to whoever
        was already subscribed: a mailing is a thing that was sent at a
        moment, not a standing subscription, and somebody who signed up
        halfway through it did not sign up for it.
        """
        from newsletter.models import Subscription

        # Lower-cased because case is not identity for a mailbox and this is
        # the only thing standing between one human and two copies:
        # django-newsletter's own subscribe page and admin do not normalise, so
        # Bob@Example.COM and bob@example.com are two rows for one person.
        recipients = Subscription.objects.filter(
            subscribed=True, unsubscribed=False).annotate(
                address=Lower(Coalesce(
                    NullIf("email_field", models.Value("")), "user__email")))
        # The never-email list, consulted here and not only at import time.
        # Suppressing takes people off their lists, but a row that slipped
        # past that -- a user-linked subscription, a case variant, one created
        # afterwards -- must still never be mailed.
        # Lower on both sides, for the same reason matching() does it: save()
        # normalises, but bulk_create, loaddata and raw SQL do not, and a
        # suppressed address this misses is one we mail.
        recipients = recipients.exclude(
            address__in=SuppressedAddress.objects.annotate(
                normalized=Lower("email")).values("normalized"))
        interests = self._interest_list()
        if interests:
            addressed = models.Q(newsletter__in=interests)
            if self.includes_all_list():
                # Everyone on the All list is included whatever public list
                # this names, which is the whole point of that list: they asked
                # for everything, so a mailing about a topic they never picked
                # -- including one added long after they subscribed -- is
                # exactly what they signed up for. See includes_all_list() for
                # why hidden lists (the internal test list) do not pull it in.
                addressed |= models.Q(newsletter__slug=ALL_INTEREST_SLUG)
            recipients = recipients.filter(addressed)
        if self.send_started_at is not None:
            # create_date is the fallback because subscribe_date is nullable
            # and a row loaded from a fixture bypasses the save() that would
            # have set it. Without the fallback such a subscriber silently
            # drops out of a send once the first batch has gone -- which is
            # the worse direction to fail than including a late one.
            recipients = recipients.filter(
                models.Q(subscribe_date__lte=self.send_started_at)
                | models.Q(subscribe_date__isnull=True,
                           create_date__lte=self.send_started_at))
        return recipients.select_related("newsletter", "user").order_by("pk")

    def _interest_list(self) -> list:
        """The named lists, or [] before the row exists (no m2m to read yet)."""
        return list(self.interests.all()) if self.pk else []

    def includes_all_list(self) -> bool:
        """Whether the All list is pulled in on top of what this names.

        True when it names at least one public list and is not addressed to All
        already. Kept here beside recipients(), so the admin and the send page
        describe what the query actually does instead of restating the rule.

        Hidden lists (the internal test list) do not count as public, which is
        what keeps a mailing aimed at the test list from going to the whole All
        list -- the opposite of what that list is for.
        """
        interests = self._interest_list()
        return (any(interest.visible for interest in interests)
                and not any(interest.slug == ALL_INTEREST_SLUG
                            for interest in interests))

    def audience_description(self) -> str:
        interests = self._interest_list()
        if not interests:
            return "everyone"
        names = ", ".join(interest.title for interest in interests)
        if self.includes_all_list():
            return f"{names}, and everyone on All"
        return names

    def pending_recipients(self) -> "models.QuerySet":
        """Who is still owed a copy.

        Excluded by *address*, not by subscription row: somebody on two of the
        selected lists has two rows and is owed one copy, so once either row
        has been delivered the other is not pending. This is also what makes a
        duplicate drain out of the count instead of sitting there forever
        keeping the send from finishing.
        """
        if self.status == self.Status.SENT:
            # Finished. Its audience is not recomputed, so editing the lists
            # on a sent mailing cannot leave it showing work to do forever.
            return self.recipients().none()
        # Lower() on both sides, the same as the suppression check above and
        # for the same reason: `address` is folded, so comparing it against a
        # raw column would let a delivery row whose email skipped save() --
        # a future bulk_create or data migration -- fail to match and mail its
        # recipient a second time. save() normalises today; this does not lean
        # on that being the only writer forever.
        delivered = self.deliveries.annotate(
            normalized=Lower("email")).values("normalized")
        claimed = self.deliveries.exclude(
            subscription__isnull=True).values_list("subscription_id", flat=True)
        # Excluded by subscription as well as by address: an address that
        # changes after its delivery row is written would otherwise come back
        # as pending under the new address, be refused by the per-subscription
        # constraint, and pin the send open at "1 still to go" forever.
        return self.recipients().exclude(
            address__in=delivered).exclude(pk__in=claimed)

    def recipient_count(self) -> int:
        return self.recipients().count()

    def pending_count(self) -> int:
        return self.pending_recipients().count()

    def sent_count(self) -> int:
        return self.deliveries.filter(
            status=MailingListDelivery.Status.SENT).count()

    def failed_count(self) -> int:
        return self.deliveries.filter(
            status=MailingListDelivery.Status.FAILED).count()

    @staticmethod
    def unsubscribe_url(subscription, request=None) -> str:
        # Imported here rather than at module level: main.mailing imports this
        # module for SuppressedAddress, so the other direction has to be lazy.
        from main.mailing import unsubscribe_url

        return unsubscribe_url(subscription, request)

    def render_for(self, subscription, request=None,
                   unsubscribe_url=None) -> str:
        """The body as this one recipient sees it.

        The unsubscribe link is appended when the body does not already place
        it, because a mailing without one is the kind of thing that gets a
        domain listed rather than merely complained about. The caller can pass
        the link in; _build_email needs it for the header anyway, and reversing
        it twice per recipient is a query-shaped waste.
        """
        from django.template import Context, Template

        unsubscribe_url = (unsubscribe_url
                           or self.unsubscribe_url(subscription, request))
        context = Context({
            "name": subscription.name or "",
            "email": subscription.email,
            "interest": subscription.newsletter,
            "unsubscribe_url": unsubscribe_url,
        }, autoescape=False)
        body = str(Template(self.body).render(context))
        if unsubscribe_url not in body:
            body = (f"{body.rstrip()}\n\n--\n"
                    f"You are getting this because you subscribed to "
                    f"{subscription.newsletter} at pigscanfly.ca.\n"
                    f"Unsubscribe: {unsubscribe_url}\n")
        return body

    def send_test(self, address: str, request=None) -> None:
        """Send one copy to the author, with no Delivery row and no dedupe.

        Uses whatever subscription that address already has so the test
        renders through exactly the same path as the real thing, including a
        working unsubscribe link.
        """
        from newsletter.models import Subscription

        # Confirmed subscriptions only, and one of this mailing's own lists
        # where it names any: a test is meant to be a faithful preview, and
        # rendering somebody's unsubscribe link for a list they already left
        # is not one. It also stops the open signup endpoint being a way to
        # make any address a legal test target.
        candidates = Subscription.objects.filter(
            subscribed=True, unsubscribed=False).filter(
                models.Q(email_field__iexact=address)
                | models.Q(user__email__iexact=address))
        interests = self._interest_list()
        subscription = (candidates.filter(newsletter__in=interests).first()
                        if interests else None) or candidates.first()
        if subscription is None:
            raise ValueError(
                f"{address} has no confirmed subscription, so there is no "
                "unsubscribe link to render and the test would not be a "
                "faithful preview. Subscribe it first -- the test list in "
                "mailing_list_test_group.yaml exists for this.")
        message = self._build_email(subscription, request)
        message.subject = f"[test] {message.subject}"
        message.to = [address]
        message.send(fail_silently=False)

    def send_batch(self, limit: Optional[int] = None,
                   request=None) -> Tuple[int, int]:
        """Mail the next batch. Returns (sent, failed).

        One SMTP connection for the batch, one Delivery row per recipient
        claimed immediately before its send. Claiming per recipient rather
        than per batch is deliberate: if the process dies halfway through, the
        rows already written are exactly the addresses that already have the
        mail.
        """
        limit = limit or send_batch_size()
        if self.status == self.Status.SENT:
            # Already finished. Reopening it would mail an old message to
            # whoever has subscribed since.
            return (0, 0)
        batch = list(self.pending_recipients()[:limit])
        if not batch:
            self._finish()
            return (0, 0)
        if self.send_started_at is None:
            # Written before the first mail goes out, because it is what
            # closes the audience -- setting it afterwards would leave a
            # window where a new subscriber joins the send in progress.
            #
            # Guarded on the column rather than on this instance: two senders
            # that both loaded the message before either wrote would otherwise
            # both take this branch and the later timestamp would win, moving
            # the freeze point forward and letting in somebody who subscribed
            # after the first batch went out.
            MailingListMessage.objects.filter(
                pk=self.pk, send_started_at__isnull=True).update(
                    status=self.Status.SENDING,
                    send_started_at=timezone.now())
            self.refresh_from_db(fields=["status", "send_started_at"])
        sent = failed = 0
        with smtp_connection() as connection:
            for subscription in batch:
                # The row is claimed *before* the send, and the unique
                # constraint on (message, email) is what makes the claim
                # exclusive: two people clicking send at the same moment
                # cannot both take the same address -- not even via two
                # different subscription rows for it -- so nobody gets the
                # mailing twice. The cost is that a process killed between
                # the claim and the send leaves a row saying sent for a mail
                # that never went, which is the right way round for a mailing
                # list, where one missed copy beats one duplicate.
                delivery = self._claim(subscription)
                if delivery is None:
                    continue
                try:
                    email = self._build_email(subscription, request)
                    email.connection = connection
                    email.send(fail_silently=False)
                except Exception as e:
                    # One bad address does not stop the mailing; it is
                    # recorded, counted, and not retried by the next batch.
                    logger.exception(
                        "Could not send message %s to %s.",
                        self.pk, subscription.email)
                    delivery.status = MailingListDelivery.Status.FAILED
                    delivery.error = str(e)[:500]
                    delivery.save(update_fields=["status", "error"])
                    failed += 1
                    continue
                sent += 1
        if not self.pending_recipients().exists():
            self._finish()
        return (sent, failed)

    def _claim(self, subscription) -> Optional["MailingListDelivery"]:
        """Take this recipient, or None if somebody else already has them.

        Its own transaction: an IntegrityError marks the surrounding atomic
        block unusable, and on the admin's send page there is one wrapping
        the whole request.
        """
        try:
            with transaction.atomic():
                # Re-read inside the transaction: a batch is materialised up
                # front and then mailed one at a time over one connection, so
                # somebody who unsubscribes while it is running would
                # otherwise still get this copy.
                still_wanted = type(subscription).objects.filter(
                    pk=subscription.pk, subscribed=True,
                    unsubscribed=False).exists()
                if not still_wanted:
                    logger.info(
                        "Skipping %s for message %s: they are no longer "
                        "subscribed.", subscription.email, self.pk)
                    return None
                return MailingListDelivery.objects.create(
                    message=self, subscription=subscription,
                    email=subscription.email)
        except IntegrityError:
            logger.info(
                "Message %s was already claimed for %s; not sending again.",
                self.pk, subscription.email)
            return None

    def _finish(self) -> None:
        if self.status == self.Status.SENT:
            return
        if self.send_started_at is None:
            # Nothing was ever sent -- an empty audience, or the last pending
            # recipient unsubscribing between the view's check and ours.
            # Marking it sent would be a lie and, because a sent message is
            # never reopened and status is read-only in the admin, would make
            # it permanently unsendable.
            return
        self.status = self.Status.SENT
        self.sent_at = timezone.now()
        MailingListMessage.objects.filter(pk=self.pk).update(
            status=self.status, sent_at=self.sent_at)

    def _build_email(self, subscription, request=None):
        link = self.unsubscribe_url(subscription, request)
        email = EmailMessage(
            subject=self.subject,
            body=self.render_for(subscription, request, unsubscribe_url=link),
            from_email=mailing_list_from_email(),
            to=[subscription.get_recipient()])
        # RFC 2369. Mail clients put a real unsubscribe button on the message
        # when this is present, which people use instead of the "this is spam"
        # button.
        email.extra_headers = {"List-Unsubscribe": f"<{link}>"}
        return email


class MailingListDelivery(models.Model):
    """One recipient's copy of one mailing. Claimed before the send."""

    class Status(models.TextChoices):
        SENT = 'S', 'sent'
        FAILED = 'F', 'failed'

    message = models.ForeignKey(
        MailingListMessage, on_delete=models.CASCADE,
        related_name="deliveries")
    # SET_NULL, not CASCADE: this row is the record that a copy went out, and
    # deleting a subscriber must not erase it -- doing so both loses the audit
    # trail and lets an unfinished mailing send that address a second copy.
    subscription = models.ForeignKey(
        "newsletter.Subscription", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="mailing_deliveries")
    # The address the copy went to, copied rather than followed through the
    # subscription: it is what the uniqueness below is enforced on, and it
    # outlives the subscription being edited or deleted. Lower-cased, because
    # case is not identity for a mailbox.
    email = models.EmailField(max_length=254)
    status = models.CharField(
        max_length=1, choices=Status.choices, default=Status.SENT)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # The guard against sending twice, enforced on the address rather
            # than only the subscription row: somebody on two of the lists a
            # mailing names has two rows, and two concurrent senders could
            # otherwise take one each and both mail them.
            models.UniqueConstraint(
                fields=["message", "email"],
                name="unique_delivery_per_address"),
            models.UniqueConstraint(
                fields=["message", "subscription"],
                name="unique_delivery_per_message"),
        ]

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f'{self.email}: {self.get_status_display()}'
