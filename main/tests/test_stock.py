"""Tests for manually managed Product stock."""

from io import StringIO
from unittest import mock

from django.contrib import admin
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import RequestFactory, TestCase

from main.models import CartProduct, Order, Product


OLD_CODE_PRODUCT_COLUMNS = [
    "description",
    "external_product_id",
    "isbn",
    "upc",
    "mpn",
    "kickstarter",
    "kindle_link",
    "amazon_link",
    "bookshop_link",
    "amazon_in_link",
    "flipkart_link",
    "preorder_only",
    "noorder",
    "backorder",
    "date_available",
    "brand",
    "sizes",
    "name",
    "page",
    "price",
    "image",
    "image_name",
    "tax_code",
    "cat",
    "mode",
]


class ProductStockTest(TestCase):
    def test_stock_defaults_to_zero_for_new_products(self):
        with mock.patch("main.models.Payments") as payments:
            payments.create_product.return_value = "prod_stock_default"
            product = Product.objects.create(name="Manual stock book")

        self.assertEqual(product.stock, 0)

    def test_raw_insert_from_old_code_uses_database_default(self):
        values = [
            "Inserted by old code",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            False,
            None,
            None,
            None,
            "Raw insert without stock",
            "",
            1234,
            "",
            "",
            Product.TaxTypes.GOODS,
            Product.Categories.MERCH,
            Product.Modes.PAYMENT,
        ]
        placeholders = ", ".join(["%s"] * len(OLD_CODE_PRODUCT_COLUMNS))
        sql = (
            f"INSERT INTO main_product ({', '.join(OLD_CODE_PRODUCT_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )

        with connection.cursor() as cursor:
            cursor.execute(sql, values)

        product = Product.objects.get(name="Raw insert without stock")
        self.assertEqual(product.stock, 0)

    def test_zero_stock_physical_product_is_not_purchasable(self):
        product = Product(
            name="Out of stock book",
            cat=Product.Categories.BOOKS,
            stock=0,
        )

        self.assertFalse(product.is_purchasable())
        self.assertEqual(product.get_availability(), "out_of_stock")
        self.assertEqual(product.stock_description(), "***Out of Stock***")
        self.assertEqual(product.buy_text(), "Out of Stock")

    def test_zero_stock_preorder_physical_book_is_purchasable(self):
        product = Product(
            name="Preorder book",
            cat=Product.Categories.BOOKS,
            preorder_only=True,
            stock=0,
        )

        self.assertFalse(product.is_out_of_stock())
        self.assertTrue(product.is_purchasable())
        self.assertEqual(product.get_availability(), "preorder")
        self.assertEqual(product.stock_description(), "***PreOrder Only***")
        self.assertEqual(product.buy_text(), "Pre-Order")

    def test_zero_stock_backorder_physical_book_is_purchasable(self):
        product = Product(
            name="Backorder book",
            cat=Product.Categories.BOOKS,
            backorder=True,
            stock=0,
        )

        self.assertFalse(product.is_out_of_stock())
        self.assertTrue(product.is_purchasable())
        self.assertEqual(product.get_availability(), "backorder")
        self.assertEqual(product.stock_description(), "***Back Order Only***")
        self.assertEqual(product.buy_text(), "Back Order")

    @mock.patch("main.models.Payments")
    def test_zero_stock_preorder_can_be_added_to_cart(self, payments):
        payments.create_product.return_value = "prod_preorder"
        payments.create_price.return_value = "price_preorder"
        product = Product.objects.create(
            name="Preorder book",
            cat=Product.Categories.BOOKS,
            preorder_only=True,
            stock=0,
            external_product_id="prod_preorder",
        )

        response = self.client.post(f"/add-to-cart/{product.pk}/1")

        self.assertRedirects(response, "/cart", fetch_redirect_response=False)
        self.assertEqual(CartProduct.objects.get().product, product)

    @mock.patch("main.models.Payments")
    def test_zero_stock_backorder_can_be_added_to_cart(self, payments):
        payments.create_product.return_value = "prod_backorder"
        payments.create_price.return_value = "price_backorder"
        product = Product.objects.create(
            name="Backorder book",
            cat=Product.Categories.BOOKS,
            backorder=True,
            stock=0,
            external_product_id="prod_backorder",
        )

        response = self.client.post(f"/add-to-cart/{product.pk}/1")

        self.assertRedirects(response, "/cart", fetch_redirect_response=False)
        self.assertEqual(CartProduct.objects.get().product, product)

    def test_zero_stock_non_book_physical_product_keeps_status_quo(self):
        product = Product(
            name="Sticker",
            cat=Product.Categories.MERCH,
            stock=0,
        )

        self.assertTrue(product.is_physical_good())
        self.assertFalse(product.is_out_of_stock())
        self.assertTrue(product.is_purchasable())
        self.assertEqual(product.get_availability(), "in_stock")
        self.assertEqual(product.stock_description(), "")
        self.assertEqual(product.buy_text(), "Add to Cart")

    def test_positive_stock_physical_product_is_purchasable(self):
        product = Product(
            name="Available book",
            cat=Product.Categories.BOOKS,
            stock=1,
        )

        self.assertTrue(product.is_purchasable())
        self.assertEqual(product.get_availability(), "in_stock")
        self.assertEqual(product.stock_description(), "")
        self.assertEqual(product.buy_text(), "Add to Cart")

    @mock.patch("main.models.Payments")
    def test_server_rejects_out_of_stock_physical_product(self, payments):
        payments.create_product.return_value = "prod_out"
        payments.create_price.return_value = "price_out"
        product = Product.objects.create(
            name="Out of stock book",
            cat=Product.Categories.BOOKS,
            stock=0,
            external_product_id="prod_out",
        )

        response = self.client.post(f"/add-to-cart/{product.pk}/1")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(CartProduct.objects.exists())

    @mock.patch("main.models.Payments")
    def test_service_with_zero_stock_can_still_be_added_to_cart(self, payments):
        payments.create_product.return_value = "prod_service"
        payments.create_price.return_value = "price_service"
        service = Product.objects.create(
            name="Consulting",
            cat=Product.Categories.SERVICES,
            tax_code=Product.TaxTypes.SERVICES,
            mode=Product.Modes.SUBSCRIPTION,
            stock=0,
            external_product_id="prod_service",
        )

        response = self.client.post(f"/add-to-cart/{service.pk}/1")

        self.assertRedirects(response, "/cart", fetch_redirect_response=False)
        self.assertEqual(CartProduct.objects.get().product, service)

    @mock.patch("main.views.Payments")
    @mock.patch("main.models.Payments")
    def test_checkout_rejects_a_cart_that_went_out_of_stock(
            self, payments, view_payments):
        # Stock is edited by hand in the admin and a cart can sit for days, so
        # what was purchasable at add-to-cart time may not be at checkout.
        payments.create_product.return_value = "prod_late"
        payments.create_price.return_value = "price_late"
        product = Product.objects.create(
            name="Sells out later",
            cat=Product.Categories.BOOKS,
            stock=5,
            external_product_id="prod_late",
        )
        self.client.post(f"/add-to-cart/{product.pk}/1")
        self.assertTrue(CartProduct.objects.exists())
        view_payments.pwyw_checkout_blocker.return_value = None

        Product.objects.filter(pk=product.pk).update(stock=0)
        response = self.client.post("/checkout")

        self.assertRedirects(response, "/cart", fetch_redirect_response=False)
        # No Stripe session, and no PENDING order left behind for it.
        view_payments.checkout.assert_not_called()
        self.assertFalse(Order.objects.exists())

    @mock.patch("main.views.Payments")
    @mock.patch("main.models.Payments")
    def test_checkout_still_works_while_the_stock_holds(
            self, payments, view_payments):
        payments.create_product.return_value = "prod_ok"
        payments.create_price.return_value = "price_ok"
        view_payments.pwyw_checkout_blocker.return_value = None
        view_payments.checkout.return_value = (
            "https://checkout.example/session", "cs_ok")
        product = Product.objects.create(
            name="In stock book",
            cat=Product.Categories.BOOKS,
            stock=5,
            external_product_id="prod_ok",
        )
        self.client.post(f"/add-to-cart/{product.pk}/1")

        response = self.client.post("/checkout")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], "https://checkout.example/session")

    def test_seed_rejects_fixture_owned_stock(self):
        product = Product.objects.create(
            pk=100,
            name="Old stock-managed book",
            price=1,
            stock=7,
            external_product_id="prod_live",
            cat=Product.Categories.BOOKS,
        )

        fixture = [
            {
                "model": "main.product",
                "pk": 100,
                "fields": {
                    "name": "Learning Spark (1st edition)",
                    "price": 3999,
                    "stock": 0,
                    "cat": Product.Categories.BOOKS,
                },
            },
        ]
        with mock.patch(
            "main.management.commands.seed_products._load_fixture",
            return_value=fixture,
        ):
            with self.assertRaisesMessage(CommandError, "stock"):
                call_command("seed_products", stdout=StringIO())

        product.refresh_from_db()
        self.assertEqual(product.stock, 7)
        self.assertEqual(product.price, 1)

    def test_product_admin_form_includes_editable_stock(self):
        product_admin = admin.site._registry[Product]
        request = RequestFactory().get("/admin/main/product/100/change/")

        form = product_admin.get_form(request)

        self.assertIn("stock", form.base_fields)
        self.assertFalse(form.base_fields["stock"].disabled)
        self.assertIn(
            "does not cap order quantity",
            form.base_fields["stock"].help_text,
        )


class DigitalStockExemptionTest(TestCase):
    """A DIGITAL product must never be gated by a stock count.

    Nothing in is_out_of_stock() names delivery_type. The exemption falls out
    of is_physical_good() reading delivery_type directly, so a download is
    already non-physical and short-circuits the rest of the predicate. That is
    the correct design -- one place decides what "physical" means -- but it
    also means the exemption is invisible at the point it matters, and any
    future change that makes is_physical_good() infer physicality from
    category or mode again would silently make the pay-what-you-want e-book
    unbuyable at its fixture default of stock=0. Nobody would get an error;
    the buy button would just turn into "Out of Stock" for a file that cannot
    run out.

    So these pin the behaviour rather than the implementation: they assert on
    purchasability, not on which predicate produced it.
    """

    fixtures = ["initial_products"]

    EBOOK_PK = 106

    def test_the_zero_stock_pwyw_ebook_is_purchasable(self):
        # The fixture ships no stock value at all, so this is the state the
        # e-book is actually in on a fresh deploy, not a contrived one.
        ebook = Product.objects.get(pk=self.EBOOK_PK)

        self.assertEqual(ebook.stock, 0)
        self.assertEqual(ebook.delivery_type, Product.DeliveryTypes.DIGITAL)
        self.assertTrue(ebook.is_pwyw)

        self.assertFalse(ebook.is_out_of_stock())
        self.assertTrue(ebook.is_purchasable())
        self.assertEqual(ebook.get_availability(), "in_stock")
        self.assertEqual(ebook.buy_text(), "Add to Cart")
        self.assertEqual(ebook.stock_description(), "")

    def test_a_digital_book_is_exempt_regardless_of_category(self):
        # Same category and mode as a physical book, so the only thing keeping
        # it purchasable is delivery_type.
        product = Product(
            name="Some download",
            cat=Product.Categories.BOOKS,
            mode=Product.Modes.PAYMENT,
            delivery_type=Product.DeliveryTypes.DIGITAL,
            stock=0,
        )

        self.assertFalse(product.is_out_of_stock())
        self.assertTrue(product.is_purchasable())

    def test_the_zero_stock_ebook_can_still_be_added_to_cart(self):
        # is_purchasable() is also the server-side gate in AddToCartView, so
        # the exemption has to hold end to end, not just on the model.
        with mock.patch("main.models.Payments") as payments:
            payments.create_product.return_value = "prod_ebook"
            payments.create_price.return_value = "price_ebook"
            response = self.client.post(f"/add-to-cart/{self.EBOOK_PK}/1")

        self.assertRedirects(response, "/cart")
        self.assertEqual(CartProduct.objects.get().product_id, self.EBOOK_PK)

    def test_the_zero_stock_ebook_page_offers_a_live_buy_button(self):
        response = self.client.get(f"/product/{self.EBOOK_PK}")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add to Cart")
        self.assertNotContains(response, "Out of Stock")
        self.assertNotContains(response, "disabled")

    def test_the_physical_dc4k_editions_are_still_gated(self):
        # The other half of the contract: the exemption is scoped to
        # downloads, and must not have leaked to the print SKUs just because
        # they belong to the same title.
        for pk in (104, 105):
            with self.subTest(pk=pk):
                product = Product.objects.get(pk=pk)
                self.assertEqual(product.stock, 0)
                self.assertEqual(
                    product.delivery_type, Product.DeliveryTypes.PHYSICAL)
                self.assertTrue(product.is_out_of_stock())
                self.assertFalse(product.is_purchasable())
                self.assertEqual(product.buy_text(), "Out of Stock")
