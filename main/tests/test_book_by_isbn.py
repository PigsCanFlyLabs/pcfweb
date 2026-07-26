"""/book/<isbn> -- the stable ISBN alias for a product page.

The route exists so templates can link a book without hardcoding a fixture
primary key. A hardcoded pk does not fail loudly when the row moves; it keeps
resolving, to whatever product later holds that number. An ISBN is the book's
own identity and survives a reseed.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from main.models import Product
from main.views import BookByIsbnView


@override_settings(THUMBNAIL_DEBUG=False)
class BookByIsbnRedirectTest(TestCase):
    fixtures = ["initial_products"]

    # Every book the /services credentials cite, with the pk it must reach.
    CATALOGUE = {
        "9781449358624": 100,   # Learning Spark (1st edition)
        "9781491943205": 101,   # High Performance Spark
        "9781492050124": 102,   # Kubeflow for Machine Learning
        "9781098118808": 103,   # Scaling Python with Ray
        "9781782167068": 107,   # Fast Data Processing with Spark
    }

    def test_every_credential_isbn_redirects_to_its_product_page(self):
        for isbn, pk in self.CATALOGUE.items():
            with self.subTest(isbn=isbn):
                response = self.client.get(f"/book/{isbn}")

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], f"/product/{pk}")

    def test_a_hyphenated_isbn_finds_the_same_book(self):
        """`978-1-960595-99-7` is what people paste off a back cover."""
        response = self.client.get("/book/978-1-960595-99-7")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/product/104")
        # Anti-vacuity: the bare form reaches the same place.
        self.assertEqual(
            self.client.get("/book/9781960595997")["Location"],
            "/product/104")

    def test_spaced_and_mixed_separators_also_normalise(self):
        for raw in ("978 1 960595 99 7", "978-1 960595—99‐7", " 9781960595997 "):
            with self.subTest(raw=raw):
                response = self.client.get(f"/book/{raw}")

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], "/product/104")

    def test_the_ebook_is_reachable_by_its_own_ebook_isbn(self):
        """pk 106 has ebook_isbn and no print_isbn, so the fallback matters."""
        response = self.client.get("/book/9781960595980")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/product/106")

    def test_an_unknown_isbn_is_a_404_not_a_redirect(self):
        """Bouncing to /products would assert the book exists here.

        A wrong URL is a wrong URL; telling a crawler that any 13 digits
        resolve to the catalogue is worse than telling it nothing.
        """
        response = self.client.get("/book/9780000000002")

        self.assertEqual(response.status_code, 404)

    def test_a_non_isbn_string_is_a_404(self):
        for raw in ("not-an-isbn", "----", "0"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    self.client.get(f"/book/{raw}").status_code, 404)

    def test_an_over_long_isbn_is_rejected_without_querying(self):
        """No reason to normalise and run three lookups over a huge URL.

        Not a validity check -- anything that is not a real ISBN 404s on the
        lookup anyway. This just bounds the work an arbitrary URL can cause.
        """
        over_long = "9" * (BookByIsbnView.MAX_ISBN_LENGTH + 1)

        with self.assertNumQueries(0):
            response = self.client.get(f"/book/{over_long}")

        self.assertEqual(response.status_code, 404)

    def test_a_very_long_url_is_still_rejected(self):
        response = self.client.get("/book/" + "1" * 5000)

        self.assertEqual(response.status_code, 404)

    def test_the_bound_is_generous_enough_for_a_separated_isbn13(self):
        """The cap must not reject something a person would plausibly paste."""
        self.assertGreaterEqual(BookByIsbnView.MAX_ISBN_LENGTH, len("978-1-960595-99-7"))

        response = self.client.get("/book/978 - 1 - 960595 - 99 - 7")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/product/104")

    def test_the_redirect_target_actually_renders(self):
        """A 302 to a 404 would be a worse bug than no route at all."""
        for isbn, pk in self.CATALOGUE.items():
            with self.subTest(isbn=isbn):
                response = self.client.get(f"/book/{isbn}", follow=True)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.redirect_chain, [(f"/product/{pk}", 302)])

    def test_it_does_not_render_the_product_page_itself(self):
        """One canonical URL. A second rendering would split SEO."""
        response = self.client.get("/book/9781449358624")

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(b"add-to-cart-form", response.content)

    def test_the_route_is_reversible_by_name(self):
        self.assertEqual(
            reverse("book-by-isbn", kwargs={"isbn": "9781449358624"}),
            "/book/9781449358624")

    def test_it_survives_the_product_moving_to_a_different_pk(self):
        """The entire point: pks are seeded data, ISBNs are the book.

        A template linking /product/100 would silently point at whatever row
        later holds pk 100. Linking by ISBN follows the book instead.
        """
        book = Product.objects.get(pk=100)
        isbn = book.print_isbn
        Product.objects.filter(pk=100).delete()
        book.pk = 900
        book.save_base(raw=True, force_insert=True)

        response = self.client.get(f"/book/{isbn}")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/product/900")


class IsbnNormalisationTest(TestCase):
    """The normaliser on its own, including the ISBN-10 trailing X."""

    def normalise(self, raw):
        return BookByIsbnView.normalise(raw)

    def test_separators_are_stripped(self):
        self.assertEqual(self.normalise("978-1-4493-5862-4"), "9781449358624")
        self.assertEqual(self.normalise("978 1 4493 5862 4"), "9781449358624")
        self.assertEqual(self.normalise(" 9781449358624 "), "9781449358624")

    def test_a_trailing_isbn10_check_x_is_upper_cased(self):
        # ISBN-10 uses X as the check value for 10. Lower case is what a
        # hand-typed URL produces, and the stored form is upper.
        self.assertEqual(self.normalise("043942089x"), "043942089X")
        self.assertEqual(self.normalise("0-439-42089-x"), "043942089X")

    def test_digits_are_left_alone(self):
        self.assertEqual(self.normalise("9781449358624"), "9781449358624")
