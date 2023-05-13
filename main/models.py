from django.contrib.auth.models import User
from django.db import models
from django.templatetags.static import static
from django.urls import reverse

from main.payments import Payments
from typing import Optional

from easy_thumbnails.files import get_thumbnailer


# Create your models here.
class Product(models.Model):
    name = models.CharField(max_length=250, primary_key=False)
    description = models.TextField(default="No description.")
    image = models.ImageField(upload_to='product-images')
    external_product_id = models.CharField(max_length=250, blank=True, null=True)
    product_id = models.AutoField(primary_key=True)
    isbn = models.CharField(max_length=20, blank=True, null=True)
    upc = models.CharField(max_length=20, blank=True, null=True)
    mpn = models.CharField(max_length=100, blank=True, null=True)
    kickstarter = models.CharField(max_length=200, blank=True, null=True)
    kindle_link = models.CharField(max_length=200, blank=True, null=True)
    preorder_only = models.BooleanField(default=False, null=False)
    noorder = models.BooleanField(default=False, null=False)
    backorder = models.BooleanField(default=False, null=False)
    date_available = models.DateField(null=True, blank=True)
    brand = models.CharField(null=True, blank=True, max_length=200)
    sizes = models.CharField(null=True, blank=True, max_length=200)

    def generate_external_product_id(self):
        external_product_id = Payments.create_product(
            self.name, self.description, self.price, currency="usd")
        return external_product_id

    def save(self, *args, **kwargs):
        if not self.external_product_id or True:
            self.external_product_id = self.generate_external_product_id()
        super().save(*args, **kwargs)

    class Modes(models.TextChoices):
        PAYMENT = 'P', 'payment'
        SUBSCRIPTION = 'S', 'subscription'

    class TaxTypes(models.TextChoices):
        # See https://stripe.com/docs/tax/tax-categories
        GOODS = 'txcd_99999999', 'Goods'
        SERVICES = 'txcd_20030000', 'Services'
        HOSTING = 'txcd_10701100', 'Hosting'
        PHONES = 'txcd_34021000', 'Phones'
        BOOKS = 'txcd_35010000', 'Books'  # Physical books
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

    def get_display_price(self) -> str:
        formatted_price = "{0:.2f}".format(self.price / 100)
        if self.preorder_only:
            return f"Pre-order: {formatted_price}"
        else:
            return formatted_price
    def get_absolute_url(self) -> str:
        return reverse('product', kwargs={'product_id': self.product_id})

    def get_image_url(self) -> Optional[str]:
        try:
            return self.image.url
        except:
            if self.image_name is not None:
                return static(f"assets/images/{self.image_name}")
            else:
                return None

    def get_thumb(self):
        t = None
        print("hi")
        try:
            if self.image_name is not None:
                from static_thumbnails.templatetags.static_thumbnails import static_storage
                t = get_thumbnailer(
                    static_storage,
                    relative_name=f"assets/images/{self.image_name}")
                print("k?")
            else:
                t = get_thumbnailer(self.image)
        except Exception as e:
            print(f"Got exception {e} trying to load thumbnailer.")
            return self.get_image_url()
        print(f"Getting thumbailer {t}")
        try:
            th = t.get_thumbnail({'size': (290, 380)})
            print(f"Got thumbailer {t} with thumb {th}")
            return th.url
        except Exception as e:
            print(f"Error generating thumbnail?: {e}")
            return self.get_image_url()

    def __str__(self) -> str:
        return f'{self.name}'

    def __repr__(self) -> str:
        return f'<Product: {self.name}>'

    def get_alt_links(self):
        links = []
        if self.isbn is not None and self.isbn != "":
            links.append((
                "Read on O'Reilly Safari (free trial)",
                "https://www.tkqlhce.com/click-7645222-14045081"))
        if self.kindle_link is not None and self.kindle_link != "":
            links.append((
                "Buy on Kindle (e-book)",
                self.kindle_link))
        if self.kickstarter is not None and self.kickstarter != "":
            links.append((
                "Follow along on kick starter",
                self.kickstarter))
        return links

    def get_display_text(self):
        if self.isbn is not None:
            return f"{self.description}<p>All of Holden's books are avaible signed on request</p>"
        else:
            return self.description

    def get_gtin(self):
        if self.isbn is not None:
            return self.isbn
        else:
            return self.upc

    def get_availability(self):
        if self.preorder_only:
            return "preorder"
        elif self.backorder:
            return "backorder"
        else:
            return "in_stock"

    def buy_text(self):
        if self.preorder_only:
            return "Pre-Order"
        elif self.backorder:
            return "Back Order"
        else:
            return "Add to Cart"

    def stock_description(self):
        if self.backorder:
            return "***Back Order Only***"
        elif self.preorder_only:
            return "***PreOrder Only***"
        else:
            return ""

    def get_brand(self):
        if self.brand is not None:
            return self.brand
        elif self.cat == Product.Categories.BOOKS:
            return "O'Reilly"
        else:
            return "Pigs Can Fly Labs"

    def get_sizes(self):
        if self.sizes is not None:
            return self.sizes.split(",")
        else:
            return [None]

    def get_mpn(self):
        if self.mpn is not None:
            return self.mpn
        else:
            return f"PCF{self.pk}"

class Cart(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        )
    cart_id = models.AutoField(primary_key=True)
    products = models.ManyToManyField(
        'CartProduct', related_name='cart_products')

    def clear(self):
        self.products.remove(*self.products.all())

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

    def generate_price_id(self):
        if self.product.mode == Product.Modes.PAYMENT:
            price_id = Payments.create_price(
                self.product.external_product_id, self.product.price, currency="usd")
        else:
            price_id = Payments.create_price(
                self.product.external_product_id, self.product.price, currency="usd",
                interval="year"
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
