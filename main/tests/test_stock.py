"""Tests for manually managed Product stock."""

from io import StringIO
from unittest import mock

from django.contrib import admin
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory, TestCase

from main.models import CartProduct, Product


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
            product_id = cursor.lastrowid

        self.assertEqual(Product.objects.get(pk=product_id).stock, 0)

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

    def test_seed_preserves_admin_managed_stock(self):
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
            call_command("seed_products", stdout=StringIO())

        product.refresh_from_db()
        self.assertEqual(product.stock, 7)
        self.assertEqual(product.price, 3999)

    def test_product_admin_form_includes_editable_stock(self):
        product_admin = admin.site._registry[Product]
        request = RequestFactory().get("/admin/main/product/100/change/")

        form = product_admin.get_form(request)

        self.assertIn("stock", form.base_fields)
        self.assertFalse(form.base_fields["stock"].disabled)
