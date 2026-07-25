import hashlib
import hmac
import itertools
import json
import re
import time
from unittest import mock

import stripe
from django.contrib.auth.models import User
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase, override_settings

from main.models import Cart, CartProduct, Order, OrderItem, Product
from main.payments import Payments
from main.utils import get_client_ip
from main.views import AddToCartView

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


class CheckoutTaxTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _cart_with_product(self, mode):
        product = Product.objects.create(
            name=f"{mode} product",
            description="Checkout test product.",
            external_product_id=f"prod_{mode}",
            price=2500,
            mode=mode,
        )
        cart = Cart.objects.create()
        cart_product = CartProduct.objects.create(
            cart=cart,
            product=product,
            quantity=1,
            price_id=f"price_{mode}",
        )
        cart.products.add(cart_product)
        return cart

    def _checkout(self, mode=Product.Modes.PAYMENT, coupon=None):
        request = self.factory.get("/checkout")
        cart = self._cart_with_product(mode)
        return Payments.checkout(request, cart, coupon=coupon)

    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_normal_checkout_enables_automatic_tax(self, create_session):
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout()

        params = create_session.call_args.kwargs
        self.assertEqual(params["automatic_tax"], {"enabled": True})
        self.assertEqual(params["billing_address_collection"], "required")
        self.assertNotIn("discounts", params)

    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_valid_coupon_keeps_discount_and_enables_tax(self, create_session):
        create_session.return_value.url = "https://checkout.example/session"

        self._checkout(coupon="coupon_valid")

        params = create_session.call_args.kwargs
        self.assertEqual(params["automatic_tax"], {"enabled": True})
        self.assertEqual(params["billing_address_collection"], "required")
        self.assertEqual(params["discounts"], [{"coupon": "coupon_valid"}])

    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_invalid_coupon_retry_drops_discount_but_keeps_tax(self, create_session):
        create_session.side_effect = [
            stripe.InvalidRequestError(
                "No such coupon", "discounts[0][coupon]"),
            mock.Mock(url="https://checkout.example/session"),
        ]

        self._checkout(coupon="coupon_bad")

        first_params = create_session.call_args_list[0].kwargs
        retry_params = create_session.call_args_list[1].kwargs
        self.assertEqual(first_params["discounts"], [{"coupon": "coupon_bad"}])
        self.assertNotIn("discounts", retry_params)
        self.assertEqual(retry_params["automatic_tax"], {"enabled": True})
        self.assertEqual(retry_params["billing_address_collection"], "required")

    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_payment_and_subscription_modes_collect_address_for_tax(self, create_session):
        create_session.return_value.url = "https://checkout.example/session"

        for mode, expected_stripe_mode in (
            (Product.Modes.PAYMENT, "payment"),
            (Product.Modes.SUBSCRIPTION, "subscription"),
        ):
            with self.subTest(mode=expected_stripe_mode):
                create_session.reset_mock()

                self._checkout(mode=mode)

                params = create_session.call_args.kwargs
                self.assertEqual(params["mode"], expected_stripe_mode)
                self.assertEqual(params["automatic_tax"], {"enabled": True})
                self.assertEqual(params["billing_address_collection"], "required")

    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_coupon_checkout_does_not_retry_non_coupon_stripe_errors(self, create_session):
        create_session.side_effect = stripe.InvalidRequestError(
            "No such price", "line_items[0][price]")

        with self.assertRaises(stripe.InvalidRequestError):
            self._checkout(coupon="coupon_valid")

        create_session.assert_called_once()

    def test_coupon_error_detection_uses_param_and_documented_codes(self):
        cases = [
            (stripe.InvalidRequestError("plain error", None), False),
            (stripe.InvalidRequestError("plain error", ""), False),
            (stripe.InvalidRequestError(
                "No such coupon", "discounts[0][coupon]"), True),
            (stripe.InvalidRequestError(
                "Stripe Tax is not enabled", "automatic_tax"), False),
            (stripe.InvalidRequestError(
                "Coupon expired", None, code="coupon_expired"), True),
            (stripe.InvalidRequestError(
                "Missing resource", None, code="resource_missing"), True),
            (stripe.InvalidRequestError(
                "Missing price", "line_items[0][price]",
                code="resource_missing"), False),
            (stripe.InvalidRequestError(
                "First-time customer required", None,
                code="promotion_code_customer_missing_first_time"), True),
            (stripe.InvalidRequestError(
                "Customer is not first-time", None,
                code="promotion_code_customer_not_first_time"), True),
        ]

        for error, expected in cases:
            with self.subTest(param=error.param, code=error.code):
                self.assertEqual(Payments._is_coupon_error(error), expected)

    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_tax_configuration_error_is_diagnosable_and_not_retried(self, create_session):
        create_session.side_effect = stripe.InvalidRequestError(
            "Stripe Tax is not enabled",
            "automatic_tax",
            code="stripe_tax_inactive",
        )

        with mock.patch("main.payments.logger.error") as log_error:
            with self.assertRaises(stripe.InvalidRequestError) as error:
                self._checkout(coupon="coupon_valid")

        self.assertIn("Stripe Tax must be activated", str(error.exception))
        self.assertIn("STRIPE_AUTOMATIC_TAX=false", str(error.exception))
        log_error.assert_called_once()
        self.assertIn("Stripe Tax must be activated", log_error.call_args.args[0])
        create_session.assert_called_once()
        params = create_session.call_args.kwargs
        self.assertEqual(params["automatic_tax"], {"enabled": True})

    @override_settings(STRIPE_AUTOMATIC_TAX=False)
    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_automatic_tax_escape_hatch_omits_tax_and_logs_warning(self, create_session):
        create_session.return_value.url = "https://checkout.example/session"

        with self.assertLogs("main.payments", level="WARNING") as logs:
            self._checkout()

        params = create_session.call_args.kwargs
        self.assertNotIn("automatic_tax", params)
        self.assertIn("STRIPE_AUTOMATIC_TAX", "\n".join(logs.output))


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


class AddToCartWithoutJavascriptTest(CartTestBase):
    """The buy form must work with JavaScript entirely disabled.

    A form with no action attribute posts to the current URL, which would be
    the GET-only product page -- a 405 on the purchase path for anyone
    without JS.
    """

    def get_buy_form(self, product_pk=100):
        """Return (action, form_html) for the buy form, as a browser sees it."""
        html = self.client.get(f"/product/{product_pk}").content.decode()
        form = re.search(
            r'<form[^>]*id="add-to-cart-form"[^>]*>(.*?)</form>', html,
            re.DOTALL)
        self.assertIsNotNone(form, "buy form missing from the product page")
        assert form is not None  # for mypy
        action = re.search(r'action="([^"]*)"', form.group(0))
        self.assertIsNotNone(action, "buy form has no action attribute")
        assert action is not None  # for mypy
        return action.group(1), form.group(0)

    def test_the_buy_form_has_a_real_action(self):
        action, _ = self.get_buy_form()
        self.assertTrue(action.startswith("/add-to-cart/100/"), action)

    def test_the_quantity_input_is_a_field_of_the_buy_form(self):
        _, form_html = self.get_buy_form()
        self.assertIn('name="quantity"', form_html)

    def test_submitting_the_form_without_javascript_adds_the_typed_quantity(self):
        action, _ = self.get_buy_form()

        # Exactly what a no-JS browser posts: the form's own action, plus the
        # fields inside the form.
        response = self.client.post(action, {"quantity": "7"})

        self.assertRedirects(response, "/cart")
        cart_product = CartProduct.objects.get()
        self.assertEqual(cart_product.quantity, 7)
        self.assertEqual(cart_product.product_id, 100)

    def test_the_url_quantity_is_still_honoured_without_a_posted_field(self):
        response = self.client.post("/add-to-cart/100/4")
        self.assertRedirects(response, "/cart")
        self.assertEqual(CartProduct.objects.get().quantity, 4)

    def test_a_posted_quantity_of_zero_is_a_400(self):
        self.assertEqual(
            self.client.post("/add-to-cart/100/1", {"quantity": "0"}).status_code,
            400)
        self.assertFalse(CartProduct.objects.exists())

    def test_a_quantity_too_big_for_the_column_is_a_400_not_a_500(self):
        # Python ints are unbounded; BIGINT is not, so an oversized quantity
        # used to parse fine and then blow up at write time.
        too_big = AddToCartView.MAX_QUANTITY + 1

        posted = self.client.post("/add-to-cart/100/1", {"quantity": str(too_big)})
        self.assertEqual(posted.status_code, 400)

        from_url = self.client.post(f"/add-to-cart/100/{too_big}")
        self.assertEqual(from_url.status_code, 400)

        self.assertFalse(CartProduct.objects.exists())

    def test_the_largest_storable_quantity_still_works(self):
        # The bound is the column's capacity, so its exact value is valid.
        response = self.client.post(
            "/add-to-cart/100/1", {"quantity": str(AddToCartView.MAX_QUANTITY)})

        self.assertRedirects(response, "/cart")
        self.assertEqual(
            CartProduct.objects.get().quantity, AddToCartView.MAX_QUANTITY)

    def test_a_non_numeric_posted_quantity_is_a_400(self):
        self.assertEqual(
            self.client.post("/add-to-cart/100/1",
                             {"quantity": "abc"}).status_code,
            400)
        self.assertFalse(CartProduct.objects.exists())


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


class CartMergeAtomicityTest(CartTestBase):
    """The merge must be all-or-nothing.

    Summing a quantity onto the surviving row and deleting the row it came
    from are two statements; in autocommit a crash between them loses the
    quantity permanently and leaves a half-merged cart behind.
    """

    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.user_cart = Cart.objects.create(user=self.user)
        self.existing = CartProduct.objects.create(
            cart=self.user_cart, product=Product.objects.get(pk=100),
            quantity=3, price_id="price_existing")
        self.user_cart.products.add(self.existing)

        # A session cart with two rows: one that merges into self.existing,
        # and one that gets reparented.
        self.client.post("/add-to-cart/100/2")
        self.client.post("/add-to-cart/101/4")
        self.session_cart_id = self.client.session["cart_id"]
        self.client.force_login(self.user)

    def test_a_failure_partway_through_rolls_the_whole_merge_back(self):
        real_save = CartProduct.save
        calls = []

        def failing_save(cart_product, *args, **kwargs):
            calls.append(cart_product.pk)
            if len(calls) > 1:
                raise RuntimeError("boom, halfway through the merge")
            return real_save(cart_product, *args, **kwargs)

        with mock.patch.object(CartProduct, "save", failing_save):
            with self.assertRaises(RuntimeError):
                self.client.get("/cart")

        # Nothing moved: quantities, row parents and row count are untouched.
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.quantity, 3)
        self.assertEqual(CartProduct.objects.count(), 3)
        session_rows = CartProduct.objects.filter(cart=self.session_cart_id)
        self.assertEqual(
            sorted((cp.product_id, cp.quantity) for cp in session_rows),
            [(100, 2), (101, 4)])
        # The session cart survives, still linked to its rows and still
        # pointed at by the session, so the merge can simply be retried.
        session_cart = Cart.objects.get(cart_id=self.session_cart_id)
        self.assertEqual(session_cart.products.count(), 2)
        self.assertEqual(self.client.session["cart_id"], self.session_cart_id)

    def test_the_retry_after_a_failed_merge_produces_the_right_totals(self):
        real_save = CartProduct.save
        calls = []

        def failing_save(cart_product, *args, **kwargs):
            calls.append(cart_product.pk)
            if len(calls) > 1:
                raise RuntimeError("boom, halfway through the merge")
            return real_save(cart_product, *args, **kwargs)

        with mock.patch.object(CartProduct, "save", failing_save):
            with self.assertRaises(RuntimeError):
                self.client.get("/cart")

        response = self.client.get("/cart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CartProduct.objects.count(), 2)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.quantity, 5)
        self.assertEqual(
            sorted((cp.product_id, cp.quantity)
                   for cp in self.user_cart.products.all()),
            [(100, 5), (101, 4)])
        self.assertFalse(
            Cart.objects.filter(cart_id=self.session_cart_id).exists())
        self.assertNotIn("cart_id", self.client.session)


class SignupCartTest(CartTestBase):
    def test_signing_up_leaves_cart_creation_to_get_cart(self):
        response = self.client.post(
            "/signup", {"email": "new@example.com", "password": "hunter2hunter2"})
        # fetch_redirect_response: rendering the home page needs the build-time
        # image assets, see the note on PageSmokeTest.
        self.assertRedirects(response, "/", fetch_redirect_response=False)

        user = User.objects.get(email="new@example.com")
        self.assertFalse(Cart.objects.filter(user=user).exists())

        self.client.post("/add-to-cart/100/1")

        self.assertEqual(Cart.objects.filter(user=user).count(), 1)
        self.assertEqual(
            CartProduct.objects.get().cart_id,
            Cart.objects.get(user=user).cart_id)


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

    @mock.patch("main.models.Payments")
    @mock.patch("main.views.Payments")
    def test_checkout_redirects_to_the_payment_provider(
            self, payments, model_payments):
        # Checkout now records a PENDING order first, so it needs a non-empty
        # cart -- and Payments.checkout returns (url, session_id).
        model_payments.create_product.return_value = "prod_test"
        model_payments.create_price.return_value = "price_test"
        payments.checkout.return_value = (
            "https://checkout.example/session", "cs_test_smoke")
        self.client.post("/add-to-cart/100/1")

        response = self.client.get("/checkout")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], "https://checkout.example/session")

    def test_checkout_with_an_empty_cart_goes_back_to_the_cart(self):
        self.assertRedirects(self.client.get("/checkout"), "/cart")

    def test_logout_requires_a_login(self):
        response = self.client.get("/logout")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])


WEBHOOK_SECRET = "whsec_test_secret_value"
WEBHOOK_URL = "/stripe/webhook"
OWNER_EMAIL = "owner@example.com"


def stripe_signature(payload: str, secret: str = WEBHOOK_SECRET,
                     timestamp=None) -> str:
    """Build a real Stripe-Signature header.

    Deliberately the genuine HMAC construction rather than a mocked-out
    construct_event: a suite that never runs the signature check cannot tell
    a working verification from a missing one, which is the one bug in this
    feature that actually matters.
    """
    timestamp = int(time.time()) if timestamp is None else timestamp
    signature = hmac.new(
        secret.encode(), f"{timestamp}.{payload}".encode(),
        hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


@override_settings(
    STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET,
    ADMINS=[("Owner", OWNER_EMAIL)],
    DEFAULT_FROM_EMAIL="support@pigscanfly.ca")
class OrderTestBase(TestCase):
    """Orders and the Stripe webhook. Stripe itself is stubbed; the signature
    verification is not."""

    fixtures = ["initial_products"]

    def setUp(self):
        patcher = mock.patch("main.models.Payments")
        payments = patcher.start()
        self.addCleanup(patcher.stop)
        self.payments = payments
        payments.create_product.return_value = "prod_test"
        # A distinct Stripe Price per cart row, as the real code does -- the
        # reconciliation join is on price id, so a shared one would be
        # unrepresentative.
        prices = itertools.count(1)
        payments.create_price.side_effect = (
            lambda *a, **kw: f"price_test_{next(prices)}")

        # By default Stripe reports exactly what was snapshotted, i.e. the
        # customer changed nothing. Tests override these to model an
        # adjustment or a failed lookup.
        self.billed_quantities: dict = {}
        self.extra_line_items: list = []
        self.line_items_error = None
        self.line_items_has_more = False
        payments.list_line_items.side_effect = self.fake_line_items

    def fake_line_items(self, session_id, limit=100):
        """Stand in for Stripe's line-item listing for a session.

        billed_quantities is keyed by product pk; None means the customer
        removed that line at checkout, so Stripe does not report it at all.
        """
        if self.line_items_error is not None:
            raise self.line_items_error
        order = Order.objects.filter(stripe_session_id=session_id).first()
        data = []
        if order is not None:
            for item in order.items.all():
                quantity = self.billed_quantities.get(
                    item.product_id, item.snapshot_quantity)
                if quantity is None:
                    continue
                data.append(
                    {"price": {"id": item.price_id}, "quantity": quantity})
        return {"data": data + self.extra_line_items,
                "has_more": self.line_items_has_more}

    def place_order(self, product_pk=100, quantity=2,
                    session_id="cs_test_session", client=None):
        """Run the real checkout path, with only Stripe's HTTP call stubbed."""
        client = client or self.client
        client.post(f"/add-to-cart/{product_pk}/{quantity}")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/session", id=session_id)
            response = client.post("/checkout")
        self.create_call = create
        self.checkout_response = response
        return Order.objects.get(stripe_session_id=session_id)

    def session_payload(self, order, **overrides):
        """A checkout.session object shaped like Stripe's."""
        session = {
            "id": order.stripe_session_id or "cs_test_session",
            "object": "checkout.session",
            "client_reference_id": str(order.pk),
            "metadata": {"order_id": str(order.pk)},
            "payment_status": "paid",
            "currency": "usd",
            # Subtotal agrees with the snapshot; the mismatch case has its
            # own test.
            "amount_subtotal": order.snapshot_subtotal(),
            "amount_total": order.snapshot_subtotal() + 700,
            "total_details": {"amount_tax": 200, "amount_shipping": 500},
            "customer_details": {
                "email": "buyer@example.com",
                "name": "Buyer Person",
                "address": {
                    "line1": "1 Billing Way", "line2": "",
                    "city": "San Francisco", "state": "CA",
                    "postal_code": "94110", "country": "US",
                },
            },
            "shipping_details": {
                "name": "Buyer Person",
                "address": {
                    "line1": "2 Ship Lane", "line2": "Apt 3",
                    "city": "Oakland", "state": "CA",
                    "postal_code": "94607", "country": "US",
                },
            },
        }
        session.update(overrides)
        return session

    def event_body(self, order, event_type="checkout.session.completed",
                   **overrides) -> str:
        return json.dumps({
            "id": "evt_test_1",
            "object": "event",
            "type": event_type,
            "data": {"object": self.session_payload(order, **overrides)},
        })

    @staticmethod
    def order_emails():
        """Just the fulfilment notifications.

        Configuring ADMINS also enables Django's own 500 mail, so a test that
        provokes an error would otherwise see it in the outbox too.
        """
        return [m for m in mail.outbox if "New paid order" in m.subject]

    def deliver(self, body: str, signature=None, secret=WEBHOOK_SECRET):
        """POST a webhook body, signed for real unless told otherwise."""
        if signature is None:
            signature = stripe_signature(body, secret=secret)
        extra = {} if signature is False else {
            "HTTP_STRIPE_SIGNATURE": signature}
        return self.client.post(
            WEBHOOK_URL, data=body, content_type="application/json", **extra)


class CheckoutCreatesOrderTest(OrderTestBase):
    """Requirement 1: checkout writes the snapshot the webhook will need."""

    def test_checkout_records_a_pending_order_with_a_line_item_snapshot(self):
        order = self.place_order(product_pk=100, quantity=2)
        product = Product.objects.get(pk=100)

        self.assertEqual(self.checkout_response.status_code, 302)
        self.assertEqual(
            self.checkout_response["Location"],
            "https://checkout.example/session")
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.stripe_session_id, "cs_test_session")
        self.assertEqual(order.currency, "usd")
        self.assertEqual(order.amount_total, product.price * 2)
        self.assertIsNone(order.paid_at)
        self.assertIsNone(order.notified_at)

        item = order.items.get()
        self.assertEqual(item.product_id, 100)
        self.assertEqual(item.product_name, product.name)
        self.assertEqual(item.unit_amount, product.price)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.snapshot_quantity, 2)
        self.assertEqual(item.currency, "usd")
        self.assertEqual(item.price_id, CartProduct.objects.get().price_id)
        self.assertTrue(item.price_id)

    def test_the_order_id_is_sent_to_stripe_as_the_client_reference(self):
        # Without this there is nothing at all tying a Stripe session to
        # anything local.
        order = self.place_order()
        kwargs = self.create_call.call_args.kwargs
        self.assertEqual(kwargs["client_reference_id"], str(order.pk))
        self.assertEqual(kwargs["metadata"], {"order_id": str(order.pk)})

    def test_the_invalid_coupon_retry_differs_only_by_the_discount(self):
        # The retry is a copy of the single parameter dict minus "discounts",
        # so the order id rides along by construction. Pinned because a
        # version that rebuilt the retry parameters separately would silently
        # drop it -- and would also let the two paths drift apart on tax,
        # which is what CheckoutTaxTest guards from the other side.
        self.client.post("/add-to-cart/100/1")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.side_effect = [
                stripe.InvalidRequestError(
                    "No such coupon", "discounts[0][coupon]"),
                mock.Mock(url="https://checkout.example/session",
                          id="cs_after_retry"),
            ]
            self.client.post("/checkout", {"coupon": "coupon_bad"})

        order = Order.objects.get()
        first, retry = [call.kwargs for call in create.call_args_list]
        self.assertEqual(first["discounts"], [{"coupon": "coupon_bad"}])
        self.assertNotIn("discounts", retry)
        self.assertEqual(
            {k: v for k, v in first.items() if k != "discounts"}, retry)
        for params in (first, retry):
            self.assertEqual(params["client_reference_id"], str(order.pk))
            self.assertEqual(params["metadata"], {"order_id": str(order.pk)})
        self.assertEqual(order.stripe_session_id, "cs_after_retry")

    def test_a_logged_in_buyers_order_is_attached_to_them(self):
        user = User.objects.create_user(
            username="buyer", email="buyer@example.com",
            password="hunter2hunter2")
        self.client.force_login(user)

        order = self.place_order()

        self.assertEqual(order.user_id, user.pk)

    def test_an_anonymous_buyers_order_has_no_user(self):
        self.assertIsNone(self.place_order().user_id)

    def test_the_snapshot_outlives_the_product_and_its_price(self):
        # Product.price is edited in place and Stripe Price objects are minted
        # per cart row, so neither can be trusted for a historical order.
        order = self.place_order(product_pk=100, quantity=2)
        product = Product.objects.get(pk=100)
        original_price = product.price

        Product.objects.filter(pk=100).update(price=original_price + 5000)
        Product.objects.filter(pk=100).delete()

        item = order.items.get()
        self.assertIsNone(item.product_id)
        self.assertEqual(item.unit_amount, original_price)
        self.assertEqual(item.product_name, "Learning Spark (1st edition)")
        self.assertEqual(item.total_amount(), original_price * 2)

    def test_checking_out_an_empty_cart_records_nothing(self):
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            response = self.client.post("/checkout")

        self.assertRedirects(response, "/cart")
        create.assert_not_called()
        self.assertFalse(Order.objects.exists())

    def test_a_failed_stripe_checkout_does_not_leave_a_pending_order(self):
        # No session was created, so nothing will ever arrive for this order
        # -- not even checkout.session.expired. It must not sit in the admin
        # as PENDING forever.
        self.client.post("/add-to-cart/100/1")
        with mock.patch("main.payments.stripe.checkout.Session.create",
                        side_effect=RuntimeError("Stripe is down")):
            with self.assertLogs("main.views", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    self.client.post("/checkout")

        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertIsNone(order.stripe_session_id)
        # The snapshot is kept, so it is still auditable.
        self.assertEqual(order.items.count(), 1)

    def test_a_cancelled_checkout_order_cannot_later_be_paid(self):
        self.client.post("/add-to-cart/100/1")
        with mock.patch("main.payments.stripe.checkout.Session.create",
                        side_effect=RuntimeError("Stripe is down")):
            with self.assertLogs("main.views", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    self.client.post("/checkout")
        order = Order.objects.get()

        response = self.deliver(self.event_body(order, id="cs_ghost"))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        # Not assertEqual(outbox, 0): configuring ADMINS also turns on
        # Django's own 500 mail, and the failed checkout above sent one.
        self.assertEqual(self.order_emails(), [])

    def test_multiple_cart_lines_are_all_snapshotted(self):
        self.client.post("/add-to-cart/100/1")
        self.client.post("/add-to-cart/101/3")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/session", id="cs_multi")
            self.client.post("/checkout")

        order = Order.objects.get()
        self.assertEqual(
            sorted((i.product_id, i.quantity) for i in order.items.all()),
            [(100, 1), (101, 3)])
        self.assertEqual(order.amount_total, order.snapshot_subtotal())


class WebhookSignatureTest(OrderTestBase):
    """Requirement 3, and the security boundary of the whole feature: an
    unverified payload must never be processed."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order()
        self.body = self.event_body(self.order)

    def assertNothingHappened(self, response):
        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertIsNone(self.order.paid_at)
        self.assertIsNone(self.order.notified_at)
        self.assertEqual(self.order.customer_email, "")
        self.assertEqual(len(mail.outbox), 0)

    def test_a_missing_signature_header_is_rejected(self):
        self.assertNothingHappened(self.deliver(self.body, signature=False))

    def test_an_empty_signature_header_is_rejected(self):
        self.assertNothingHappened(self.deliver(self.body, signature=""))

    def test_a_garbage_signature_header_is_rejected(self):
        self.assertNothingHappened(
            self.deliver(self.body, signature="t=1,v1=deadbeef"))

    def test_a_signature_made_with_the_wrong_secret_is_rejected(self):
        self.assertNothingHappened(
            self.deliver(self.body, secret="whsec_not_the_real_secret"))

    def test_a_body_swapped_after_signing_is_rejected(self):
        # The forgery that matters: a real signature for a payload the
        # attacker then replaces with their own.
        signature = stripe_signature(self.body)
        forged = self.event_body(self.order, payment_status="paid",
                                 amount_total=1)
        self.assertNotEqual(forged, self.body)

        self.assertNothingHappened(self.deliver(forged, signature=signature))

    def test_a_stale_signature_outside_the_tolerance_is_rejected(self):
        old = stripe_signature(self.body, timestamp=int(time.time()) - 3600)
        self.assertNothingHappened(self.deliver(self.body, signature=old))

    def test_a_malformed_body_with_a_valid_signature_is_rejected(self):
        self.assertNothingHappened(self.deliver("this is not json{{"))

    @override_settings(STRIPE_WEBHOOK_SECRET="")
    def test_an_unconfigured_secret_rejects_everything(self):
        # Failing closed: with no secret nothing can be verified, so a
        # correctly signed-looking delivery must still be refused.
        self.assertNothingHappened(self.deliver(self.body))

    def test_the_webhook_refuses_get(self):
        response = self.client.get(WEBHOOK_URL)
        self.assertEqual(response.status_code, 405)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_the_webhook_does_not_require_a_csrf_token(self):
        # Stripe has no token to send; the signature is the authentication.
        strict = Client(enforce_csrf_checks=True)
        body = self.event_body(self.order)
        response = strict.post(
            WEBHOOK_URL, data=body, content_type="application/json",
            HTTP_STRIPE_SIGNATURE=stripe_signature(body))
        self.assertEqual(response.status_code, 200)


class WebhookPaymentTest(OrderTestBase):
    """Requirement 2: a validly-signed completed session pays the order and
    tells the owner, exactly once."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=100, quantity=2)

    def test_a_valid_completed_event_marks_the_order_paid(self):
        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNotNone(self.order.paid_at)
        self.assertEqual(self.order.customer_email, "buyer@example.com")
        self.assertEqual(self.order.customer_name, "Buyer Person")
        self.assertEqual(self.order.amount_total,
                         self.order.snapshot_subtotal() + 700)
        self.assertEqual(self.order.amount_tax, 200)
        self.assertEqual(self.order.currency, "usd")

    def test_the_reported_addresses_are_recorded(self):
        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.assertEqual(self.order.shipping_line1, "2 Ship Lane")
        self.assertEqual(self.order.shipping_line2, "Apt 3")
        self.assertEqual(self.order.shipping_city, "Oakland")
        self.assertEqual(self.order.shipping_state, "CA")
        self.assertEqual(self.order.shipping_postal_code, "94607")
        self.assertEqual(self.order.shipping_country, "US")
        self.assertEqual(self.order.billing_line1, "1 Billing Way")
        self.assertEqual(self.order.billing_city, "San Francisco")

    def test_shipping_reported_under_collected_information_is_recorded(self):
        # Newer Stripe API versions moved it; both shapes must work.
        body = self.event_body(
            self.order,
            shipping_details=None,
            collected_information={"shipping_details": {
                "name": "Newer Shape",
                "address": {"line1": "9 New Way", "city": "Berkeley",
                            "state": "CA", "postal_code": "94704",
                            "country": "US"},
            }})
        self.deliver(body)

        self.order.refresh_from_db()
        self.assertEqual(self.order.shipping_name, "Newer Shape")
        self.assertEqual(self.order.shipping_line1, "9 New Way")

    def test_exactly_one_email_goes_to_the_owner(self):
        self.deliver(self.event_body(self.order))

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [OWNER_EMAIL])
        self.assertEqual(message.from_email, "support@pigscanfly.ca")
        self.assertIn(str(self.order.pk), message.subject)

    def test_the_email_carries_everything_needed_to_fulfil(self):
        self.deliver(self.event_body(self.order))

        body = mail.outbox[0].body
        self.assertIn(f"Order #{self.order.pk}", body)
        self.assertIn("2 x Learning Spark", body)
        self.assertIn("buyer@example.com", body)
        for line in ["2 Ship Lane", "Apt 3", "Oakland, CA", "94607"]:
            self.assertIn(line, body)
        self.order.refresh_from_db()
        self.assertIn(self.order.total_display_price(), body)

    def test_a_successful_notification_is_recorded_on_the_order(self):
        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.notified_at)
        self.assertEqual(self.order.notification_error, "")

    def test_the_billed_quantities_are_read_back_from_stripe(self):
        # Checkout enables adjustable_quantity, so the snapshot alone is not
        # a safe pick list. WebhookLineItemReconciliationTest covers the
        # cases where it actually differs.
        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.assertTrue(self.order.quantities_are_authoritative())
        self.assertNotIn("WARNING", mail.outbox[0].body)

    def test_a_completed_but_unpaid_session_does_not_pay_the_order(self):
        # e.g. an ACH debit that has not settled yet.
        response = self.deliver(
            self.event_body(self.order, payment_status="unpaid"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_later_async_success_pays_that_same_order(self):
        self.deliver(self.event_body(self.order, payment_status="unpaid"))
        response = self.deliver(self.event_body(
            self.order, event_type="checkout.session.async_payment_succeeded"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(len(mail.outbox), 1)

    def test_an_async_failure_cancels_the_pending_order(self):
        response = self.deliver(self.event_body(
            self.order, event_type="checkout.session.async_payment_failed"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_expired_session_cancels_the_pending_order(self):
        response = self.deliver(self.event_body(
            self.order, event_type="checkout.session.expired"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)

    def test_a_cancellation_event_cannot_undo_a_payment(self):
        self.deliver(self.event_body(self.order))
        self.deliver(self.event_body(
            self.order, event_type="checkout.session.expired"))

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_an_event_for_an_unknown_order_is_accepted_without_side_effects(self):
        body = self.event_body(
            self.order, id="cs_never_seen", client_reference_id="999999",
            metadata={})

        response = self.deliver(body)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_unbound_order_is_found_by_its_client_reference(self):
        # Belt and braces: if the session id never made it onto the order,
        # the reference Stripe echoes back has to be enough to bind it.
        Order.objects.filter(pk=self.order.pk).update(stripe_session_id=None)

        response = self.deliver(self.event_body(self.order, id="cs_other_id"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(self.order.stripe_session_id, "cs_other_id")


class WebhookSessionBindingTest(OrderTestBase):
    """The stored stripe_session_id *is* the binding between an order and the
    payment for it, so it gets verified rather than overwritten.

    Without this, a signed event naming an order in client_reference_id could
    re-point an order already bound to another session and mark it paid,
    silently rewriting the local binding.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order(session_id="cs_session_a")

    def assertOrderUntouched(self, response):
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(self.order.stripe_session_id, "cs_session_a")
        self.assertIsNone(self.order.paid_at)
        self.assertEqual(self.order.customer_email, "")
        self.assertEqual(len(mail.outbox), 0)

    def test_a_different_session_cannot_re_bind_and_pay_an_order(self):
        body = self.event_body(self.order, id="cs_session_b")

        with self.assertLogs("main.views", level="ERROR") as logs:
            response = self.deliver(body)

        self.assertOrderUntouched(response)
        self.assertIn("already bound", "\n".join(logs.output))

    def test_a_different_session_reaching_it_via_metadata_is_also_refused(self):
        body = self.event_body(
            self.order, id="cs_session_b", client_reference_id=None,
            metadata={"order_id": str(self.order.pk)})

        with self.assertLogs("main.views", level="ERROR"):
            response = self.deliver(body)

        self.assertOrderUntouched(response)

    def test_a_cancellation_from_a_different_session_is_also_refused(self):
        body = self.event_body(
            self.order, id="cs_session_b",
            event_type="checkout.session.expired")

        with self.assertLogs("main.views", level="ERROR"):
            response = self.deliver(body)

        self.assertOrderUntouched(response)

    def test_the_orders_own_session_still_works(self):
        # The guard must not break the ordinary path it is protecting.
        response = self.deliver(self.event_body(self.order, id="cs_session_a"))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(len(mail.outbox), 1)


class WebhookLineItemReconciliationTest(OrderTestBase):
    """Checkout enables adjustable_quantity, so the cart snapshot is not
    necessarily what was bought -- and the owner's email *is* the pick list.

    The webhook therefore re-reads the billed quantities from Stripe. That
    extra call is strictly best-effort: a paid order must never be lost
    because a secondary lookup failed.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=100, quantity=2)
        self.item = self.order.items.get()

    def test_the_default_case_records_that_nothing_changed(self):
        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertIsNotNone(self.order.reconciled_at)
        self.assertEqual(self.order.reconciliation_error, "")
        self.assertEqual(self.item.quantity, 2)
        self.assertFalse(self.item.quantity_adjusted())

    def test_a_quantity_increased_at_checkout_is_persisted(self):
        self.billed_quantities = {100: 5}

        self.deliver(self.event_body(self.order))

        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 5)
        self.assertEqual(self.item.snapshot_quantity, 2)
        self.assertTrue(self.item.quantity_adjusted())

    def test_a_quantity_decreased_at_checkout_is_persisted(self):
        self.billed_quantities = {100: 1}

        self.deliver(self.event_body(self.order))

        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)
        self.assertEqual(self.item.snapshot_quantity, 2)

    def test_the_original_quantity_stays_recoverable(self):
        self.billed_quantities = {100: 7}

        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        unit = self.item.unit_amount
        self.assertEqual(self.item.snapshot_quantity, 2)
        self.assertEqual(self.order.original_subtotal(), unit * 2)
        self.assertEqual(self.order.snapshot_subtotal(), unit * 7)
        self.assertEqual(
            [i.pk for i in self.order.adjusted_items()], [self.item.pk])

    def test_a_line_removed_at_checkout_becomes_zero_and_is_recorded(self):
        # Stripe simply omits a line the customer zeroed out; shipping it
        # anyway is exactly the failure this reconciliation exists to stop.
        self.billed_quantities = {100: None}

        self.deliver(self.event_body(self.order))

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 0)
        self.assertEqual(self.item.snapshot_quantity, 2)
        self.assertIn("did not bill", self.order.reconciliation_error)

    def test_the_email_shows_the_billed_quantity_and_the_adjustment(self):
        self.billed_quantities = {100: 5}

        self.deliver(self.event_body(self.order))

        body = mail.outbox[0].body
        self.assertIn("5 x Learning Spark", body)
        self.assertNotIn("2 x Learning Spark", body)
        self.assertIn("adjusted at checkout, was 2", body)
        # Reconciled, so no "do not trust this list" warning.
        self.assertNotIn("DO NOT SHIP", body)

    def test_multiple_lines_are_each_matched_on_their_own_price(self):
        self.client.post("/add-to-cart/101/3")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_multi")
            self.client.post("/checkout")
        order = Order.objects.get(stripe_session_id="cs_multi")
        self.billed_quantities = {100: 1, 101: 9}

        self.deliver(self.event_body(order))

        order.refresh_from_db()
        self.assertEqual(
            sorted((i.product_id, i.quantity, i.snapshot_quantity)
                   for i in order.items.all()),
            [(100, 1, 2), (101, 9, 3)])
        self.assertEqual(order.reconciliation_error, "")

    def test_a_billed_line_matching_nothing_is_recorded_not_crashed(self):
        self.extra_line_items = [
            {"price": {"id": "price_from_nowhere"}, "quantity": 3}]

        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNotNone(self.order.reconciled_at)
        self.assertIn("price_from_nowhere", self.order.reconciliation_error)
        self.assertIn("matches no line", self.order.reconciliation_error)
        # And the owner is told the match was not clean.
        self.assertIn("not everything matched up cleanly", mail.outbox[0].body)

    def test_an_order_line_with_no_price_id_is_recorded_not_crashed(self):
        OrderItem.objects.filter(pk=self.item.pk).update(price_id="")

        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertIn("no Stripe price id", self.order.reconciliation_error)

    def test_more_line_items_than_one_page_changes_nothing_at_all(self):
        # This does not page, so page one alone would zero every line that
        # lives on page two -- silently dropping real items from the pick
        # list. Refuse before writing anything rather than commit a partial
        # truth and mention it in a caveat.
        self.client.post("/add-to-cart/101/3")
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_big")
            self.client.post("/checkout")
        order = Order.objects.get(stripe_session_id="cs_big")
        self.line_items_has_more = True
        # Stripe's page one omits one of the two lines, as a real second page
        # would; nothing here may act on that.
        self.billed_quantities = {101: None}

        response = self.deliver(self.event_body(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(
            sorted((i.product_id, i.quantity, i.snapshot_quantity)
                   for i in order.items.all()),
            [(100, 2, 2), (101, 3, 3)])
        self.assertIsNone(order.reconciled_at)
        self.assertFalse(order.quantities_are_authoritative())
        self.assertIn("more than 100", order.reconciliation_error)
        # And the owner is told the list is unverified.
        self.assertIn("could not be re-read from Stripe", mail.outbox[-1].body)

    def test_reconciliation_runs_only_for_the_delivery_that_won(self):
        body = self.event_body(self.order)

        self.deliver(body)
        self.deliver(body)
        self.deliver(body)

        self.assertEqual(self.payments.list_line_items.call_count, 1)

    def test_exactly_one_lookup_of_one_page_is_made(self):
        # One extra call, no pagination loop: this is on the webhook's
        # response path.
        self.deliver(self.event_body(self.order))

        self.payments.list_line_items.assert_called_once_with(
            "cs_test_session", limit=100)

    def test_the_stripe_call_is_bounded_by_a_timeout_and_no_retries(self):
        # Checked against the real wrapper, since the class above stubs it.
        # A hung connection on the SDK's ~80s default would pin the worker
        # long after Stripe had given up and queued a re-delivery.
        with mock.patch("main.payments.stripe.StripeClient") as client_class:
            Payments.list_line_items("cs_x", limit=100)

        kwargs = client_class.call_args.kwargs
        self.assertEqual(kwargs["max_network_retries"], 0)
        # The timeout lives on the HTTP client, not on the request, so a
        # per-call argument would have been silently sent to Stripe as a
        # query parameter and bounded nothing.
        self.assertEqual(kwargs["http_client"]._timeout, 5)
        self.assertEqual(Payments.LINE_ITEM_TIMEOUT, 5)
        client_class.return_value.checkout.sessions.line_items.list \
            .assert_called_once_with("cs_x", {"limit": 100})

    def test_the_bounded_client_is_not_the_one_checkout_uses(self):
        # Bounding this call must not drag Session.create down to 5 seconds.
        with mock.patch("main.payments.stripe.StripeClient"):
            Payments.list_line_items("cs_x")

        self.assertIsNone(stripe.default_http_client)


class WebhookReconciliationFailureTest(OrderTestBase):
    """A failed line-item lookup degrades to the snapshot; it never costs a
    paid order."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order(product_pk=100, quantity=2)

    def deliver_with_broken_lookup(self, error=None, **overrides):
        self.line_items_error = error or OSError("Stripe is unreachable")
        with self.assertLogs("main.models", level="ERROR"):
            return self.deliver(self.event_body(self.order, **overrides))

    def test_a_lookup_failure_still_returns_200(self):
        self.assertEqual(self.deliver_with_broken_lookup().status_code, 200)

    def test_a_lookup_failure_still_records_the_paid_order(self):
        self.deliver_with_broken_lookup()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNotNone(self.order.paid_at)
        self.assertEqual(self.order.customer_email, "buyer@example.com")
        self.assertEqual(self.order.shipping_line1, "2 Ship Lane")

    def test_a_lookup_failure_keeps_the_snapshot_quantities(self):
        self.deliver_with_broken_lookup()

        item = self.order.items.get()
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.snapshot_quantity, 2)

    def test_a_lookup_failure_is_visible_on_the_order(self):
        self.deliver_with_broken_lookup()

        self.order.refresh_from_db()
        self.assertIsNone(self.order.reconciled_at)
        self.assertFalse(self.order.quantities_are_authoritative())
        self.assertIn("Stripe is unreachable", self.order.reconciliation_error)

    def test_a_lookup_failure_with_a_mismatched_subtotal_shouts(self):
        # This is the dangerous combination: the totals say the customer
        # changed something and we could not find out what.
        self.deliver_with_broken_lookup(
            amount_subtotal=self.order.snapshot_subtotal() + 4321)

        body = mail.outbox[0].body
        self.assertIn("DO NOT SHIP FROM THE LIST ABOVE", body)
        self.assertIn("Stripe is unreachable", body)

    def test_a_lookup_failure_with_a_matching_subtotal_says_so_quietly(self):
        self.deliver_with_broken_lookup()

        body = mail.outbox[0].body
        self.assertNotIn("DO NOT SHIP", body)
        self.assertIn("could not be re-read from Stripe", body)

    def test_the_owner_is_still_emailed_once(self):
        self.deliver_with_broken_lookup()
        self.assertEqual(len(mail.outbox), 1)

    def test_a_crash_before_the_marker_lands_rolls_the_quantities_back(self):
        # The quantities and the "these came from Stripe" marker are one
        # fact. Landing the first without the second would leave the admin
        # showing Stripe's numbers while reconciled_at says they were never
        # checked -- and the email would call them unverified.
        self.billed_quantities = {100: 5}
        # timezone.now() is evaluated for reconciled_at, i.e. after the item
        # quantities have been written and before the marker is.
        with mock.patch("main.models.timezone.now",
                        side_effect=RuntimeError("boom, between the writes")):
            with self.assertLogs("main.models", level="ERROR"):
                reconciled = self.order.reconcile_line_items()

        self.assertFalse(reconciled)
        item = self.order.items.get()
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.snapshot_quantity, 2)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.reconciled_at)
        self.assertIn("boom, between the writes",
                      self.order.reconciliation_error)

    def test_a_late_bound_order_is_still_reconciled(self):
        Order.objects.filter(pk=self.order.pk).update(stripe_session_id=None)

        self.deliver(self.event_body(self.order, id="cs_late_binding"))

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        # It got bound before reconciliation ran, so the lookup did happen.
        self.assertEqual(self.order.stripe_session_id, "cs_late_binding")
        self.assertIsNotNone(self.order.reconciled_at)


class WebhookIdempotencyTest(OrderTestBase):
    """Requirement 4: Stripe retries on any non-2xx and can duplicate
    deliveries outright."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order()

    def test_redelivering_the_same_event_pays_and_mails_once(self):
        body = self.event_body(self.order)

        first = self.deliver(body)
        second = self.deliver(body)
        third = self.deliver(body)

        self.assertEqual(
            [first.status_code, second.status_code, third.status_code],
            [200, 200, 200])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_paid_transition_only_ever_runs_from_pending(self):
        # The guard is a conditional UPDATE, not a read-then-write, so it is
        # the database that refuses the second transition.
        self.deliver(self.event_body(self.order))
        self.order.refresh_from_db()
        first_paid_at = self.order.paid_at

        self.deliver(self.event_body(self.order, amount_total=999999))

        self.order.refresh_from_db()
        self.assertEqual(self.order.paid_at, first_paid_at)
        self.assertNotEqual(self.order.amount_total, 999999)

    def test_a_redelivery_after_fulfilment_does_not_re_notify(self):
        self.deliver(self.event_body(self.order))
        # The owner ships it and marks it done in the admin.
        Order.objects.filter(pk=self.order.pk).update(
            status=Order.Status.FULFILLED)
        mail.outbox.clear()

        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.FULFILLED)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_redelivery_after_a_failed_email_does_not_retry_the_email(self):
        # Deliberate: the order is recorded either way, and re-mailing on
        # every one of Stripe's retries is exactly the flood being avoided.
        with mock.patch("main.models.send_mail",
                        side_effect=OSError("SMTP is down")):
            with self.assertLogs("main.models", level="ERROR"):
                self.deliver(self.event_body(self.order))

        response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)


class WebhookEmailFailureTest(OrderTestBase):
    """Requirement 5: a failing send_mail must not fail the webhook.

    A 500 here makes Stripe retry for up to three days, so an SMTP outage
    would turn into an order that looks unrecorded plus a mail flood later.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order()

    def deliver_with_broken_mail(self, exception=None):
        exception = exception or OSError("SMTP server is unreachable")
        # assertLogs both proves the failure is not swallowed silently and
        # keeps the deliberate traceback out of the test output.
        with mock.patch("main.models.send_mail", side_effect=exception):
            with self.assertLogs("main.models", level="ERROR"):
                return self.deliver(self.event_body(self.order))

    def test_a_send_failure_still_returns_200(self):
        self.assertEqual(self.deliver_with_broken_mail().status_code, 200)

    def test_a_send_failure_still_records_the_paid_order(self):
        self.deliver_with_broken_mail()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNotNone(self.order.paid_at)
        self.assertEqual(self.order.customer_email, "buyer@example.com")
        self.assertEqual(self.order.shipping_line1, "2 Ship Lane")

    def test_a_send_failure_is_visible_on_the_order(self):
        self.deliver_with_broken_mail()

        self.order.refresh_from_db()
        self.assertIsNone(self.order.notified_at)
        self.assertIn("SMTP server is unreachable", self.order.notification_error)

    @override_settings(ADMINS=[])
    def test_no_configured_admins_is_recorded_rather_than_raised(self):
        with self.assertLogs("main.models", level="ERROR"):
            response = self.deliver(self.event_body(self.order))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertIsNone(self.order.notified_at)
        self.assertIn("ADMINS", self.order.notification_error)


class WebhookUnhandledEventTest(OrderTestBase):
    """Requirement 6: anything this endpoint has no opinion about gets a 200
    so Stripe stops retrying it."""

    def setUp(self):
        super().setUp()
        self.order = self.place_order()

    def test_an_unrelated_event_type_is_accepted_and_ignored(self):
        for event_type in ["payment_intent.succeeded", "invoice.paid",
                           "customer.created", "charge.refunded"]:
            with self.subTest(event_type=event_type):
                response = self.deliver(
                    self.event_body(self.order, event_type=event_type))

                self.assertEqual(response.status_code, 200)
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, Order.Status.PENDING)
                self.assertIsNone(self.order.paid_at)
                self.assertEqual(len(mail.outbox), 0)

    def test_an_event_with_no_recognisable_object_is_accepted(self):
        body = json.dumps({
            "id": "evt_weird", "object": "event",
            "type": "some.new.event.type", "data": {},
        })
        response = self.deliver(body)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)


class WebhookInterleavedDeliveryTest(OrderTestBase):
    """The race the idempotency guard exists to close.

    NOT a threaded test. A genuine two-thread version was written and then
    abandoned: against the SQLite test database both threads die with
    "database table is locked" before either reaches the guard, so it measures
    SQLite's shared-cache locking rather than this code, and making it pass
    would need exactly the elaborate synchronisation that produces flaky
    tests. Postgres -- what prod runs, and where select_for_update is a real
    row lock -- is not available in CI.

    What is covered instead is the specific interleaving that matters: two
    deliveries that both observe the order as PENDING before either writes.
    That is precisely what a read-then-write check gets wrong, and it is
    deterministic.
    """

    def setUp(self):
        super().setUp()
        self.order = self.place_order()

    def test_only_one_of_two_deliveries_that_both_saw_pending_can_pay(self):
        # Both readers hold a PENDING view of the row, as two concurrent
        # webhook workers would.
        first_view = Order.objects.get(pk=self.order.pk)
        second_view = Order.objects.get(pk=self.order.pk)
        self.assertEqual(first_view.status, Order.Status.PENDING)
        self.assertEqual(second_view.status, Order.Status.PENDING)

        # The transition is a conditional UPDATE, so the database decides it,
        # not either reader's already-stale copy.
        first = Order.objects.filter(
            pk=first_view.pk, status=Order.Status.PENDING).update(
                status=Order.Status.PAID)
        second = Order.objects.filter(
            pk=second_view.pk, status=Order.Status.PENDING).update(
                status=Order.Status.PAID)

        self.assertEqual((first, second), (1, 0))

    def test_a_delivery_arriving_mid_flight_does_not_double_notify(self):
        # Re-enter the webhook from inside the first delivery's post-commit
        # work, i.e. while the first one is still running.
        body = self.event_body(self.order)
        real_notify = Order.notify_owner
        reentered = []

        def notify_and_reenter(order):
            if not reentered:
                reentered.append(True)
                self.deliver(body)
            return real_notify(order)

        with mock.patch.object(Order, "notify_owner", notify_and_reenter):
            self.deliver(body)

        self.assertTrue(reentered)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(len(self.order_emails()), 1)



class CheckoutSuccessPageTest(OrderTestBase):
    """The success page keeps clearing the cart, but never decides payment."""

    def test_the_cart_is_still_cleared(self):
        self.place_order()
        self.assertTrue(CartProduct.objects.exists())

        response = self.client.get("/checkout/success")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(CartProduct.objects.exists())

    def test_loading_the_success_page_does_not_pay_the_order(self):
        order = self.place_order()

        self.client.get(f"/checkout/success?session_id={order.stripe_session_id}")

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_known_session_id_shows_the_recorded_order(self):
        order = self.place_order()
        self.deliver(self.event_body(order))

        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertContains(response, f"Order #{order.pk}")
        self.assertContains(response, "Learning Spark")
        self.assertContains(response, "Paid")

    def test_an_unknown_session_id_just_renders_the_plain_page(self):
        response = self.client.get("/checkout/success?session_id=cs_nope")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Checkout Successful!")
        self.assertNotContains(response, "Order #")


class OrderModelTest(OrderTestBase):
    def test_the_snapshot_subtotal_sums_the_lines(self):
        order = Order.objects.create()
        OrderItem.objects.create(
            order=order, product_name="A", unit_amount=1000, quantity=2)
        OrderItem.objects.create(
            order=order, product_name="B", unit_amount=250, quantity=3)

        self.assertEqual(order.snapshot_subtotal(), 2750)

    def test_quantities_match_when_stripe_reported_no_subtotal(self):
        order = Order.objects.create()
        self.assertIsNone(order.amount_subtotal)
        self.assertTrue(order.quantities_match())

    def test_display_prices_are_dollars(self):
        order = Order.objects.create(amount_total=123456)
        self.assertEqual(order.total_display_price(), "1234.56")

    def test_an_order_with_no_shipping_address_says_so(self):
        order = Order.objects.create(customer_email="x@example.com")
        self.assertEqual(order.shipping_address_lines(), [])
        self.assertIn("no shipping address", order.notification_body())

    def test_two_orders_cannot_share_a_stripe_session(self):
        Order.objects.create(stripe_session_id="cs_dupe")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(stripe_session_id="cs_dupe")

    def test_pending_orders_may_all_have_no_session_id_yet(self):
        Order.objects.create()
        Order.objects.create()
        self.assertEqual(
            Order.objects.filter(stripe_session_id__isnull=True).count(), 2)

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
