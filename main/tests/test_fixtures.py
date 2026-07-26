"""Tests for the shipped product fixtures."""

from unittest import mock

from django.test import TestCase

from main.models import Product
from main.tests.base import SHIPPING_NOTICE_TEXT


def isbn13_check_digit(isbn: str) -> str:
    """The check digit ISBN-13 requires for the first twelve digits.

    Weights alternate 1, 3 from the left; the check digit is whatever brings
    the weighted sum up to a multiple of ten.
    """
    total = sum(int(digit) * (3 if position % 2 else 1)
                for position, digit in enumerate(isbn[:12]))
    return str((10 - total % 10) % 10)


AMAZON_IN_LABEL = "Buy on Amazon.in (print)"
FLIPKART_LABEL = "Buy on Flipkart (print)"
BOOKSHOP_LABEL = "Buy on Bookshop.org (support local bookstores)"


class BookIsbnTest(TestCase):
    """Every shipped ISBN has to be a real one.

    get_gtin() hands `isbn` straight to the Google Merchant feed as the
    product's GTIN, and Google disapproves a product submitted with an
    incorrect one. A single mistyped digit is therefore a silent feed
    failure: nothing here breaks, the listing just stops being accepted.
    Checking the check digit is the cheapest possible defence, and it covers
    every book added later rather than only the ones in the fixture today.
    """

    fixtures = ["initial_products"]

    def books_with_isbns(self):
        return [book for book
                in Product.objects.filter(cat=Product.Categories.BOOKS)
                if book.isbn]

    def test_every_book_isbn_is_a_valid_isbn13(self):
        books = self.books_with_isbns()
        # Guards against the whole test passing because the queryset is empty.
        self.assertGreaterEqual(len(books), 6)
        for book in books:
            with self.subTest(pk=book.pk, isbn=book.isbn):
                isbn = str(book.isbn)
                self.assertRegex(isbn, r"^\d{13}$")
                self.assertEqual(isbn[-1], isbn13_check_digit(isbn))

    def test_the_check_digit_rule_rejects_a_typo(self):
        # Without this, a bug in isbn13_check_digit() that made it agree with
        # anything would leave the test above passing and useless. Every real
        # ISBN in the fixture, with its last digit bumped, must be rejected.
        for book in self.books_with_isbns():
            isbn = str(book.isbn)
            typo = isbn[:12] + str((int(isbn[-1]) + 1) % 10)
            with self.subTest(pk=book.pk, typo=typo):
                self.assertNotEqual(typo, isbn)
                self.assertNotEqual(typo[-1], isbn13_check_digit(typo))

    def test_no_two_products_share_a_gtin(self):
        # Two SKUs submitted under one GTIN are a duplicate in the feed. The
        # Executive Edition exists precisely to carry a different number from
        # the standard edition, so a copy-paste here is a real hazard.
        gtins = [product.get_gtin() for product in Product.objects.all()
                 if product.get_gtin()]

        self.assertTrue(gtins)
        self.assertEqual(len(gtins), len(set(gtins)))


class InitialProductsFixtureTest(TestCase):
    fixtures = ["initial_products"]

    def test_fixture_loads_the_four_books_as_books(self):
        books = Product.objects.filter(pk__in=[100, 101, 102, 103])
        self.assertEqual(books.count(), 4)
        for book in books:
            self.assertEqual(book.cat, Product.Categories.BOOKS)
            self.assertEqual(book.tax_code, Product.TaxTypes.BOOKS)
            self.assertTrue(book.isbn)

    def test_fixture_products_have_no_stripe_id_until_first_use(self):
        book = Product.objects.get(pk=100)
        self.assertFalse(book.external_product_id)

    def test_book_alt_links_lead_with_amazon(self):
        book = Product.objects.get(pk=100)
        links = book.get_alt_links()
        self.assertTrue(links)
        name, url = links[0]
        self.assertIn("Amazon", name)
        self.assertEqual(url, "https://www.amazon.com/dp/1449358624")

    def test_book_page_shows_amazon_link_and_shipping_notice(self):
        response = self.client.get("/product/100")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Buy on Amazon")
        self.assertContains(response, "https://www.amazon.com/dp/1449358624")
        self.assertContains(response, SHIPPING_NOTICE_TEXT)

    @mock.patch("main.models.Payments")
    def test_fixture_book_is_out_of_stock_as_shipped(self, payments):
        payments.create_product.return_value = "prod_test"
        payments.create_price.return_value = "price_test"

        response = self.client.get("/product/100")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "***Out of Stock***")

        response = self.client.post("/add-to-cart/100/1")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, b"Product is not purchasable.")

    def test_google_product_feed_includes_books_and_long_handling_times(self):
        response = self.client.get("/google_products.xml")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<g:gtin>9781449358624</g:gtin>")
        self.assertContains(response, "<g:max_handling_time>21</g:max_handling_time>")

    def test_google_product_feed_omits_availability_date_for_out_of_stock_book(self):
        Product.objects.filter(pk=100).update(date_available="2030-01-15")

        response = self.client.get("/google_products.xml")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<g:availability>out_of_stock</g:availability>")
        self.assertNotContains(response, "<g:availability_date>")

    @mock.patch("main.models.Payments")
    def test_cart_with_physical_book_shows_shipping_notice(self, payments):
        payments.create_product.return_value = "prod_test"
        payments.create_price.return_value = "price_test"
        Product.objects.filter(pk=100).update(stock=1)
        self.client.post("/add-to-cart/100/1")
        response = self.client.get("/cart")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, SHIPPING_NOTICE_TEXT)

    def test_alt_links_without_country_skip_india_stores(self):
        book = Product.objects.get(pk=101)
        names = [name for name, _ in book.get_alt_links()]
        self.assertNotIn(AMAZON_IN_LABEL, names)
        self.assertNotIn(FLIPKART_LABEL, names)
        self.assertIn(BOOKSHOP_LABEL, names)

    def test_alt_links_for_india_lead_with_indian_stores(self):
        book = Product.objects.get(pk=101)
        names = [name for name, _ in book.get_alt_links(country="IN")]
        self.assertEqual(names[0], AMAZON_IN_LABEL)
        self.assertEqual(names[1], FLIPKART_LABEL)
        self.assertIn("Buy on Amazon (print)", names)

    @mock.patch("main.views.get_country_code", return_value="IN")
    def test_book_page_shows_indian_links_for_indian_visitors(self, _geo):
        response = self.client.get("/product/101")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://www.amazon.in/dp/1491943203")
        self.assertContains(response, FLIPKART_LABEL)

    def test_book_page_hides_indian_links_by_default(self):
        # No GeoLite2 database in the test environment, so country is None.
        response = self.client.get("/product/101")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "amazon.in")
        self.assertContains(response, BOOKSHOP_LABEL)
