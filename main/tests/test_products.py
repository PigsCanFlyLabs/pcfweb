"""Tests for the Product model and its Stripe bookkeeping."""

from unittest import mock

from django.test import TestCase

from main.models import Product
from main.payments import Payments
from main.tests.base import SHIPPING_NOTICE_TEXT


class ServiceProductTest(TestCase):
    def setUp(self):
        self.service = Product.objects.create(
            name="Distributed systems consulting",
            description="Consulting services.",
            external_product_id="prod_preexisting",
            price=100000,
            cat=Product.Categories.SERVICES,
            tax_code=Product.TaxTypes.SERVICES,
            mode=Product.Modes.SUBSCRIPTION,
            # Now stated rather than inferred from the category. Migration
            # 0009 backfills the existing service rows the same way.
            delivery_type=Product.DeliveryTypes.SERVICE,
        )

    def test_service_is_not_a_physical_good(self):
        self.assertFalse(self.service.is_physical_good())

    def test_service_page_hides_shipping_notice(self):
        response = self.client.get(f"/product/{self.service.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SHIPPING_NOTICE_TEXT)


class ProductCreationTaxCodeTest(TestCase):
    @mock.patch("main.payments.stripe.Product.create")
    def test_create_product_sends_tax_code_when_set(self, create_product):
        create_product.return_value = {"id": "prod_taxed"}

        product_id = Payments.create_product(
            "Book",
            "A physical book.",
            2500,
            tax_code=Product.TaxTypes.BOOKS,
        )

        self.assertEqual(product_id, "prod_taxed")
        create_product.assert_called_once_with(
            name="Book",
            description="A physical book.",
            tax_code=Product.TaxTypes.BOOKS,
        )

    @mock.patch("main.payments.stripe.Product.create")
    def test_create_product_strips_tax_code_when_set(self, create_product):
        create_product.return_value = {"id": "prod_taxed"}

        Payments.create_product(
            "Book",
            "A physical book.",
            2500,
            tax_code=f"  {Product.TaxTypes.BOOKS}  ",
        )

        create_product.assert_called_once_with(
            name="Book",
            description="A physical book.",
            tax_code=Product.TaxTypes.BOOKS,
        )

    @mock.patch("main.payments.stripe.Product.create")
    def test_create_product_omits_blank_tax_code(self, create_product):
        create_product.return_value = {"id": "prod_default_tax"}

        Payments.create_product("Sticker", "A sticker.", 500, tax_code="   ")

        create_product.assert_called_once_with(
            name="Sticker",
            description="A sticker.",
        )


class ProductSaveStripeTest(TestCase):
    @mock.patch("main.models.Payments")
    def test_save_skips_stripe_when_external_id_present(self, payments):
        Product.objects.create(
            name="Already synced",
            external_product_id="prod_existing",
            price=1000,
        )
        payments.create_product.assert_not_called()

    @mock.patch("main.models.Payments")
    def test_save_generates_stripe_id_when_missing(self, payments):
        payments.create_product.return_value = "prod_new"
        product = Product.objects.create(name="Fresh product", price=1000)
        payments.create_product.assert_called_once_with(
            "Fresh product",
            "No description.",
            1000,
            currency="usd",
            tax_code=Product.TaxTypes.GOODS,
        )
        self.assertEqual(product.external_product_id, "prod_new")


class ProductUrlTest(TestCase):
    fixtures = ["initial_products"]

    def test_get_absolute_url_reverses_to_the_product_page(self):
        product = Product.objects.get(pk=100)
        self.assertEqual(product.get_absolute_url(), "/product/100")
        response = self.client.get(product.get_absolute_url())
        self.assertEqual(response.status_code, 200)
