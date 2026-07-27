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
