from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from main.models import Product


class EbookIdentifierFixtureTest(TestCase):
    fixtures = ["initial_products"]

    EXPECTED_EBOOK_ISBNS = {
        100: "9781449359058",
        101: "9781491943151",
        102: "9781492050070",
        103: "9781098118761",
    }

    def test_the_verified_oreilly_rows_keep_their_pinned_retail_epub_isbns(self):
        for pk, ebook_isbn in self.EXPECTED_EBOOK_ISBNS.items():
            with self.subTest(pk=pk):
                self.assertEqual(Product.objects.get(pk=pk).ebook_isbn, ebook_isbn)

    def test_the_deliberate_non_ebook_rows_stay_blank(self):
        for pk in (104, 105, 107):
            with self.subTest(pk=pk):
                self.assertFalse(Product.objects.get(pk=pk).ebook_isbn)


class SeededKindleAsinTest(TestCase):
    """The Kindle ASINs must arrive from the fixture on a clean deploy.

    Exercises the real deploy path -- ``manage.py seed_products``, as
    scripts/start-server.sh invokes it -- against an empty database, rather
    than loaddata, because the point of the change is that seeding no longer
    rejects the fixture and no longer needs a human to type these into the
    admin.
    """

    # Verified against the live Amazon listings.
    EXPECTED_EBOOK_ASINS = {
        100: "B00SW0TY8O",
        102: "B08L5Q9W59",
        103: "B0BNM6PQ9Q",
    }

    def setUp(self):
        patcher = mock.patch("main.models.Payments")
        patcher.start()
        self.addCleanup(patcher.stop)
        call_command("seed_products", stdout=StringIO())

    def test_seed_populates_the_verified_kindle_asins(self):
        for pk, asin in self.EXPECTED_EBOOK_ASINS.items():
            with self.subTest(pk=pk):
                self.assertEqual(Product.objects.get(pk=pk).ebook_asin, asin)

    def test_seeded_asins_produce_kindle_links(self):
        for pk, asin in self.EXPECTED_EBOOK_ASINS.items():
            with self.subTest(pk=pk):
                self.assertEqual(
                    Product.objects.get(pk=pk).get_kindle_link(),
                    f"https://www.amazon.com/dp/{asin}",
                )

    def test_seeded_asins_reach_the_product_page(self):
        for pk, asin in self.EXPECTED_EBOOK_ASINS.items():
            with self.subTest(pk=pk):
                response = self.client.get(f"/product/{pk}")
                self.assertEqual(response.status_code, 200)
                body = response.content.decode()
                self.assertIn(f"https://www.amazon.com/dp/{asin}", body)
                self.assertIn(Product.AMAZON_EBOOK_LABEL, body)

    def test_high_performance_spark_kindle_link_is_blank_by_design(self):
        """pk 101 has no Amazon e-book link, and that is the intended state.

        Amazon delisted the 1st edition's Kindle edition (B0725YT69J 404s),
        and the 2nd edition's B0H3CMNN3Q is a different book, so the fixture
        deliberately omits ebook_asin. The page must therefore carry no
        Amazon e-book link at all -- not an empty href, which get_alt_links()
        prevents by filtering out falsy URLs.
        """
        product = Product.objects.get(pk=101)
        self.assertFalse(product.ebook_asin)
        self.assertIsNone(product.get_kindle_link())

        labels = [label for label, _ in product.get_alt_links()]
        self.assertNotIn(Product.AMAZON_EBOOK_LABEL, labels)
        # Anti-vacuity: the row did render its other retailer buttons, so the
        # assertion above is about this link and not about an empty list.
        self.assertIn("Buy on Amazon (print)", labels)

        response = self.client.get("/product/101")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn(Product.AMAZON_EBOOK_LABEL, body)
        # The wrong-book ASIN must never appear on this page.
        self.assertNotIn("B0H3CMNN3Q", body)
        self.assertNotIn('href=""', body)

    def test_rows_with_no_kindle_edition_stay_blank(self):
        """DC4K (104-106) has no Kindle edition; 107 has no verified ASIN."""
        for pk in (104, 105, 106, 107):
            with self.subTest(pk=pk):
                self.assertFalse(Product.objects.get(pk=pk).ebook_asin)
