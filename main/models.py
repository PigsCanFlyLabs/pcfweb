import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import models, transaction
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
    isbn = models.CharField(max_length=20, blank=True, null=True)
    upc = models.CharField(max_length=20, blank=True, null=True)
    mpn = models.CharField(max_length=100, blank=True, null=True)
    kickstarter = models.CharField(max_length=200, blank=True, null=True)
    kindle_link = models.CharField(max_length=200, blank=True, null=True)
    amazon_link = models.CharField(max_length=200, blank=True, null=True)
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
    delivery_type = models.CharField(
        max_length=20,
        choices=DeliveryTypes.choices,
        default=DeliveryTypes.PHYSICAL)

    # "We are licensed to distribute this file ourselves." Not a feature flag:
    # it is the interlock that stops a mis-set delivery_type dropdown from
    # emailing a book somebody else holds the distribution rights to. The
    # O'Reilly titles are Holden's writing but O'Reilly's to hand out, so this
    # defaults to False and they stay False.
    sells_ebook = models.BooleanField(default=False)

    # Pay-what-you-want. Turns Product.price into a *suggestion*: the Stripe
    # Price is minted with custom_unit_amount instead of a fixed unit_amount,
    # and the buyer types their own number (including zero).
    is_pwyw = models.BooleanField(default=False)

    # Filename stem of the downloadable archive, without directory or
    # extension: the file served is <digital_asset_name>.zip under
    # settings.BOOK_ASSET_ROOT. An explicit field rather than something
    # derived from `name`, so renaming a book in the admin cannot silently
    # break fulfilment. Admin-editable, therefore never trusted -- see
    # main.digital.resolve_asset_path.
    digital_asset_name = models.CharField(max_length=100, blank=True)

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

    def get_alt_links(self, country: Optional[str] = None):
        candidates = []
        if country == "IN":
            candidates += [
                ("Buy on Amazon.in (print)", self.amazon_in_link),
                ("Buy on Flipkart (print)", self.flipkart_link),
            ]
        candidates += [
            ("Buy on Amazon (print)", self.amazon_link),
            ("Buy on Bookshop.org (support local bookstores)",
             self.bookshop_link),
            ("Read on O'Reilly Safari (free trial)",
             "https://www.tkqlhce.com/click-7645222-14045081"
             if self.isbn else None),
            ("Buy on Kindle (e-book)", self.kindle_link),
            ("Follow along on Kickstarter", self.kickstarter),
        ]
        return [(label, url) for label, url in candidates if url]

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
        # Stock is intentionally scoped to physical books for now. When the
        # DC4K delivery_type field lands, DIGITAL book products must be exempt
        # here so a stock value of 0 cannot block emailed ebook fulfilment.
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
        if self.isbn:
            return format_html(
                "{}<p>{}</p>", self.description, self.SIGNED_ON_REQUEST_NOTE)
        return format_html("{}", self.description)

    def get_feed_description(self) -> str:
        """The same copy as plain text, for the Google product feed.

        The feed is XML, so markup from get_display_text() would arrive at
        Google as escaped angle brackets and show up literally in the listing.
        """
        if self.isbn:
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
        return self.isbn or self.upc

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
    digital_delivery_sent_at = models.DateTimeField(null=True, blank=True)
    digital_delivery_error = models.TextField(blank=True)

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
        """Lines this site is licensed to deliver itself.

        Reads the interlock live off the Product rather than from the
        snapshot: revoked distribution rights must stop delivery immediately,
        including on a webhook re-delivery for an old order.
        """
        return [item for item in self.items.select_related('product')
                if item.product is not None
                and item.product.is_digitally_fulfilled()]

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

    def _deliver_digital_goods(self) -> bool:
        items = self.digital_items()
        if not items:
            # Nothing downloadable on this order, which is the common case.
            return False
        if not self.customer_email:
            self._record_digital_delivery_failure(
                "Stripe reported no customer email for this order, so the "
                "download link could not be sent. Get an address from the "
                "Stripe Dashboard and resend it by hand.")
            return False

        links: List[Tuple[str, str]] = []
        problems: List[str] = []
        for item in items:
            product = item.product
            assert product is not None  # digital_items() guarantees it
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
