import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, send_mail
from django.db import IntegrityError, models, transaction
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
from main.utils import admin_recipients, normalize_email, smtp_connection
from typing import Any, Dict, List, Optional, Tuple, cast

from easy_thumbnails.files import get_thumbnailer

logger = logging.getLogger(__name__)

# Everything on this site is priced and charged in USD; it is hardcoded at
# every Stripe call site. Orders store it explicitly anyway so a historical
# order still says what it was actually charged in if that ever changes.
DEFAULT_CURRENCY = "usd"


# Create your models here.
class Product(models.Model):
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
    stock = models.PositiveIntegerField(
        default=0,
        db_default=0,
        help_text=(
            "Manually gates whether physical books can be purchased here; "
            "it does not cap order quantity or decrement automatically."
        ),
    )
    date_available = models.DateField(null=True, blank=True)
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

    def get_display_price(self) -> str:
        formatted_price = "{0:.2f}".format(self.price / 100)
        if self.preorder_only:
            return f"Pre-order: {formatted_price}"
        else:
            return formatted_price

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
        return format_html("{}", formatted)

    def get_feed_description(self) -> str:
        """The same copy as plain text, for the Google product feed.

        The feed is XML, so markup from get_display_text() would arrive at
        Google as escaped angle brackets and show up literally in the listing.
        """
        if self.print_isbn:
            return f"{self.description}\n\n{self.SIGNED_ON_REQUEST_NOTE}"
        return self.description

    def get_feed_price(self) -> str:
        """Bare numeric price for the feed's <g:price>.

        Not get_display_price(): that prefixes "Pre-order: " for preorder
        products, which is fine on the page and invalid in the feed -- Google
        rejects a price it cannot parse.
        """
        return "{0:.2f}".format(self.price / 100)

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

    class Meta:
        # Without this, two concurrent adds of the same product race into two
        # rows, and every later lookup of that (cart, product) blows up with
        # MultipleObjectsReturned.
        constraints = [
            models.UniqueConstraint(
                fields=['cart', 'product'], name='unique_cart_product'),
        ]

    def generate_price_id(self):
        external_product_id = self.product.ensure_external_product_id()
        if self.product.mode == Product.Modes.PAYMENT:
            price_id = Payments.create_price(
                external_product_id, self.product.price, currency="usd",
                pay_what_you_want=self.product.is_pwyw)
        else:
            # Stripe's custom_unit_amount is payment-mode only, so a
            # recurring pay-what-you-want price cannot exist. create_price
            # refuses the combination rather than quietly billing the preset
            # every year as if it were a fixed price.
            price_id = Payments.create_price(
                external_product_id, self.product.price, currency="usd",
                interval="year", pay_what_you_want=self.product.is_pwyw
            )
        return price_id

    def save(self, *args, **kwargs):
        if not self.price_id:
            self.price_id = self.generate_price_id()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f'{self.product.name}'

    def __repr__(self) -> str:
        return f'<CartProduct: {self.product.name}>'

    def total_price(self):
        return (self.product.price * self.quantity)

    def total_display_price(self):
        return "{0:.2f}".format(self.total_price() / 100)


def _display_amount(cents: Optional[int]) -> str:
    if cents is None:
        return "-"
    return "{0:.2f}".format(cents / 100)


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
                unit_amount=cp.product.price,
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
                # The snapshot holds the suggested amount, because that is
                # what the line was worth when the cart was frozen. What the
                # buyer actually chose to pay is only in the order total.
                line += ("   [pay-what-you-want: shown at the suggested "
                         "amount, see the order total for what was paid]")
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
