from unittest import mock

from django.test import RequestFactory, TestCase

from main.models import Product
from main.utils import get_client_ip

SHIPPING_NOTICE_TEXT = "shipping times for physical goods are currently long"
AMAZON_IN_LABEL = "Buy on Amazon.in (print)"
FLIPKART_LABEL = "Buy on Flipkart (print)"
BOOKSHOP_LABEL = "Buy on Bookshop.org (support local bookstores)"


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

    def test_google_product_feed_includes_books_and_long_handling_times(self):
        response = self.client.get("/google_products.xml")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<g:gtin>9781449358624</g:gtin>")
        self.assertContains(response, "<g:max_handling_time>21</g:max_handling_time>")

    @mock.patch("main.models.Payments")
    def test_cart_with_physical_book_shows_shipping_notice(self, payments):
        payments.create_product.return_value = "prod_test"
        payments.create_price.return_value = "price_test"
        self.client.get("/add-to-cart/100/1")
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


class ClientIpTest(TestCase):
    def test_first_forwarded_for_entry_wins(self):
        request = RequestFactory().get(
            "/", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.244.0.9")
        self.assertEqual(get_client_ip(request), "203.0.113.7")

    def test_falls_back_to_remote_addr(self):
        request = RequestFactory().get("/")
        self.assertEqual(get_client_ip(request), "127.0.0.1")


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
        )

    def test_service_is_not_a_physical_good(self):
        self.assertFalse(self.service.is_physical_good())

    def test_service_page_hides_shipping_notice(self):
        response = self.client.get(f"/product/{self.service.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SHIPPING_NOTICE_TEXT)


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
        payments.create_product.assert_called_once()
        self.assertEqual(product.external_product_id, "prod_new")


class StaticPagesTest(TestCase):
    def test_privacy_page_renders_privacy_template(self):
        response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "privacy.html")

    def test_tos_page_renders_tos_template(self):
        response = self.client.get("/tos")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tos.html")


class SeedProductsCommandTest(TestCase):
    """Tests for the ``seed_products`` management command."""

    def setUp(self):
        # Prevent accidental Stripe API calls in any code path.
        self._payments_patcher = mock.patch("main.models.Payments")
        self.mock_payments = self._payments_patcher.start()
        self.mock_payments.create_product.return_value = "prod_seeded_via_command"
        self.addCleanup(self._payments_patcher.stop)

    # -- helpers -----------------------------------------------------------

    def _run_seed(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command("seed_products", stdout=out)
        return out.getvalue()

    # -- empty database ----------------------------------------------------

    def test_empty_db_creates_all_fixture_rows(self):
        """A fresh database gets all four books created."""
        self.assertEqual(Product.objects.count(), 0)

        output = self._run_seed()
        self.assertIn("created", output.lower())

        books = Product.objects.filter(pk__in=[100, 101, 102, 103])
        self.assertEqual(books.count(), 4)
        for book in books:
            self.assertEqual(book.cat, Product.Categories.BOOKS)
            self.assertTrue(book.isbn)
            self.assertTrue(book.name)

    # -- idempotency -------------------------------------------------------

    def test_running_twice_is_noop(self):
        """Second run changes nothing."""
        self._run_seed()
        before = list(
            Product.objects.filter(pk__in=[100, 101, 102, 103]).values()
        )
        self._run_seed()
        after = list(
            Product.objects.filter(pk__in=[100, 101, 102, 103]).values()
        )

        self.assertEqual(before, after)

    # -- regression: external_product_id survives -------------------------

    def test_preserves_external_product_id_on_existing_product(self):
        """Core regression test: seed must NOT nuke a live Stripe product id.

        Simulates the production probe: a Product already exists at a fixture
        pk with a real external_product_id.  After seeding, the Stripe id
        must survive while fixture-owned fields (price) are updated from the
        fixture.
        """
        # Create a product that looks like it's had live Stripe integration.
        Product.objects.create(
            pk=100,
            name="Old name that should be clobbered",
            description="Old desc",
            price=1,  # deliberately wrong — fixture says 3999
            external_product_id="prod_live_stripe_id_from_add_to_cart",
            cat=Product.Categories.BOOKS,
            isbn="9781449358624",
        )

        self._run_seed()

        product = Product.objects.get(pk=100)
        # Generated field must survive.
        self.assertEqual(
            product.external_product_id, "prod_live_stripe_id_from_add_to_cart"
        )
        # Fixture-owned fields must be updated.
        self.assertEqual(product.price, 3999)
        self.assertEqual(product.name, "Learning Spark (1st edition)")

    # -- non-fixture products untouched ------------------------------------

    def test_non_fixture_products_untouched(self):
        """Products with pk < 100 are unaffected by the seed."""
        non_fixture = Product.objects.create(
            pk=50,
            name="User-created product",
            price=5000,
            external_product_id="prod_handmade",
        )

        self._run_seed()

        product = Product.objects.get(pk=50)
        self.assertEqual(product.name, "User-created product")
        self.assertEqual(product.price, 5000)
        self.assertEqual(product.external_product_id, "prod_handmade")

    # -- fixture field update without clobbering --------------------------

    def test_fixture_field_change_reflected_on_rerun(self):
        """Simulate a deploy that changes a fixture field (e.g. price).

        On the second run the changed field must update while the
        external_product_id is preserved.
        """
        # First run: normal seeding.
        self._run_seed()
        product = Product.objects.get(pk=100)
        self.assertEqual(product.price, 3999)

        # Manually simulate what a previous loaddata would have done:
        # nuke external_product_id and set an old price.
        Product.objects.filter(pk=100).update(
            external_product_id=None, price=2999
        )
        product.refresh_from_db()
        self.assertIsNone(product.external_product_id)
        self.assertEqual(product.price, 2999)

        # Second seed must restore fixture fields but NOT clobber the NULL
        # Stripe id (which means the next add-to-cart will regenerate it
        # once, but future deploys won't re-nuke it).
        self._run_seed()

        product.refresh_from_db()
        self.assertEqual(product.price, 3999)
        # NULL stays NULL — we don't manufacture a Stripe id during seed.
        self.assertIsNone(product.external_product_id)
