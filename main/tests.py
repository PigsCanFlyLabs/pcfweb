from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase, override_settings

from main.models import Cart, CartProduct, Product
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


class CartTestBase(TestCase):
    """Cart tests: stubs Stripe out, since every CartProduct save hits it."""

    fixtures = ["initial_products"]

    def setUp(self):
        patcher = mock.patch("main.models.Payments")
        payments = patcher.start()
        self.addCleanup(patcher.stop)
        payments.create_product.return_value = "prod_test"
        payments.create_price.return_value = "price_test"

    def make_user(self, email="buyer@example.com", username="buyer"):
        return User.objects.create_user(
            username=username, email=email, password="hunter2hunter2")


class CartOwnershipTest(CartTestBase):
    """Regression: removing from a cart used to be an unscoped pk delete."""

    def test_another_session_cannot_remove_someone_elses_cart_row(self):
        victim = Client()
        victim.post("/add-to-cart/100/1")
        cart_product = CartProduct.objects.get()

        attacker = Client()
        response = attacker.post(f"/remove-from-cart/{cart_product.pk}")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(CartProduct.objects.filter(pk=cart_product.pk).exists())
        # And the victim still sees it.
        self.assertContains(victim.get("/cart"), "Learning Spark")

    def test_another_user_cannot_remove_a_logged_in_users_cart_row(self):
        owner = self.make_user()
        victim = Client()
        victim.force_login(owner)
        victim.post("/add-to-cart/100/1")
        cart_product = CartProduct.objects.get()

        attacker = Client()
        attacker.force_login(
            self.make_user(email="thief@example.com", username="thief"))
        response = attacker.post(f"/remove-from-cart/{cart_product.pk}")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(CartProduct.objects.filter(pk=cart_product.pk).exists())

    def test_the_owner_can_still_remove_their_own_row(self):
        self.client.post("/add-to-cart/100/1")
        cart_product = CartProduct.objects.get()

        response = self.client.post(f"/remove-from-cart/{cart_product.pk}")

        self.assertRedirects(response, "/cart")
        self.assertFalse(CartProduct.objects.filter(pk=cart_product.pk).exists())

    def test_a_forged_session_cart_id_does_not_reach_a_user_cart(self):
        owner = self.make_user()
        user_cart = Cart.objects.create(user=owner)

        session = self.client.session
        session["cart_id"] = user_cart.cart_id
        session.save()
        self.client.post("/add-to-cart/100/1")

        self.assertFalse(CartProduct.objects.filter(cart=user_cart).exists())


class CartHttpMethodTest(CartTestBase):
    """Regression: cart mutations used to be GETs, i.e. CSRF-free."""

    def test_add_to_cart_rejects_get(self):
        response = self.client.get("/add-to-cart/100/1")
        self.assertEqual(response.status_code, 405)
        self.assertFalse(CartProduct.objects.exists())

    def test_remove_from_cart_rejects_get(self):
        self.client.post("/add-to-cart/100/1")
        cart_product = CartProduct.objects.get()

        response = self.client.get(f"/remove-from-cart/{cart_product.pk}")

        self.assertEqual(response.status_code, 405)
        self.assertTrue(CartProduct.objects.filter(pk=cart_product.pk).exists())

    def test_add_to_cart_post_without_csrf_token_is_rejected(self):
        strict = Client(enforce_csrf_checks=True)
        response = strict.post("/add-to-cart/100/1")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CartProduct.objects.exists())

    def test_remove_from_cart_post_without_csrf_token_is_rejected(self):
        self.client.post("/add-to-cart/100/1")
        cart_product = CartProduct.objects.get()

        strict = Client(enforce_csrf_checks=True)
        response = strict.post(f"/remove-from-cart/{cart_product.pk}")

        self.assertEqual(response.status_code, 403)
        self.assertTrue(CartProduct.objects.filter(pk=cart_product.pk).exists())

    def test_product_page_posts_to_add_to_cart_with_a_csrf_token(self):
        response = self.client.get("/product/100")
        self.assertContains(response, 'id="add-to-cart-form"')
        self.assertContains(response, 'method="POST"')
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertNotContains(response, 'href="/add-to-cart')

    def test_cart_page_posts_to_remove_from_cart_with_a_csrf_token(self):
        self.client.post("/add-to-cart/100/1")
        cart_product = CartProduct.objects.get()

        response = self.client.get("/cart")

        self.assertContains(
            response, f'action="/remove-from-cart/{cart_product.pk}"')
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertNotContains(response, 'href="/remove-from-cart')


class CartAuthenticationTest(CartTestBase):
    """Regression: `request.user is User` was always False."""

    def test_logged_in_user_gets_their_persistent_cart(self):
        user = self.make_user()
        self.client.force_login(user)

        self.client.post("/add-to-cart/100/1")

        cart = Cart.objects.get(user=user)
        self.assertEqual(
            [cp.product_id for cp in cart.products.all()], [100])
        self.assertNotIn("cart_id", self.client.session)

    def test_persistent_cart_survives_a_new_session(self):
        user = self.make_user()
        first = Client()
        first.force_login(user)
        first.post("/add-to-cart/100/2")

        second = Client()
        second.force_login(user)
        response = second.get("/cart")

        self.assertContains(response, "Learning Spark")
        self.assertEqual(CartProduct.objects.count(), 1)

    def test_login_merges_session_cart_into_an_empty_user_cart(self):
        self.client.post("/add-to-cart/100/2")
        session_cart_id = self.client.session["cart_id"]
        user = self.make_user()

        self.client.force_login(user)
        response = self.client.get("/cart")

        self.assertEqual(response.status_code, 200)
        user_cart = Cart.objects.get(user=user)
        cart_product = CartProduct.objects.get()
        # The reparented row was actually saved, not just mutated in memory.
        self.assertEqual(cart_product.cart_id, user_cart.cart_id)
        self.assertEqual(list(user_cart.products.all()), [cart_product])
        self.assertEqual(cart_product.quantity, 2)
        self.assertFalse(Cart.objects.filter(cart_id=session_cart_id).exists())
        self.assertNotIn("cart_id", self.client.session)

    def test_login_merges_quantities_without_duplicating_rows(self):
        user = self.make_user()
        user_cart = Cart.objects.create(user=user)
        existing = CartProduct.objects.create(
            cart=user_cart, product=Product.objects.get(pk=100), quantity=3,
            price_id="price_existing")
        user_cart.products.add(existing)

        self.client.post("/add-to-cart/100/2")
        self.client.force_login(user)
        response = self.client.get("/cart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CartProduct.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.quantity, 5)
        self.assertEqual(list(user_cart.products.all()), [existing])

    def test_login_merges_distinct_products_into_the_user_cart(self):
        user = self.make_user()
        user_cart = Cart.objects.create(user=user)
        kept = CartProduct.objects.create(
            cart=user_cart, product=Product.objects.get(pk=100), quantity=1,
            price_id="price_existing")
        user_cart.products.add(kept)

        self.client.post("/add-to-cart/101/4")
        self.client.force_login(user)
        response = self.client.get("/cart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(cp.product_id for cp in user_cart.products.all()),
            [100, 101])
        self.assertEqual(
            CartProduct.objects.filter(cart=user_cart).count(), 2)
        moved = CartProduct.objects.get(product_id=101)
        self.assertEqual(moved.cart_id, user_cart.cart_id)
        self.assertEqual(moved.quantity, 4)


class CartQuantityTest(CartTestBase):
    def test_adding_the_same_product_twice_adds_up(self):
        self.client.post("/add-to-cart/100/2")
        self.client.post("/add-to-cart/100/3")

        cart_product = CartProduct.objects.get()
        self.assertEqual(cart_product.quantity, 5)
        self.assertEqual(CartProduct.objects.count(), 1)

    def test_duplicate_cart_rows_are_rejected_by_the_database(self):
        self.client.post("/add-to-cart/100/1")
        cart_product = CartProduct.objects.get()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CartProduct.objects.create(
                    cart=cart_product.cart, product=cart_product.product,
                    quantity=1, price_id="price_duplicate")

    def test_clear_deletes_the_cart_product_rows(self):
        self.client.post("/add-to-cart/100/1")
        self.client.post("/add-to-cart/101/1")
        cart = Cart.objects.get()

        cart.clear()

        self.assertEqual(cart.products.count(), 0)
        self.assertEqual(CartProduct.objects.count(), 0)


class CartBadInputTest(CartTestBase):
    """Regression: ordinary bad input used to be a 500."""

    def test_unknown_product_page_is_a_404(self):
        self.assertEqual(self.client.get("/product/999999").status_code, 404)

    def test_unknown_category_path_is_a_404(self):
        self.assertEqual(self.client.get("/products/bogus").status_code, 404)

    def test_unknown_category_query_param_is_a_404(self):
        response = self.client.get("/products", {"category": "bogus"})
        self.assertEqual(response.status_code, 404)

    def test_lower_case_category_still_works(self):
        response = self.client.get("/products/b")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Learning Spark")

    def test_adding_an_unknown_product_is_a_404(self):
        response = self.client.post("/add-to-cart/999999/1")
        self.assertEqual(response.status_code, 404)

    def test_non_numeric_quantity_is_a_404(self):
        self.assertEqual(
            self.client.post("/add-to-cart/100/abc").status_code, 404)

    def test_negative_quantity_is_a_404(self):
        self.assertEqual(
            self.client.post("/add-to-cart/100/-5").status_code, 404)
        self.assertFalse(CartProduct.objects.exists())

    def test_zero_quantity_is_a_400(self):
        self.assertEqual(
            self.client.post("/add-to-cart/100/0").status_code, 400)
        self.assertFalse(CartProduct.objects.exists())

    def test_removing_an_unknown_cart_row_is_a_404(self):
        self.assertEqual(
            self.client.post("/remove-from-cart/999999").status_code, 404)

    def test_stale_cart_id_cookie_recovers_instead_of_500ing(self):
        session = self.client.session
        session["cart_id"] = 999999
        session.save()

        response = self.client.get("/cart")

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.client.session.get("cart_id"), 999999)
        # And the cart keeps working afterwards.
        self.assertEqual(
            self.client.post("/add-to-cart/100/1").status_code, 302)
        self.assertEqual(self.client.get("/cart").status_code, 200)


class ProductUrlTest(TestCase):
    fixtures = ["initial_products"]

    def test_get_absolute_url_reverses_to_the_product_page(self):
        product = Product.objects.get(pk=100)
        self.assertEqual(product.get_absolute_url(), "/product/100")
        response = self.client.get(product.get_absolute_url())
        self.assertEqual(response.status_code, 200)


# The home page thumbnails assets/images/*, which build.sh copies in from the
# sibling pcfweb-assets repo and which is .gitignore'd -- so it is absent in a
# plain checkout. THUMBNAIL_DEBUG makes a missing source raise; turn it off
# here so the smoke test covers the view rather than the asset drop.
@override_settings(THUMBNAIL_DEBUG=False)
class PageSmokeTest(TestCase):
    """These pages had no coverage at all; at minimum they must render."""

    fixtures = ["initial_products"]

    def test_public_pages_render(self):
        for path in ["/", "/products", "/products/B", "/services", "/subscribe",
                     "/about", "/contact", "/returns", "/signup", "/login",
                     "/cart"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    @mock.patch("main.views.Payments")
    def test_checkout_redirects_to_the_payment_provider(self, payments):
        payments.checkout.return_value = "https://checkout.example/session"
        response = self.client.get("/checkout")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], "https://checkout.example/session")

    def test_logout_requires_a_login(self):
        response = self.client.get("/logout")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])
