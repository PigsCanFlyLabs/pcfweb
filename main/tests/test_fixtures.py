"""Tests for the shipped product fixtures."""

from unittest import mock

from django.test import TestCase, override_settings

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
BOOKSHOP_EBOOK_LABEL = "Buy the e-book on Bookshop.org (DRM-free)"
# Only these two of the eight catalogue rows have a Bookshop e-book listing;
# coverage is publisher-gated, not something derivable from an ISBN.
BOOKSHOP_EBOOK_URLS = {
    102: (
        "https://bookshop.org/p/books/kubeflow-for-machine-learning-"
        "boris-lublinsky/6feb89c16760d5f7?ean=9781492050070"
    ),
    103: (
        "https://bookshop.org/p/books/scaling-python-with-ray-adventures-"
        "in-cloud-and-serverless-patterns-boris-lublinsky/"
        "4dc16509c22353e3?ean=9781098118761"
    ),
}
BOOKSHOP_EBOOK_ABSENT_PKS = [100, 101, 104, 105, 106, 107]


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

    def test_every_book_with_an_isbn_also_has_a_print_isbn(self):
        """`isbn` set but `print_isbn` NULL is a silently broken row.

        The isbn -> print_isbn backfill is a one-shot data migration: it ran
        once, over the rows that existed at migrate time. Any row added to the
        fixture afterwards -- as the DC4K SKUs were, on a branch that predated
        the identifier work -- gets no backfill, so setting only the legacy
        `isbn` leaves print_isbn NULL.

        Nothing raises when that happens. get_gtin() is
        `print_isbn or ebook_isbn or upc`, so the row just drops out of the
        Google Merchant feed's <g:gtin>, and get_display_text() quietly stops
        offering "available signed on request". Both failures are invisible
        from inside the app, which is why this is asserted on the fixture
        rather than left to be noticed in production.
        """
        books = self.books_with_isbns()
        # Guards against the whole test passing because the queryset is empty.
        self.assertGreaterEqual(len(books), 6)
        for book in books:
            with self.subTest(pk=book.pk, isbn=book.isbn):
                self.assertTrue(
                    book.print_isbn,
                    f"pk {book.pk} sets isbn={book.isbn!r} but leaves "
                    f"print_isbn={book.print_isbn!r}; it would lose its "
                    "<g:gtin> and its signed-copies note.")
                # The legacy column and the print column must agree, not merely
                # both be non-empty -- a mismatched pair feeds one number to
                # the page and a different one to Google.
                self.assertEqual(book.print_isbn, book.isbn)

    def test_no_two_products_share_a_gtin(self):
        # Two SKUs submitted under one GTIN are a duplicate in the feed. The
        # Executive Edition exists precisely to carry a different number from
        # the standard edition, so a copy-paste here is a real hazard.
        gtins = [product.get_gtin() for product in Product.objects.all()
                 if product.get_gtin()]

        self.assertTrue(gtins)
        self.assertEqual(len(gtins), len(set(gtins)))


@override_settings(THUMBNAIL_DEBUG=False)
class NotForSaleBookTest(TestCase):
    """pk 107, Fast Data Processing with Spark: listed, never buyable.

    It is in the catalogue for the publishing history -- it is the first book
    written about Apache Spark, which the /services credentials cite -- but it
    is a 2013 Packt title we do not sell. So it must have a reachable page and
    must appear nowhere that implies it is on sale.
    """

    fixtures = ["initial_products"]
    PK = 107

    def book(self):
        return Product.objects.get(pk=self.PK)

    def test_the_book_is_in_the_catalogue_but_not_purchasable(self):
        book = self.book()

        self.assertTrue(book.noorder)
        self.assertFalse(book.is_purchasable())

    def test_its_page_is_reachable_by_direct_link(self):
        response = self.client.get(f"/product/{self.PK}")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fast Data Processing with Spark")

    def test_it_is_absent_from_the_products_listing(self):
        html = self.client.get("/products").content.decode()

        self.assertNotIn("Fast Data Processing with Spark", html)
        # Anti-vacuity: the listing is really rendering books.
        self.assertIn("Learning Spark", html)

    def test_it_is_absent_from_the_homepage(self):
        html = self.client.get("/").content.decode()

        self.assertNotIn("Fast Data Processing with Spark", html)

    def test_it_is_absent_from_the_google_merchant_feed(self):
        body = self.client.get("/google_products.xml").content.decode()

        self.assertNotIn("Fast Data Processing with Spark", body)
        self.assertNotIn("<g:id>107</g:id>", body)
        # Anti-vacuity: the feed really does carry the sellable books.
        self.assertIn("<g:id>100</g:id>", body)

    def test_it_is_attributed_to_packt_not_oreilly(self):
        """get_brand() defaults the Books category to O'Reilly.

        This is a Packt title, and this is the page that exists to record who
        published what, so inheriting the default would be plainly wrong.
        """
        self.assertEqual(self.book().get_brand(), "Packt")

    def test_it_does_not_advertise_an_oreilly_safari_trial(self):
        labels = [label for label, _ in self.book().get_alt_links()]

        self.assertNotIn("Read on O'Reilly Safari (free trial)", labels)
        # It does still offer the place you can actually get it.
        self.assertIn("Buy on Amazon (print)", labels)

    def test_its_page_shows_no_price_at_all(self):
        """A bare "0.00" above a page with no buy button reads as broken.

        price is 0 because there is no price to charge, not because the book
        is free, so no number is the honest rendering.
        """
        html = self.client.get(f"/product/{self.PK}").content.decode()

        self.assertNotIn('<span class="price">0.00</span>', html)
        self.assertIn('<span class="price">Not sold here</span>', html)

    def test_its_page_surfaces_the_amazon_link_where_the_price_was(self):
        response = self.client.get(f"/product/{self.PK}")

        self.assertContains(response, "Available from Amazon")
        self.assertContains(
            response,
            "https://www.amazon.com/Fast-Processing-Spark-Holden-Karau/"
            "dp/1782167064")

    def test_its_page_offers_nothing_that_implies_an_order(self):
        """Everything order-shaped goes together, or the page still reads wrong.

        "Out of Stock" means temporarily unavailable and would be a different
        lie; a quantity picker and a running "Total: $0.00" are worse.
        """
        html = self.client.get(f"/product/{self.PK}").content.decode()

        self.assertNotIn("Out of Stock", html)
        self.assertNotIn("No. of Orders", html)
        self.assertNotIn('h4 class="calc-total"', html)
        self.assertNotIn('id="quantity"', html)
        # Not shipped by us either, so the shipping delay notice is noise.
        self.assertNotIn("shipping-notice", html)

    def test_a_sellable_book_still_shows_all_of_that(self):
        """Control: the suppression is scoped to noorder, not global."""
        html = self.client.get("/product/100").content.decode()

        self.assertIn('<span class="price">39.99</span>', html)
        self.assertIn("No. of Orders", html)
        self.assertIn('h4 class="calc-total"', html)
        self.assertIn('id="quantity"', html)
        self.assertNotIn("Not sold here", html)

    def test_it_carries_no_asin_so_the_seed_command_still_runs(self):
        """ASINs are in seed_products' SEED_PROTECTED_FIELDS.

        A fixture row carrying one makes `seed_products` exit 1, which under
        `set -e` in scripts/start-server.sh stops the primary pod from
        booting. The explicit amazon_link below is what get_amazon_link()
        prefers anyway, so the ASIN would never have been read.
        """
        book = self.book()

        self.assertFalse(book.print_asin)
        self.assertFalse(book.default_asin)
        self.assertEqual(
            book.get_amazon_link(),
            "https://www.amazon.com/Fast-Processing-Spark-Holden-Karau/"
            "dp/1782167064")


class InitialProductsFixtureTest(TestCase):
    fixtures = ["initial_products"]

    def test_fixture_loads_the_four_books_as_books(self):
        books = Product.objects.filter(pk__in=[100, 101, 102, 103])
        self.assertEqual(books.count(), 4)
        for book in books:
            self.assertEqual(book.cat, Product.Categories.BOOKS)
            self.assertEqual(book.tax_code, Product.TaxTypes.BOOKS)
            self.assertTrue(book.isbn)
            self.assertEqual(book.print_isbn, book.isbn)

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

    def test_only_the_two_titles_with_a_listing_carry_a_bookshop_ebook(self):
        for pk, url in BOOKSHOP_EBOOK_URLS.items():
            with self.subTest(pk=pk):
                self.assertEqual(
                    Product.objects.get(pk=pk).bookshop_ebook_link, url)

    def test_bookshop_ebook_urls_keep_the_format_selecting_ean(self):
        # The slug+id alone serves the paperback; ?ean= is what selects the
        # DRM-free e-book. Stripping it would silently sell the wrong format.
        for pk, ean in ((102, "9781492050070"), (103, "9781098118761")):
            with self.subTest(pk=pk):
                url = Product.objects.get(pk=pk).bookshop_ebook_link
                assert url is not None
                self.assertTrue(url.endswith(f"?ean={ean}"), url)

    def test_titles_without_a_bookshop_ebook_listing_have_none(self):
        for pk in BOOKSHOP_EBOOK_ABSENT_PKS:
            with self.subTest(pk=pk):
                product = Product.objects.get(pk=pk)
                self.assertFalse(product.bookshop_ebook_link)
                names = [name for name, _ in product.get_alt_links()]
                self.assertNotIn(BOOKSHOP_EBOOK_LABEL, names)

    def test_bookshop_ebook_link_is_offered_under_its_own_label(self):
        for pk, url in BOOKSHOP_EBOOK_URLS.items():
            with self.subTest(pk=pk):
                links = Product.objects.get(pk=pk).get_alt_links()
                self.assertIn((BOOKSHOP_EBOOK_LABEL, url), links)

    def test_bookshop_print_and_ebook_are_separate_labelled_links(self):
        # The print label is format-neutral, so the e-book must never be
        # emitted under it -- one label meaning two formats is the bug.
        book = Product.objects.get(pk=102)
        links = dict(book.get_alt_links())
        self.assertEqual(links[BOOKSHOP_LABEL], book.bookshop_link)
        self.assertEqual(links[BOOKSHOP_EBOOK_LABEL], book.bookshop_ebook_link)
        self.assertNotEqual(links[BOOKSHOP_LABEL], links[BOOKSHOP_EBOOK_LABEL])

    def test_book_page_offers_the_bookshop_ebook(self):
        response = self.client.get("/product/103")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, BOOKSHOP_EBOOK_LABEL)
        self.assertContains(response, BOOKSHOP_EBOOK_URLS[103])

    def test_blank_alt_link_renders_no_button_and_no_empty_href(self):
        # The filtering in get_alt_links() is what lets six rows leave the
        # field unset with no gating flag; an empty string must drop out too,
        # not render a button pointing at href="".
        # .update() rather than .save(), which would mint a Stripe product.
        self.assertFalse(Product.objects.get(pk=101).bookshop_ebook_link)
        Product.objects.filter(pk=101).update(bookshop_ebook_link="")
        book = Product.objects.get(pk=101)
        self.assertEqual(book.bookshop_ebook_link, "")

        self.assertNotIn(
            BOOKSHOP_EBOOK_LABEL,
            [name for name, _ in book.get_alt_links()])
        self.assertTrue(all(url for _, url in book.get_alt_links()))

        response = self.client.get("/product/101")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, BOOKSHOP_EBOOK_LABEL)
        self.assertNotContains(response, 'href=""')
