"""Tests for the ``seed_products`` management command."""

from unittest import mock

from django.test import TestCase

from main.models import Product


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
            self.assertEqual(book.print_isbn, book.isbn)
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

    def test_preserves_admin_owned_asins_on_existing_product(self):
        Product.objects.create(
            pk=100,
            name="Old name that should be clobbered",
            description="Old desc",
            price=1,
            external_product_id="prod_live",
            cat=Product.Categories.BOOKS,
            isbn="9781449358624",
            default_asin="DEFAULTASIN",
            print_asin="PRINTASIN",
            ebook_asin="EBOOKASIN",
        )

        self._run_seed()

        product = Product.objects.get(pk=100)
        self.assertEqual(product.default_asin, "DEFAULTASIN")
        self.assertEqual(product.print_asin, "PRINTASIN")
        self.assertEqual(product.ebook_asin, "EBOOKASIN")

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

    # -- freshly created rows stay lazily generated ------------------------

    def test_created_rows_have_no_external_product_id(self):
        """Seeding a fresh DB leaves external_product_id unset.

        The fixture documents the Stripe id as generated lazily on first
        add-to-cart; the create path must not pre-generate one.
        """
        self._run_seed()

        for product in Product.objects.filter(pk__in=[100, 101, 102, 103]):
            self.assertFalse(
                product.external_product_id,
                f"pk={product.pk} was seeded with a Stripe id",
            )

    # -- all-or-nothing ----------------------------------------------------

    def test_seed_is_atomic_when_an_entry_fails(self):
        """A failure partway through leaves the database untouched."""
        Product.objects.create(
            pk=100,
            name="Pre-existing",
            price=1,
            external_product_id="prod_live",
            cat=Product.Categories.BOOKS,
        )

        broken_fixture = [
            {
                "model": "main.product",
                "pk": 100,
                "fields": {"name": "Learning Spark (1st edition)", "price": 3999},
            },
            # A field that doesn't exist on Product — blows up mid-seed.
            {
                "model": "main.product",
                "pk": 103,
                "fields": {"name": "Broken", "no_such_field": "boom"},
            },
        ]

        with mock.patch(
            "main.management.commands.seed_products._load_fixture",
            return_value=broken_fixture,
        ):
            with self.assertRaises(Exception):
                self._run_seed()

        # The pk=100 update must have been rolled back with the failure.
        product = Product.objects.get(pk=100)
        self.assertEqual(product.name, "Pre-existing")
        self.assertEqual(product.price, 1)
        self.assertFalse(Product.objects.filter(pk=103).exists())

    # -- an empty fixture is a failure, not a no-op ------------------------

    def test_empty_fixture_fails_loudly(self):
        """A truncated fixture must not report a successful seed."""
        from django.core.management.base import CommandError

        for empty in ([], [{"model": "main.othermodel", "pk": 1, "fields": {}}]):
            with self.subTest(fixture=empty):
                with mock.patch(
                    "main.management.commands.seed_products._load_fixture",
                    return_value=empty,
                ):
                    with self.assertRaises(CommandError):
                        self._run_seed()

    def test_fixture_with_protected_product_field_fails_loudly(self):
        from django.core.management.base import CommandError

        protected_fixture = [
            {
                "model": "main.product",
                "pk": 100,
                "fields": {
                    "name": "Learning Spark (1st edition)",
                    "price": 3999,
                    "print_asin": "",
                },
            }
        ]

        with mock.patch(
            "main.management.commands.seed_products._load_fixture",
            return_value=protected_fixture,
        ):
            with self.assertRaisesMessage(CommandError, "print_asin"):
                self._run_seed()

        self.assertFalse(Product.objects.filter(pk=100).exists())


class SeedProductsStripeTest(TestCase):
    """Seeding must never call Stripe.

    Deliberately does NOT patch ``main.models.Payments`` — the real
    ``Payments.create_product`` stays in place so ``Product.save()``'s Stripe
    path is exercised for real, and only the stripe SDK boundary is
    intercepted.  seed_products runs on the primary pod during startup under
    ``set -e``, so a Stripe outage or an expired key would otherwise stop the
    pod from booting.
    """

    def test_seeding_fresh_db_makes_no_stripe_calls(self):
        from io import StringIO

        from django.core.management import call_command

        with mock.patch("main.payments.stripe.Product.create") as create_product:
            create_product.side_effect = AssertionError(
                "seed_products reached the Stripe API"
            )
            call_command("seed_products", stdout=StringIO())

        create_product.assert_not_called()
        self.assertEqual(
            Product.objects.filter(pk__in=[100, 101, 102, 103]).count(), 4
        )
