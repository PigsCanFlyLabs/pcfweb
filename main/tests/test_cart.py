"""Tests for the cart and the cart views."""

import re
from unittest import mock

from django.contrib import messages
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.utils.cache import add_never_cache_headers

from main.models import Cart, CartProduct, Product
from main.tests.base import (
    assert_never_cache_response, cache_control_directives, CartTestBase)
from main.views import AddToCartView, CartView


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


class CartMessageLayoutTest(CartTestBase):
    def test_cart_messages_render_in_the_global_header_clearance_region(self):
        request = RequestFactory().get("/cart")
        request.user = AnonymousUser()
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        messages.warning(request, "Checkout feedback")

        response = CartView.as_view()(request)
        html = response.content.decode()

        self.assertIn('class="site-messages"', html)
        self.assertIn("data-site-messages", html)
        self.assertLess(
            html.index('class="site-messages"'),
            html.index('<div class="cart">'))


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


class CartCacheControlTest(CartTestBase):
    """The cart is per-session state and must never be shared by a cache."""

    def test_the_cart_page_is_marked_uncacheable(self):
        self.client.post("/add-to-cart/106/1", {"chosen_amount": "5.00"})

        response = self.client.get("/cart")

        self.assertEqual(response.status_code, 200)
        assert_never_cache_response(self, response)

    def test_the_cache_assertion_rejects_shared_cache_directives(self):
        response = HttpResponse()
        add_never_cache_headers(response)
        response["Cache-Control"] += ", public, s-maxage=3600"

        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertIn("public", cache_control_directives(response))
        self.assertTrue(
            any(directive.startswith("s-maxage=")
                for directive in cache_control_directives(response)))
        with self.assertRaises(AssertionError):
            assert_never_cache_response(self, response)


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
        response = self.client.post("/add-to-cart/100/1", {"quantity": "0"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, b"Quantity must be at least 1.")
        self.assertFalse(CartProduct.objects.exists())

    def test_a_quantity_too_big_for_the_column_is_a_400_not_a_500(self):
        # Python ints are unbounded; BIGINT is not, so an oversized quantity
        # used to parse fine and then blow up at write time.
        too_big = AddToCartView.MAX_QUANTITY + 1

        posted = self.client.post("/add-to-cart/100/1", {"quantity": str(too_big)})
        self.assertEqual(posted.status_code, 400)
        self.assertEqual(
            posted.content,
            f"Quantity must be at most {AddToCartView.MAX_QUANTITY}.".encode(),
        )

        from_url = self.client.post(f"/add-to-cart/100/{too_big}")
        self.assertEqual(from_url.status_code, 400)
        self.assertEqual(
            from_url.content,
            f"Quantity must be at most {AddToCartView.MAX_QUANTITY}.".encode(),
        )

        self.assertFalse(CartProduct.objects.exists())

    def test_the_largest_storable_quantity_still_works(self):
        # The bound is the column's capacity, so its exact value is valid.
        response = self.client.post(
            "/add-to-cart/100/1", {"quantity": str(AddToCartView.MAX_QUANTITY)})

        self.assertRedirects(response, "/cart")
        self.assertEqual(
            CartProduct.objects.get().quantity, AddToCartView.MAX_QUANTITY)

    def test_adding_to_a_line_cannot_overflow_the_quantity_column(self):
        self.client.post(
            "/add-to-cart/100/1",
            {"quantity": str(AddToCartView.MAX_QUANTITY)},
        )

        response = self.client.post("/add-to-cart/100/1")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.content,
            (f"Combined quantity must be at most "
             f"{AddToCartView.MAX_QUANTITY}.").encode(),
        )
        self.assertEqual(
            CartProduct.objects.get().quantity, AddToCartView.MAX_QUANTITY)

    def test_a_non_numeric_posted_quantity_is_a_400(self):
        response = self.client.post("/add-to-cart/100/1", {"quantity": "abc"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, b"Quantity must be a number.")
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

    def test_login_merge_cannot_overflow_the_quantity_column(self):
        user = self.make_user()
        user_cart = Cart.objects.create(user=user)
        product = Product.objects.get(pk=100)
        existing = CartProduct.objects.create(
            cart=user_cart, product=product,
            quantity=AddToCartView.MAX_QUANTITY,
            price_id="price_existing")
        # Build the FK-only shape the admin can create: this is the one place
        # the overflow branch must repair the M2M link instead of assuming it.

        self.client.post("/add-to-cart/100/1")
        session_cart_id = self.client.session["cart_id"]
        session_line = CartProduct.objects.get(cart_id=session_cart_id)
        self.client.force_login(user)

        response = self.client.get("/cart")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            (f"We capped {product.name} at {AddToCartView.MAX_QUANTITY} "
             "because the combined cart quantity would not fit in storage."),
        )
        existing.refresh_from_db()
        self.assertEqual(existing.quantity, AddToCartView.MAX_QUANTITY)
        self.assertEqual(list(user_cart.products.all()), [existing])
        self.assertFalse(
            CartProduct.objects.filter(pk=session_line.pk).exists())
        self.assertFalse(Cart.objects.filter(pk=session_cart_id).exists())
        self.assertNotIn("cart_id", self.client.session)

        Product.objects.filter(pk=product.pk).update(price=0)
        with mock.patch("main.views.Payments.checkout") as checkout:
            checkout.return_value = (
                "https://checkout.example/session", "cs_merge_recovered")
            checkout_response = self.client.post("/checkout")

        self.assertRedirects(
            checkout_response, "https://checkout.example/session",
            fetch_redirect_response=False)

    def test_login_merge_recovers_and_still_merges_innocent_products(self):
        user = self.make_user()
        user_cart = Cart.objects.create(user=user)
        existing = CartProduct.objects.create(
            cart=user_cart, product=Product.objects.get(pk=100),
            quantity=AddToCartView.MAX_QUANTITY, price_id="price_existing")
        user_cart.products.add(existing)

        self.client.post("/add-to-cart/100/1")
        self.client.post("/add-to-cart/101/4")
        session_cart_id = self.client.session["cart_id"]
        self.client.force_login(user)

        response = self.client.get("/cart")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            (f"We capped {existing.product.name} at "
             f"{AddToCartView.MAX_QUANTITY} "
             "because the combined cart quantity would not fit in storage."),
        )
        self.assertEqual(
            sorted((cp.product_id, cp.quantity)
                   for cp in user_cart.products.order_by("product_id")),
            [(100, AddToCartView.MAX_QUANTITY), (101, 4)],
        )
        self.assertFalse(Cart.objects.filter(pk=session_cart_id).exists())
        self.assertNotIn("cart_id", self.client.session)

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
        response = self.client.post("/add-to-cart/100/0")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, b"Quantity must be at least 1.")
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


class CartNotSoldHereTest(CartTestBase):
    """A cart line for a product we do not sell must not quote a price.

    Public add-to-cart refuses a noorder product and checkout re-checks, so
    one is only ever here because it was added before the flag was set -- or
    because an admin flagged a product that was already in someone's cart.
    Either way it cannot be bought, so pricing it in the table or in the total
    quotes a number the customer is never charged.
    """

    def cart_with(self, product, quantity=1):
        client = Client()
        client.post("/add-to-cart/100/1")
        cart = Cart.objects.get()
        CartProduct.objects.create(
            cart=cart, product=product, quantity=quantity)
        cart.products.add(CartProduct.objects.get(product=product))
        return client

    def test_the_line_shows_no_price_for_a_noorder_product(self):
        book = Product.objects.get(pk=107)
        client = self.cart_with(book)

        html = client.get("/cart").content.decode()

        self.assertIn("Fast Data Processing with Spark", html)
        self.assertIn("Not sold here", html)
        # Anti-vacuity: the sellable line beside it still shows its price.
        self.assertIn("Learning Spark", html)
        self.assertIn("39.99", html)

    def test_a_noorder_line_is_left_out_of_the_cart_total(self):
        """The case that actually costs money: a priced product flagged later.

        pk 107 is priced 0, so it could not demonstrate this on its own. An
        admin toggling noorder on a product already in a cart is the real
        path, and there the old code left its price in the sum.
        """
        book = Product.objects.get(pk=101)   # 49.99, sellable
        Product.objects.filter(pk=101).update(noorder=True)
        book.refresh_from_db()
        client = self.cart_with(book, quantity=2)

        html = client.get("/cart").content.decode()

        # Only Learning Spark's 39.99 counts; 2 x 49.99 does not.
        self.assertIn("<span>39.99</span>", html)
        self.assertNotIn("<span>139.97</span>", html)
        self.assertNotIn("99.98", html)

    def test_the_cart_says_why_the_line_cannot_be_bought(self):
        client = self.cart_with(Product.objects.get(pk=107))

        html = client.get("/cart").content.decode()

        self.assertIn("unavailable-notice", html)
        self.assertIn("checkout will not accept it", html)

    def test_a_cart_without_one_shows_no_notice(self):
        """Control: the notice is scoped, not always on."""
        client = Client()
        client.post("/add-to-cart/100/1")

        html = client.get("/cart").content.decode()

        self.assertNotIn("unavailable-notice", html)
        self.assertNotIn("Not sold here", html)
        self.assertIn("39.99", html)

    def test_a_mixed_pwyw_cart_is_no_longer_blocked(self):
        # Was blocked because Stripe refuses a second line item beside a
        # custom_unit_amount price. The e-book carries an ordinary fixed price
        # now, so the cart checks out and the coupon box comes back with it.
        client = Client()
        cart = Cart.objects.create()
        session = client.session
        session["cart_id"] = cart.cart_id
        session.save()
        fixed = Product.objects.get(pk=104)
        pwyw = Product.objects.get(pk=106)
        first = CartProduct.objects.create(cart=cart, product=fixed, quantity=1)
        second = CartProduct.objects.create(cart=cart, product=pwyw, quantity=1)
        cart.products.add(first, second)

        html = client.get("/cart").content.decode()

        self.assertNotIn("pwyw-checkout-blocker", html)
        self.assertNotIn("only line in its checkout", html)
        self.assertIn('name="coupon"', html)
        # Still both in the cart, and still named.
        self.assertIn("Distributed Computing 4 Kids", html)

    def test_a_single_pwyw_line_still_explains_itself(self):
        # The notice stays -- it is what tells a buyer the amount is theirs to
        # set. Only the sentence about Stripe's rules went.
        client = Client()
        client.post("/add-to-cart/106/1")

        html = client.get("/cart").content.decode()

        self.assertIn("pwyw-notice", html)
        self.assertNotIn("pwyw-checkout-blocker", html)
        self.assertIn('name="coupon"', html)

    def test_a_pwyw_item_can_join_a_non_empty_cart(self):
        self.client.post("/add-to-cart/104/1")

        response = self.client.post("/add-to-cart/106/1", follow=True)

        self.assertRedirects(response, "/cart")
        self.assertNotContains(response, "only line in its checkout")
        self.assertEqual(CartProduct.objects.filter(product_id=106).count(), 1)
        self.assertEqual(CartProduct.objects.filter(product_id=104).count(), 1)

    def test_anything_can_join_a_cart_that_already_holds_pwyw(self):
        self.client.post("/add-to-cart/106/1")

        response = self.client.post("/add-to-cart/104/1", follow=True)

        self.assertRedirects(response, "/cart")
        self.assertNotContains(response, "only line in its checkout")
        self.assertEqual(CartProduct.objects.filter(product_id=104).count(), 1)
        self.assertEqual(CartProduct.objects.filter(product_id=106).count(), 1)

    def test_the_same_pwyw_item_can_be_added_twice(self):
        # Was refused because Stripe requires quantity 1 on a
        # custom_unit_amount line. Adding again now adds to the quantity, the
        # same as any other product.
        self.client.post("/add-to-cart/106/1")

        response = self.client.post("/add-to-cart/106/1", follow=True)

        self.assertRedirects(response, "/cart")
        self.assertNotContains(response, "checked out one at a time")
        self.assertEqual(CartProduct.objects.get(product_id=106).quantity, 2)

    def test_a_noorder_line_does_not_trigger_the_shipping_notice(self):
        """It is not being shipped, so a delivery warning is noise."""
        book = Product.objects.get(pk=107)
        CartProduct.objects.all().delete()
        client = Client()
        client.get("/cart")
        cart = Cart.objects.create()
        session = client.session
        session["cart_id"] = cart.cart_id
        session.save()
        cart_product = CartProduct.objects.create(
            cart=cart, product=book, quantity=1)
        cart.products.add(cart_product)

        html = client.get("/cart").content.decode()

        self.assertIn("Not sold here", html)
        self.assertNotIn("shipping-notice", html)


@override_settings(THUMBNAIL_DEBUG=False, FREE_SHIPPING_ENABLED=True,
                   FREE_SHIPPING_THRESHOLD=4000)
class CartFreeShippingNoticeTest(CartTestBase):
    """What the cart page says about the free shipping offer.

    The page and Payments.checkout read one method, so these are the other
    half of test_shipping.FreeShippingTest: that one pins what the session
    carries, this one pins what the buyer was told before they got there.
    """

    def add(self, pk, quantity=1):
        Product.objects.filter(pk=pk).update(stock=99)
        response = self.client.post(f"/add-to-cart/{pk}/{quantity}")
        self.assertIn(response.status_code, {200, 302})

    def cart_html(self):
        response = self.client.get("/cart")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_short_cart_is_told_how_much_more_it_needs(self):
        Product.objects.filter(pk=100).update(
            price=1500, delivery_type=Product.DeliveryTypes.PHYSICAL)
        self.add(100)

        self.assertIn("Add <strong>$25.00</strong> more", self.cart_html())

    def test_a_qualifying_cart_is_told_it_has_earned_it(self):
        Product.objects.filter(pk=100).update(
            price=4200, delivery_type=Product.DeliveryTypes.PHYSICAL)
        self.add(100)

        html = self.cart_html()

        self.assertIn("this order qualifies", html)
        self.assertNotIn("more and\n                    shipping is on us",
                         html)

    def test_the_shortfall_follows_the_quantity(self):
        # Two $15 books leave $10 to go, not $25. A shortfall computed off the
        # unit price would keep nagging a cart that had already qualified.
        Product.objects.filter(pk=100).update(
            price=1500, delivery_type=Product.DeliveryTypes.PHYSICAL)
        self.add(100, quantity=2)

        self.assertIn("Add <strong>$10.00</strong> more", self.cart_html())

    def test_a_digital_cart_is_not_offered_free_shipping(self):
        # Nothing to post: a download has no shipping to be free, and telling
        # a buyer to spend more to save postage they were never charged is
        # the shipping-on-a-PDF bug wearing a different hat.
        Product.objects.filter(pk=100).update(
            price=1500, delivery_type=Product.DeliveryTypes.DIGITAL)
        self.add(100)

        html = self.cart_html()

        self.assertNotIn("more and", html)
        self.assertNotIn("this order qualifies", html)

    @override_settings(FREE_SHIPPING_THRESHOLD=9900)
    def test_the_advertised_threshold_follows_the_setting(self):
        # The offer is advertised from the setting checkout reads, so moving
        # the threshold cannot leave a stale "$40" on the page.
        Product.objects.filter(pk=100).update(
            price=1500, delivery_type=Product.DeliveryTypes.PHYSICAL)
        self.add(100)

        html = self.cart_html()

        self.assertIn("orders over $99", html)
        self.assertNotIn("orders over $40", html)

    def test_a_cart_with_a_subscription_line_is_not_nagged(self):
        # Stripe Checkout does not support shipping options in subscription
        # mode, so Payments.checkout attaches none and a mixed cart already
        # ships free. Telling its owner to add $25 more to earn free shipping
        # would be the page contradicting its own checkout.
        Product.objects.filter(pk=100).update(
            price=1500, delivery_type=Product.DeliveryTypes.PHYSICAL)
        Product.objects.filter(pk=101).update(
            price=500, mode=Product.Modes.SUBSCRIPTION)
        self.add(100)
        self.add(101)

        html = self.cart_html()

        self.assertNotIn("more and", html)
        self.assertNotIn("this order qualifies", html)
