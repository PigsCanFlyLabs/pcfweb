"""pk 108, High Performance Spark 2nd edition, and its separation from pk 101.

The 2nd edition is a *new row*, not an edit to the 1st. Both editions are in
print and both are sold, so the failure this module is written against is the
one where the two rows bleed into each other: the 2nd edition's Kindle ASIN
landing on the 1st edition's row (which would sell a reader the wrong book),
the two sharing a cover, or the two sharing an ISBN and therefore a
<g:gtin> in the Google Merchant feed.

Every identifier asserted here was verified against a live retrieved page --
see the comments on the fixture row. The two identifiers that could NOT be
verified are asserted to be *blank*, deliberately: a test that pinned a
guessed EPUB ISBN would make the guess permanent.
"""

import re
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from main.models import Product

FIRST_EDITION_PK = 101
SECOND_EDITION_PK = 108

# Verified 2026-07-27 against the live Amazon paperback listing, whose product
# details read "ISBN-10 1098145852" and "ISBN-13 978-1098145859".
SECOND_EDITION_PRINT_ISBN = "9781098145859"
SECOND_EDITION_ISBN10 = "1098145852"
# Verified on the Kindle listing itself, which the paperback page links from
# its KINDLE format swatch -- edition linkage, not a title match.
SECOND_EDITION_ASIN = "B0H3CMNN3Q"

FIRST_EDITION_PRINT_ISBN = "9781491943205"


@override_settings(THUMBNAIL_DEBUG=False)
class SecondEditionFixtureRowTest(TestCase):
    """The row itself: identity, identifiers and the deliberate blanks."""

    fixtures = ["initial_products"]

    def book(self):
        return Product.objects.get(pk=SECOND_EDITION_PK)

    def test_the_second_edition_is_a_book_in_the_catalogue(self):
        book = self.book()

        self.assertEqual(book.name, "High Performance Spark, 2nd Edition")
        self.assertEqual(book.cat, Product.Categories.BOOKS)
        self.assertEqual(book.tax_code, Product.TaxTypes.BOOKS)
        # O'Reilly, so get_brand()'s default for Books is correct here and
        # `brand` is deliberately unset -- unlike pk 107, which is Packt.
        self.assertEqual(book.get_brand(), "O'Reilly")

    def test_it_carries_the_verified_print_isbn_in_both_columns(self):
        book = self.book()

        self.assertEqual(book.print_isbn, SECOND_EDITION_PRINT_ISBN)
        # `isbn` is the legacy column and the isbn -> print_isbn backfill is a
        # one-shot migration that ran before this row existed, so setting only
        # one of the two would drop the row's <g:gtin>.
        self.assertEqual(book.isbn, SECOND_EDITION_PRINT_ISBN)
        self.assertEqual(book.get_gtin(), SECOND_EDITION_PRINT_ISBN)

    def test_its_ebook_isbn_is_blank_because_none_could_be_verified(self):
        """The blank is the finding, so it is what gets pinned.

        No retail EPUB ISBN for this edition is published anywhere reachable:
        O'Reilly's catalogue record carries the print ISBN in its only `isbn`
        field, and every e-book retailer that would list one is behind a bot
        wall. The specific hazard this guards is somebody "completing" the row
        by copying the print ISBN across, or by using O'Reilly's platform id
        9781098145842 -- which is the /library/view/ URL id and has never been
        an ebook_isbn on any row in this file.
        """
        book = self.book()

        self.assertFalse(book.ebook_isbn)
        self.assertNotEqual(book.ebook_isbn, SECOND_EDITION_PRINT_ISBN)
        self.assertNotEqual(book.ebook_isbn, "9781098145842")

    def test_it_offers_no_unverified_retailer_buttons(self):
        """Bookshop was unreachable and Flipkart's ISBN search is junk.

        Flipkart's /search?q=9781098145859 returns thousands of watches and
        phone cases and not one mention of the book, so the sibling rows'
        search-URL pattern would be a dead button here.
        """
        book = self.book()

        self.assertFalse(book.bookshop_link)
        self.assertFalse(book.bookshop_ebook_link)
        self.assertFalse(book.flipkart_link)

    def test_its_amazon_links_use_the_isbn10_off_the_live_listing(self):
        book = self.book()

        self.assertEqual(
            book.get_amazon_link(),
            f"https://www.amazon.com/dp/{SECOND_EDITION_ISBN10}")
        self.assertEqual(
            book.get_amazon_in_link(),
            f"https://www.amazon.in/dp/{SECOND_EDITION_ISBN10}")

    def test_it_is_on_the_oreilly_platform(self):
        labels = [label for label, _ in self.book().get_alt_links()]

        self.assertTrue(self.book().on_oreilly_safari)
        self.assertIn("Read on O'Reilly Safari (free trial)", labels)

    def test_it_has_its_own_cover_not_the_first_editions(self):
        """A shared cover would put the 1st edition's art on the 2nd's page.

        The file itself lives in the sibling pcfweb-assets repo, which is why
        only the reference is asserted here.
        """
        second = self.book()
        first = Product.objects.get(pk=FIRST_EDITION_PK)

        self.assertEqual(
            second.image_name, "book_covers/high_performance_spark_2ed.jpg")
        self.assertNotEqual(second.image_name, first.image_name)

    def test_its_page_is_reachable_and_names_the_new_coauthor(self):
        response = self.client.get(f"/product/{SECOND_EDITION_PK}")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "High Performance Spark, 2nd Edition")
        # Adi Polak is the author the 2nd edition added; her name appearing is
        # what distinguishes this description from the 1st edition's.
        self.assertContains(response, "Adi Polak")
        self.assertContains(response, "Spark 4.x")

    def test_its_page_renders_no_empty_hrefs_from_the_unset_links(self):
        response = self.client.get(f"/product/{SECOND_EDITION_PK}")

        self.assertNotContains(response, 'href=""')
        self.assertNotContains(response, "bookshop.org")
        self.assertNotContains(response, "flipkart.com")


@override_settings(THUMBNAIL_DEBUG=False)
class BothEditionsCoexistTest(TestCase):
    """Two editions, two rows, no bleed between them."""

    fixtures = ["initial_products"]

    def test_the_first_edition_names_its_edition(self):
        """The one field on pk 101 that the 2nd edition is allowed to change.

        Deliberately a separate test from the identifier guard below, and
        deliberately still an assertEqual against the full expected string.
        The rename is an intentional product decision, so it gets pinned as
        precisely as the thing it was carved out of -- widening the identifier
        guard to tolerate "any name" would have thrown away the coverage that
        catches pk 101 being edited into the 2nd edition wholesale.
        """
        self.assertEqual(
            Product.objects.get(pk=FIRST_EDITION_PK).name,
            "High Performance Spark (1st edition)")

    def test_the_first_editions_identifiers_are_untouched(self):
        """Renaming the row must not have moved a single identifier on it.

        This is the guard the rename could have quietly destroyed: every
        column that identifies *which book* pk 101 is, asserted against the
        1st edition's own values and against the 2nd edition's, so that
        neither a copy-across nor an in-place "upgrade" of this row passes.
        """
        first = Product.objects.get(pk=FIRST_EDITION_PK)

        self.assertEqual(first.print_isbn, FIRST_EDITION_PRINT_ISBN)
        self.assertEqual(first.isbn, FIRST_EDITION_PRINT_ISBN)
        # The 1st edition's own retail EPUB ISBN is still pinned; adding the
        # 2nd edition must not have disturbed it.
        self.assertEqual(first.ebook_isbn, "9781491943151")
        self.assertEqual(first.image_name,
                         "book_covers/high_performance_spark.jpg")
        self.assertEqual(first.price, 4999)

        # And explicitly none of the 2nd edition's identifiers.
        self.assertNotEqual(first.print_isbn, SECOND_EDITION_PRINT_ISBN)
        self.assertNotEqual(first.isbn, SECOND_EDITION_PRINT_ISBN)
        self.assertNotEqual(first.ebook_isbn, SECOND_EDITION_PRINT_ISBN)
        self.assertNotIn(SECOND_EDITION_ISBN10, first.amazon_link or "")

    def test_the_first_edition_is_still_in_print_and_purchasable(self):
        """The rename is the whole change: no delisting rode in with it."""
        first = Product.objects.get(pk=FIRST_EDITION_PK)

        self.assertFalse(first.noorder)
        self.assertIn("High Performance Spark (1st edition)",
                      self.client.get("/products").content.decode())

    def test_the_second_editions_asin_never_reaches_the_first_editions_row(self):
        """The wrong-book failure, asserted directly.

        Amazon delisted the 1st edition's Kindle edition, so pk 101 has no
        ebook_asin. The temptation is to fill that blank with the 2nd
        edition's ASIN now that it is in the file a few rows down.
        """
        first = Product.objects.get(pk=FIRST_EDITION_PK)

        self.assertFalse(first.ebook_asin)
        self.assertIsNone(first.get_kindle_link())

        response = self.client.get(f"/product/{FIRST_EDITION_PK}")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SECOND_EDITION_ASIN)

    def test_the_two_editions_do_not_share_a_gtin(self):
        first = Product.objects.get(pk=FIRST_EDITION_PK)
        second = Product.objects.get(pk=SECOND_EDITION_PK)

        self.assertNotEqual(first.get_gtin(), second.get_gtin())
        # Anti-vacuity: both really do emit one.
        self.assertTrue(first.get_gtin())
        self.assertTrue(second.get_gtin())

    def test_both_editions_appear_in_the_merchant_feed_separately(self):
        body = self.client.get("/google_products.xml").content.decode()

        self.assertIn(f"<g:id>{FIRST_EDITION_PK}</g:id>", body)
        self.assertIn(f"<g:id>{SECOND_EDITION_PK}</g:id>", body)
        self.assertIn(f"<g:gtin>{SECOND_EDITION_PRINT_ISBN}</g:gtin>", body)
        self.assertIn(f"<g:gtin>{FIRST_EDITION_PRINT_ISBN}</g:gtin>", body)

    def test_the_second_edition_is_listed_on_the_products_page(self):
        html = self.client.get("/products").content.decode()

        self.assertIn("High Performance Spark, 2nd Edition", html)

    def test_neither_edition_is_the_unsuffixed_high_performance_spark(self):
        """What the rename was for.

        Two cards differing only by an absent suffix is the confusion: the
        unsuffixed one reads as *the* book rather than the older one, and it
        is also the cheaper of the two. So no row may be named bare.
        """
        self.assertFalse(
            Product.objects.filter(name="High Performance Spark").exists())
        # Anti-vacuity: both rows are really there, each naming its edition.
        self.assertEqual(
            Product.objects.filter(
                name__startswith="High Performance Spark").count(), 2)


class SecondEditionSeedsOnACleanDeployTest(TestCase):
    """The ASIN has to arrive from the fixture, via the real deploy path.

    ``ebook_asin`` is fixture-owned, so ``manage.py seed_products`` -- which
    scripts/start-server.sh runs under `set -euo pipefail` -- must accept this
    row and write the ASIN. If ebook_asin were ever put back into
    SEED_PROTECTED_FIELDS, this row would make the command exit 1 and stop the
    primary pod from booting, so seeding is exercised rather than loaddata.
    """

    def setUp(self):
        patcher = mock.patch("main.models.Payments")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.out = StringIO()
        call_command("seed_products", stdout=self.out)

    def test_seeding_creates_the_row_without_error(self):
        self.assertIn(f"Created product pk={SECOND_EDITION_PK}",
                      self.out.getvalue())

    def test_seeding_populates_the_verified_kindle_asin(self):
        self.assertEqual(
            Product.objects.get(pk=SECOND_EDITION_PK).ebook_asin,
            SECOND_EDITION_ASIN)

    def test_the_seeded_asin_becomes_the_kindle_link(self):
        self.assertEqual(
            Product.objects.get(pk=SECOND_EDITION_PK).get_kindle_link(),
            f"https://www.amazon.com/dp/{SECOND_EDITION_ASIN}")

    @override_settings(THUMBNAIL_DEBUG=False)
    def test_the_seeded_asin_reaches_the_product_page(self):
        response = self.client.get(f"/product/{SECOND_EDITION_PK}")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(f"https://www.amazon.com/dp/{SECOND_EDITION_ASIN}", body)
        self.assertIn(Product.AMAZON_EBOOK_LABEL, body)


@override_settings(THUMBNAIL_DEBUG=False)
class ProductsPageReleaseDateOrderingTest(TestCase):
    """The /products and /products/<category> pages sort by release_date DESC,
    NULLS LAST, with pk as the tiebreaker.

    This exercises the REAL VIEW via ``self.client.get`` and derives the order
    from product-card links parsed out of the rendered HTML.  It does not
    construct a queryset, import the view's ordering expression, or inspect
    ``order_by`` — it tests what the browser sees.
    """

    fixtures = ["initial_products"]

    @staticmethod
    def _product_pks_from_html(html: str) -> list[int]:
        """Extract product PKs from ``/product/<pk>`` links in render order."""
        pks: list[int] = []
        seen: set[int] = set()
        for m in re.finditer(r'/product/(\d+)', html):
            pk = int(m.group(1))
            if pk not in seen:
                seen.add(pk)
                pks.append(pk)
        return pks

    # -- main /products page --------------------------------------------------

    def test_hps_2nd_edition_outranks_1st_edition_in_the_rendered_page(self):
        """pk 108 (2026-06-05) appears before pk 101 (2017-06-16)
        in the rendered /products page."""
        response = self.client.get("/products")
        pks = self._product_pks_from_html(response.content.decode())

        self.assertIn(108, pks, "HPS 2nd edition missing from products page")
        self.assertIn(101, pks, "HPS 1st edition missing from products page")

        idx_108 = pks.index(108)
        idx_101 = pks.index(101)

        self.assertLess(
            idx_108, idx_101,
            f"HPS 2nd edition (pk 108) must appear before "
            f"HPS 1st edition (pk 101). Got order: {pks}",
        )

    def test_products_are_ordered_newest_first(self):
        """The overall /products order matches release_date DESC, pk tiebreak."""
        response = self.client.get("/products")
        pks = self._product_pks_from_html(response.content.decode())

        # 104/105/106 (2026-06-28, pk tiebreak), 108 (2026-06-05),
        # 103 (2022-11-29), 102 (2020-10-13), 101 (2017-06-16), 100 (2015-02-27)
        expected = [104, 105, 106, 108, 103, 102, 101, 100]
        self.assertEqual(
            pks, expected,
            f"Expected newest-first order {expected}, got {pks}",
        )

    def test_null_release_date_sinks_to_bottom(self):
        """NULL release_date rows appear last (NULLS LAST)."""
        original = Product.objects.get(pk=100).release_date
        Product.objects.filter(pk=100).update(release_date=None)
        try:
            response = self.client.get("/products")
            pks = self._product_pks_from_html(response.content.decode())

            self.assertIn(100, pks, "Product 100 missing from rendered page")
            self.assertEqual(
                pks[-1], 100,
                f"Null-date product must be last. Got order: {pks}",
            )
        finally:
            Product.objects.filter(pk=100).update(release_date=original)

    def test_no_products_are_missing_from_products_page(self):
        """All non-noorder products are present in the rendered /products page."""
        response = self.client.get("/products")
        pks = self._product_pks_from_html(response.content.decode())

        # pk 107 is noorder=True, so it is excluded from /products
        expected = {100, 101, 102, 103, 104, 105, 106, 108}
        self.assertEqual(set(pks), expected)

    # -- category page /products/B -------------------------------------------

    def test_category_page_books_sorted_newest_first(self):
        """The /products/B category page also sorts by release_date DESC."""
        response = self.client.get("/products/B")
        pks = self._product_pks_from_html(response.content.decode())

        # Same expected order as the main /products page — all non-noorder
        # products are currently in the Books category.
        expected = [104, 105, 106, 108, 103, 102, 101, 100]
        self.assertEqual(
            pks, expected,
            f"Expected books in newest-first order {expected}, got {pks}",
        )

@override_settings(THUMBNAIL_DEBUG=False)
class HomepageOurBooksCarouselOrderingTest(TestCase):
    """The homepage "Our Books" carousel uses the same order_by_release_date
    ordering as the products page, sliced to [:3].  This exercises the REAL
    VIEW via ``self.client.get('/')`` and extracts product PKs from the
    rendered HTML.  It never constructs a queryset or imports the view's
    ordering expression.
    """

    fixtures = ["initial_products"]

    @staticmethod
    def _product_pks_from_html(html: str) -> list[int]:
        """Extract product PKs from "/product/<pk>" links in render order."""
        pks: list[int] = []
        seen: set[int] = set()
        for m in re.finditer(r'/product/(\d+)', html):
            pk = int(m.group(1))
            if pk not in seen:
                seen.add(pk)
                pks.append(pk)
        return pks

    def test_homepage_is_reachable(self):
        """The homepage renders successfully."""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)

    def test_our_books_carousel_contains_the_three_newest_books(self):
        """After switching from '-price' to order_by_release_date(), the [:3]
        slice on the homepage carousel selects the three DC4K SKUs (all dated
        2026-06-28, pk-ascending tiebreak at 104/105/106).  This drops High
        Performance Spark 2e, Ray and Kubeflow off the homepage entirely.
        """
        response = self.client.get('/')
        html = response.content.decode()
        pks = self._product_pks_from_html(html)

        # The page contains the hero link (pk 104) plus three carousel
        # entries (104, 105, 106). After deduplication, the last three
        # unique PKs should be the carousel.
        unique_pks = list(dict.fromkeys(pks))
        carousel_pks = unique_pks[-3:] if len(unique_pks) >= 3 else []

        self.assertEqual(
            carousel_pks, [104, 105, 106],
            f"Carousel expected [104, 105, 106] but got {carousel_pks}. "
            f"Full unique order: {unique_pks}",
        )

        # None of the other books appear in the carousel slice.
        not_in_carousel = [108, 103, 102, 101, 100]
        for pk in not_in_carousel:
            self.assertNotIn(
                pk, carousel_pks,
                f"pk {pk} should not appear in the carousel slice "
                f"(it was displaced by DC4K filling all three [:3] slots). "
                f"Carousel: {carousel_pks}",
            )

    def test_carousel_ordering_matches_products_page_head(self):
        """The homepage carousel [:3] is exactly the head of the
        /products page ordering."""
        home_response = self.client.get('/')
        home_pks = list(dict.fromkeys(
            self._product_pks_from_html(home_response.content.decode())))
        carousel_pks = home_pks[-3:]

        products_response = self.client.get('/products')
        products_pks = self._product_pks_from_html(
            products_response.content.decode())

        self.assertEqual(
            carousel_pks, products_pks[:3],
            f"Homepage carousel {carousel_pks} must equal "
            f"/products head {products_pks[:3]}",
        )
