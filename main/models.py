import logging
import secrets

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, get_connection, send_mail
from django.db import IntegrityError, models, transaction
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from main.digital import (
    DigitalAssetError, download_url, link_lifetime_days, open_asset)
from main.payments import Payments
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
    external_product_id = models.CharField(max_length=250, blank=True, null=True)
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

    def get_thumb(self):
        t = None
        try:
            if self.image_name:
                from static_thumbnails.templatetags.static_thumbnails import static_storage
                t = get_thumbnailer(
                    static_storage,
                    relative_name=f"assets/images/{self.image_name}")
            else:
                t = get_thumbnailer(self.image)
        except Exception as e:
            logger.warning(f"Got exception {e} trying to load thumbnailer.")
            return self.get_image_url()
        try:
            th = t.get_thumbnail({'size': (290, 380)})
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
            ("Read on O'Reilly Safari (free trial)",
             self.OREILLY_SAFARI_URL if self.on_oreilly_safari else None),
            ("Buy on Kindle (e-book)", self.get_kindle_link()),
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
        around it. The only markup here is the paragraph wrapper this method
        adds; the description itself is escaped, so a stray angle bracket in
        admin-entered copy renders as text instead of as live HTML.
        """
        if self.print_isbn:
            return format_html(
                "{}<p>{}</p>", self.description, self.SIGNED_ON_REQUEST_NOTE)
        return format_html("{}", self.description)

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
                ", ".join(p for p in [self.shipping_city, self.shipping_state] if p),
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
        recipients = []
        for entry in getattr(settings, "ADMINS", None) or []:
            # Django 5.2 requires 2-tuples, but be forgiving about the bare
            # string form so a mis-set env var is not a crash in a webhook.
            recipients.append(entry if isinstance(entry, str) else entry[1])
        return [r for r in recipients if r]

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


def generate_subscription_token() -> str:
    """An unguessable id for the confirm and unsubscribe links.

    Those links are the only authentication on their endpoints -- they arrive
    by email and get followed by somebody who is not logged in -- so the token
    has to be random enough that nobody can walk it to reach a stranger's
    subscription. 32 bytes of urlsafe base64 is 43 characters.
    """
    return secrets.token_urlsafe(32)


class InterestArea(models.Model):
    """What a subscriber said they wanted to hear about.

    One row per group: general updates, Distributed Computing 4 Kids, a
    hardware project, whatever. Anyone who does not pick one lands in the
    general group.

    The slug is what an embedded signup form on another site posts, so it
    ends up in that site's markup -- renaming one silently breaks every form
    already pasted somewhere, which is why the help text says to add a new
    area instead.
    """

    slug = models.SlugField(
        max_length=64, unique=True,
        help_text=(
            "Used in signup URLs and in signup forms embedded on other "
            "sites. Changing it breaks every form already using the old "
            "value; add a new area instead."))
    name = models.CharField(max_length=120)
    description = models.CharField(
        max_length=300, blank=True,
        help_text="Shown next to the option on the signup form.")
    # Hidden from the signup form rather than deleted: an area with
    # subscribers cannot be deleted (see the PROTECT below), and their record
    # of what they opted into should outlive our interest in the topic.
    active = models.BooleanField(
        default=True,
        help_text="Inactive areas stop appearing on signup forms and stop "
                  "accepting new signups. Existing subscribers are kept.")
    # What makes the "All" group mean what it says. Without it, picking "All"
    # would put somebody in a group named after everything and then leave
    # them out of every mailing addressed to a particular topic -- the exact
    # opposite of what they asked for.
    catch_all = models.BooleanField(
        default=False, db_default=False,
        help_text="Subscribers here get every mailing, whichever groups it "
                  "is addressed to.")
    # Curated rather than alphabetical: the order these appear in on the
    # signup form is an editorial choice, not a fact about their names.
    sort_order = models.PositiveSmallIntegerField(
        default=100, db_default=100,
        help_text="Lower sorts earlier on the signup form. Ties break by "
                  "name.")
    created_at = models.DateTimeField(auto_now_add=True)

    # The group every signup that does not name one lands in. Created by a
    # data migration, and re-created by get_default() if it ever goes away.
    DEFAULT_SLUG = "general"

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name

    @classmethod
    def get_default(cls) -> "InterestArea":
        """The fallback group, created on demand.

        A signup that names no area is the common case (the footer form), so
        this has to work even on a database where the data migration's row was
        renamed or deleted, rather than 500ing the signup endpoint.
        """
        area, _created = cls.objects.get_or_create(
            slug=cls.DEFAULT_SLUG,
            defaults={"name": "General updates",
                      "description": "News from Pigs Can Fly Labs."})
        return area

    @classmethod
    def signup_choices(cls) -> "models.QuerySet":
        """The groups to offer on a signup form, in their curated order.

        Which one is *pre-selected* is a separate question and is not this:
        the form marks the general group selected, because the rule is that
        not choosing means general, and the group listed first is an
        editorial decision that must not quietly become the default.
        """
        return cls.objects.filter(active=True)

    def subscriber_count(self) -> int:
        return self.subscriptions.filter(
            status=MailingListSubscription.Status.SUBSCRIBED).count()


class MailingListSubscription(models.Model):
    """One address in one interest group.

    Signups arrive at a CSRF-exempt endpoint that anything on the internet can
    post to -- that is the point, forms on other sites use it -- so an address
    is not on the list until somebody clicks the link in the confirmation
    mail. Nothing is sent to a PENDING row but that one confirmation.
    """

    class Status(models.TextChoices):
        PENDING = 'P', 'pending confirmation'
        SUBSCRIBED = 'S', 'subscribed'
        UNSUBSCRIBED = 'U', 'unsubscribed'

    email = models.EmailField(max_length=254)
    name = models.CharField(max_length=200, blank=True)
    # PROTECT: deleting a group would otherwise take every record of who asked
    # for it -- and of the consent they gave -- with it.
    interest = models.ForeignKey(
        InterestArea, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(
        max_length=1, choices=Status.choices, default=Status.PENDING)
    # Free text, e.g. "site-footer", "dc4k-embed", "import:2026-07-26.csv".
    # Worth having when a list turns out to be full of addresses nobody
    # remembers collecting.
    source = models.CharField(max_length=200, blank=True)
    token = models.CharField(
        max_length=64, unique=True, default=generate_subscription_token,
        editable=False)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One row per address per group. Signing up twice updates the row
            # that is already there rather than making a second one, so an
            # unsubscribe cannot leave a forgotten duplicate behind that still
            # gets mail.
            models.UniqueConstraint(
                fields=["email", "interest"],
                name="unique_subscription_per_interest"),
        ]
        indexes = [models.Index(fields=["email"])]

    def __str__(self) -> str:
        return f'{self.email} ({self.interest})'

    @staticmethod
    def normalize_email(email: str) -> str:
        """Lower-case and strip, so Foo@Example.com is not a second row.

        The local part is case-sensitive per the RFC and case-insensitive at
        every mail host anybody actually uses; treating them as one address is
        what a subscriber expects and is what keeps the unique constraint
        meaningful.
        """
        return (email or "").strip().lower()

    def save(self, *args, **kwargs):
        self.email = self.normalize_email(self.email)
        super().save(*args, **kwargs)

    @classmethod
    def subscribe(cls, email: str, interest: Optional[InterestArea] = None,
                  name: str = "", source: str = "", ip: Optional[str] = None,
                  confirmed: bool = False) -> "MailingListSubscription":
        """Record a signup and return the row it landed in.

        No interest means the general group: that is the default for anyone
        who did not say otherwise when they signed up.

        `confirmed` is for signups we already have consent for -- a CSV the
        owner imported, an address moved over from the old newsletter app --
        and goes straight to SUBSCRIBED. Everything from the web goes to
        PENDING and waits for the link to be clicked.

        Signing up an address that is already SUBSCRIBED is a no-op rather
        than a reset to PENDING: otherwise anyone could quietly knock a
        subscriber off the list by re-posting their address.
        """
        interest = interest or InterestArea.get_default()
        email = cls.normalize_email(email)
        subscription, created = cls.objects.get_or_create(
            email=email, interest=interest,
            defaults={"name": name, "source": source, "ip": ip,
                      "status": cls.Status.PENDING})
        if name and not subscription.name:
            subscription.name = name
        if confirmed:
            subscription.mark_subscribed()
        elif created:
            if name:
                subscription.save()
        elif subscription.status == cls.Status.UNSUBSCRIBED:
            # Coming back after unsubscribing means confirming again. We do
            # not resurrect a withdrawn consent on somebody else's say-so, and
            # the old token stops working so a forwarded mail cannot do it
            # either.
            subscription.status = cls.Status.PENDING
            subscription.token = generate_subscription_token()
            subscription.save()
        else:
            subscription.save()
        return subscription

    def mark_subscribed(self) -> None:
        """Consent recorded: from here on a mailing may go to this address."""
        self.status = self.Status.SUBSCRIBED
        self.confirmed_at = self.confirmed_at or timezone.now()
        self.unsubscribed_at = None
        self.save()

    def unsubscribe(self) -> None:
        self.status = self.Status.UNSUBSCRIBED
        self.unsubscribed_at = timezone.now()
        self.save()

    def confirm_url(self, request=None) -> str:
        return absolute_site_url(
            reverse("mailing-list-confirm", args=[self.token]), request)

    def unsubscribe_url(self, request=None) -> str:
        return absolute_site_url(
            reverse("mailing-list-unsubscribe", args=[self.token]), request)

    def send_confirmation_email(self, request=None) -> bool:
        """Ask the address to confirm. Returns whether the mail went out.

        Best effort by design: a dead SMTP server must not turn a signup into
        a 500 on somebody else's site. The row stays PENDING, so signing up
        again sends another one.
        """
        context = {
            "subscription": self,
            "interest": self.interest,
            "confirm_url": self.confirm_url(request),
            "unsubscribe_url": self.unsubscribe_url(request),
        }
        subject = render_to_string(
            "email/mailing_list_confirm_subject.txt", context).strip()
        body = render_to_string("email/mailing_list_confirm.txt", context)
        try:
            send_mail(subject, body, mailing_list_from_email(), [self.email],
                      fail_silently=False)
        except Exception:
            logger.exception(
                "Could not send the mailing list confirmation to %s; the "
                "signup stays pending.", self.email)
            return False
        return True


def absolute_site_url(path: str, request=None) -> str:
    """Absolute URL for a link that gets followed out of an email client.

    A relative path is useless there, and the request is not always around
    (the CSV import, a management command), hence the configured base URL as
    the fallback.
    """
    if request is not None:
        return request.build_absolute_uri(path)
    base = getattr(settings, "MAILING_LIST_BASE_URL",
                   "https://www.pigscanfly.ca").rstrip("/")
    return f"{base}{path}"


def mailing_list_from_email() -> str:
    return (getattr(settings, "MAILING_LIST_FROM_EMAIL", None)
            or settings.DEFAULT_FROM_EMAIL)


class MailingListMessage(models.Model):
    """A mailing, and the record of who it actually reached.

    Sending happens in batches with one Delivery row per recipient, and a
    recipient with a Delivery row is never picked up again. That is what makes
    a send resumable: the admin sends a batch per click and a killed worker,
    a browser reload or a re-run of the management command continues where it
    stopped instead of mailing the whole list a second time.
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
    # Empty means everyone. A subscriber in several of the selected groups is
    # still mailed once -- see recipients().
    interests = models.ManyToManyField(
        InterestArea, blank=True, related_name="messages",
        help_text="Which groups to send to. Leave empty to send to every "
                  "confirmed subscriber.")
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

        Anyone in a catch-all group ("All") is included whatever groups this
        names -- that is what they signed up for. Ordered by pk so batching
        is stable, and so the row that gets the mail when somebody is in
        several selected groups is always the same one.

        Once the first batch has gone out the audience is closed to whoever
        was already on the list: a mailing is a thing that was sent at a
        moment, not a standing subscription, and somebody who signed up
        halfway through it did not sign up for it.
        """
        recipients = MailingListSubscription.objects.filter(
            status=MailingListSubscription.Status.SUBSCRIBED)
        interests = list(self.interests.all()) if self.pk else []
        if interests:
            recipients = recipients.filter(
                models.Q(interest__in=interests)
                | models.Q(interest__catch_all=True))
        if self.send_started_at is not None:
            # confirmed_at is when they joined the list proper. The fallback
            # covers a row marked subscribed by hand, which has no
            # confirmation to date from.
            recipients = recipients.filter(
                models.Q(confirmed_at__lte=self.send_started_at)
                | models.Q(confirmed_at__isnull=True,
                           created_at__lte=self.send_started_at))
        return recipients.select_related("interest").order_by("pk")

    def pending_recipients(self) -> "models.QuerySet":
        """Who is still owed a copy.

        Excluded by *address*, not by subscription row: somebody in two of
        the selected groups has two rows and is owed one copy, so once either
        row has been delivered the other is not pending. This is also what
        makes a duplicate drain out of the count instead of sitting there
        forever keeping the send from finishing.
        """
        delivered = self.deliveries.values_list("email", flat=True)
        return self.recipients().exclude(email__in=delivered)

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

    def render_for(self, subscription: "MailingListSubscription",
                   request=None) -> str:
        """The body as this one recipient sees it.

        The unsubscribe link is appended when the body does not already place
        it, because a mailing without one is the kind of thing that gets a
        domain listed rather than merely complained about.
        """
        from django.template import Context, Template

        unsubscribe_url = subscription.unsubscribe_url(request)
        context = Context({
            "name": subscription.name,
            "email": subscription.email,
            "interest": subscription.interest,
            "unsubscribe_url": unsubscribe_url,
        }, autoescape=False)
        body = str(Template(self.body).render(context))
        if unsubscribe_url not in body:
            body = (f"{body.rstrip()}\n\n--\n"
                    f"You are getting this because you subscribed to "
                    f"{subscription.interest} at pigscanfly.ca.\n"
                    f"Unsubscribe: {unsubscribe_url}\n")
        return body

    def send_test(self, address: str, request=None) -> None:
        """Send one copy to the author, with no Delivery row and no dedupe.

        Uses an unsaved subscription so the test renders through exactly the
        same path as the real thing -- including a working unsubscribe link
        for whatever `address` is actually subscribed to, if anything.
        """
        subscription = MailingListSubscription.objects.filter(
            email=MailingListSubscription.normalize_email(address)).first()
        if subscription is None:
            subscription = MailingListSubscription(
                email=address, interest=InterestArea.get_default(),
                token=generate_subscription_token())
        message = self._build_email(subscription, request)
        message.subject = f"[test] {message.subject}"
        message.to = [address]
        message.send(fail_silently=False)

    def send_batch(self, limit: Optional[int] = None, request=None) -> Tuple[int, int]:
        """Mail the next batch. Returns (sent, failed).

        One SMTP connection for the batch, one Delivery row per recipient
        written immediately after its send returns. Writing the row per
        recipient rather than per batch is deliberate: if the process dies
        halfway through, the rows already written are exactly the addresses
        that already have the mail.
        """
        limit = limit or getattr(settings, "MAILING_LIST_SEND_BATCH_SIZE", 100)
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
            self.send_started_at = timezone.now()
            self.status = self.Status.SENDING
            MailingListMessage.objects.filter(pk=self.pk).update(
                status=self.status, send_started_at=self.send_started_at)
        sent = failed = 0
        connection = get_connection(fail_silently=False)
        try:
            connection.open()
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
                delivery = self._claim(
                    subscription, MailingListDelivery.Status.SENT)
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
        finally:
            try:
                connection.close()
            except Exception:
                logger.warning(
                    "Could not close the SMTP connection for message %s.",
                    self.pk, exc_info=True)
        if not self.pending_recipients().exists():
            self._finish()
        return (sent, failed)

    def _claim(self, subscription: "MailingListSubscription", status: str,
               error: str = "") -> Optional["MailingListDelivery"]:
        """Take this recipient, or None if somebody else already has them.

        Its own transaction: an IntegrityError marks the surrounding atomic
        block unusable, and on the admin's send page there is one wrapping
        the whole request.
        """
        try:
            with transaction.atomic():
                return MailingListDelivery.objects.create(
                    message=self, subscription=subscription,
                    email=subscription.email, status=status, error=error)
        except IntegrityError:
            logger.info(
                "Message %s was already claimed for %s; not sending again.",
                self.pk, subscription.email)
            return None

    def _finish(self) -> None:
        if self.status == self.Status.SENT:
            return
        self.status = self.Status.SENT
        self.sent_at = timezone.now()
        MailingListMessage.objects.filter(pk=self.pk).update(
            status=self.status, sent_at=self.sent_at)

    def _build_email(self, subscription: "MailingListSubscription",
                     request=None):
        body = self.render_for(subscription, request)
        email = EmailMessage(
            subject=self.subject, body=body,
            from_email=mailing_list_from_email(), to=[subscription.email])
        # RFC 8058/2369. Mail clients put a real unsubscribe button on the
        # message when this is present, which people use instead of the "this
        # is spam" button.
        email.extra_headers = {
            "List-Unsubscribe": f"<{subscription.unsubscribe_url(request)}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
        return email


class MailingListDelivery(models.Model):
    """One recipient's copy of one mailing. Written after the send returns."""

    class Status(models.TextChoices):
        SENT = 'S', 'sent'
        FAILED = 'F', 'failed'

    message = models.ForeignKey(
        MailingListMessage, on_delete=models.CASCADE,
        related_name="deliveries")
    subscription = models.ForeignKey(
        MailingListSubscription, on_delete=models.CASCADE,
        related_name="deliveries")
    # The address the copy went to, copied rather than followed through the
    # subscription: it is what the uniqueness below is enforced on, and it
    # stays true if the subscription is later moved or edited.
    email = models.EmailField(max_length=254)
    status = models.CharField(
        max_length=1, choices=Status.choices, default=Status.SENT)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # The guard against sending twice, enforced on the address rather
            # than only the subscription row: somebody in two of the groups a
            # mailing names has two rows, and two concurrent senders could
            # otherwise take one each and both mail them.
            models.UniqueConstraint(
                fields=["message", "email"],
                name="unique_delivery_per_address"),
            models.UniqueConstraint(
                fields=["message", "subscription"],
                name="unique_delivery_per_message"),
        ]

    def __str__(self) -> str:
        return f'{self.email}: {self.get_status_display()}'
