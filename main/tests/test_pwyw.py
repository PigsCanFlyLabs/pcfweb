"""Tests for pay-what-you-want pricing.

The amount is collected on this site now, not on Stripe's. That moves three
things into scope that were previously Stripe's problem: validating a number
that arrives from a public form, deciding which cart row it belongs to, and
making sure the number that reaches Stripe is the one the database holds
rather than the one the last request happened to carry.

What it moves *out* of scope is the set of refusals that used to exist here.
Stripe enforces four constraints on a Price with a custom_unit_amount -- one
line item per session, quantity exactly one, no discounts, no
adjustable_quantity -- and a fifth by omission, in that such a Price cannot be
recurring. A pay-what-you-want line is an ordinary fixed Price now, so none of
those bind, and the tests that asserted the refusals assert their absence.
"""

from unittest import mock

from django.core import mail
from django.test import Client, RequestFactory, TestCase

from main.models import (
    MAX_PWYW_AMOUNT, PWYW_ROUND_DOWN_BELOW, Cart, CartProduct, Order, Product,
    PwywAmountError, parse_pwyw_amount, round_pwyw_amount)
from main.payments import Payments
from main.tests.base import (
    EBOOK_PK, BookAssetRootMixin, CartTestBase, OrderTestBase)

# The print edition of the same book: the other half of the bundle that could
# not be bought in one go until this change.
PRINT_PK = 104


class PwywAmountParsingTest(TestCase):
    """The amount is hostile input until this function has returned an int."""

    def test_dollars_and_cents_become_cents(self):
        self.assertEqual(parse_pwyw_amount("12.99"), 1299)

    def test_a_whole_number_of_dollars_becomes_cents(self):
        self.assertEqual(parse_pwyw_amount("7"), 700)

    def test_nothing_at_all_is_a_valid_amount(self):
        # The site's copy promises this in three places. It is not an edge
        # case to be tolerated, it is the offer.
        self.assertEqual(parse_pwyw_amount("0"), 0)
        self.assertEqual(parse_pwyw_amount("0.00"), 0)

    def test_the_round_down_threshold_is_one_dollar(self):
        # Pinned so a change to the band has to be deliberate. The value is
        # load-bearing: Stripe refuses 1..49c outright, so the band must reach
        # at least 50c for the forbidden range to be unreachable at all.
        self.assertEqual(PWYW_ROUND_DOWN_BELOW, 100)
        self.assertGreaterEqual(PWYW_ROUND_DOWN_BELOW, 50)

    def test_a_dollar_is_charged_as_entered(self):
        # The inclusive edge. One cent lower is a free download, so an
        # off-by-one here silently gives away a dollar sale.
        self.assertEqual(parse_pwyw_amount("1.00"), 100)

    def test_ninety_nine_cents_rounds_down_to_nothing(self):
        self.assertEqual(parse_pwyw_amount("0.99"), 0)

    def test_one_cent_rounds_down_to_nothing(self):
        self.assertEqual(parse_pwyw_amount("0.01"), 0)

    def test_the_whole_forbidden_band_is_unreachable(self):
        # Stripe rejects any charge under 50c (amount_too_small). Nothing in
        # 1..49c may survive parsing, or a buyer reaches a session Stripe will
        # refuse to create.
        for cents in range(1, 50):
            with self.subTest(cents=cents):
                self.assertEqual(parse_pwyw_amount(f"0.{cents:02d}"), 0)

    def test_nothing_between_a_cent_and_a_dollar_survives(self):
        for cents in range(1, PWYW_ROUND_DOWN_BELOW):
            with self.subTest(cents=cents):
                self.assertEqual(parse_pwyw_amount(f"0.{cents:02d}"), 0)

    def test_amounts_at_or_above_the_band_are_untouched(self):
        for cents in (100, 101, 150, 1299, 100000):
            with self.subTest(cents=cents):
                self.assertEqual(round_pwyw_amount(cents), cents)

    def test_rounding_does_not_rescue_a_refused_amount(self):
        # The band is a rounding rule, not a coercion. Everything below is
        # inside or adjacent to the 1..99c range, and every one must still be
        # an error rather than quietly becoming a free download.
        for raw in ("-0.50", "0.501", "0.005", "half", "", None, "NaN",
                    "-Infinity", "0.5x"):
            with self.subTest(raw=raw):
                with self.assertRaises(PwywAmountError):
                    parse_pwyw_amount(raw)

    def test_a_leading_dollar_sign_is_tolerated(self):
        self.assertEqual(parse_pwyw_amount("$5.00"), 500)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(parse_pwyw_amount("  3.50  "), 350)

    def test_a_negative_amount_is_refused(self):
        # Would otherwise be a Price with a negative unit_amount, i.e. an
        # invitation to be paid for taking the book.
        with self.assertRaises(PwywAmountError) as raised:
            parse_pwyw_amount("-1.00")

        self.assertIn("negative", str(raised.exception))

    def test_a_negative_zero_amount_is_refused_or_zero(self):
        # Decimal("-0.00") is not less than zero, so it lands on 0 rather than
        # on the negative branch. Either answer is safe; a negative one is not.
        self.assertEqual(parse_pwyw_amount("-0.00"), 0)

    def test_more_than_two_decimal_places_is_refused(self):
        # Rounding here would mean the amount charged is not the amount shown.
        with self.assertRaises(PwywAmountError):
            parse_pwyw_amount("1.005")

    def test_a_word_is_refused(self):
        with self.assertRaises(PwywAmountError):
            parse_pwyw_amount("free")

    def test_an_empty_amount_is_refused(self):
        with self.assertRaises(PwywAmountError):
            parse_pwyw_amount("")

    def test_a_missing_amount_is_refused(self):
        with self.assertRaises(PwywAmountError):
            parse_pwyw_amount(None)

    def test_nan_is_refused(self):
        # Decimal("NaN") parses. Every comparison against it is False, so it
        # would slide past both the floor and the ceiling untouched.
        with self.assertRaises(PwywAmountError):
            parse_pwyw_amount("NaN")

    def test_infinity_is_refused(self):
        with self.assertRaises(PwywAmountError):
            parse_pwyw_amount("Infinity")

    def test_negative_infinity_is_refused(self):
        with self.assertRaises(PwywAmountError):
            parse_pwyw_amount("-Infinity")

    def test_the_ceiling_is_inclusive(self):
        self.assertEqual(
            parse_pwyw_amount(str(MAX_PWYW_AMOUNT // 100)), MAX_PWYW_AMOUNT)

    def test_above_the_ceiling_is_refused(self):
        with self.assertRaises(PwywAmountError):
            parse_pwyw_amount(str(MAX_PWYW_AMOUNT // 100 + 1))

    def test_exponent_notation_does_not_smuggle_a_huge_amount_through(self):
        # Decimal parses "1e9" happily; str.isdigit() and int() would not have
        # agreed with each other about it.
        with self.assertRaises(PwywAmountError):
            parse_pwyw_amount("1e9")


class PwywPriceTest(TestCase):
    """A pay-what-you-want product mints an ordinary fixed Price."""

    @mock.patch("main.payments.stripe.Price.create")
    def test_a_price_is_fixed_at_the_amount_asked_for(self, create):
        # CONTROL -- passes on 3090eab too. This is the old
        # test_a_fixed_price_is_unchanged, kept because a fixed price
        # staying fixed is still worth pinning; it proves nothing new.
        create.return_value = {"id": "price_fixed"}

        Payments.create_price("prod_x", 1500)

        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["unit_amount"], 1500)
        # The whole point: no custom_unit_amount anywhere, because that is
        # what carried Stripe's four constraints.
        self.assertNotIn("custom_unit_amount", kwargs)

    @mock.patch("main.payments.stripe.Price.create")
    def test_a_recurring_price_carries_the_interval_and_the_amount(self, create):
        # Control: unchanged, and passes on 3090eab too. Here so that the
        # subscription-mode test below is read as a change of behaviour on the
        # pay-what-you-want branch specifically, not on recurring prices.
        create.return_value = {"id": "price_recurring"}

        Payments.create_price("prod_x", 1500, interval="year")

        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["unit_amount"], 1500)
        self.assertEqual(kwargs["recurring"], {"interval": "year"})

    # Stubs Stripe's HTTP call rather than Payments, because the refusal being
    # removed lived inside Payments.create_price: mocking Payments would step
    # straight over the thing under test.
    @mock.patch("main.payments.stripe.Price.create")
    def test_a_subscription_mode_pwyw_product_mints_a_price_instead_of_raising(
            self, price_create):
        # On 3090eab this raises ValueError("...only works for one-off
        # payments..."), so a subscription-mode pay-what-you-want product
        # could not go in a cart at all. The refusal existed only because
        # custom_unit_amount is payment-mode only. Verified against the live
        # Stripe test API that a recurring Price at a chosen unit_amount is
        # created and that a subscription-mode session accepts it, so the
        # product is simply billed at the chosen amount every year.
        price_create.return_value = {"id": "price_recurring"}
        product = Product.objects.create(
            name="Pay-what-you-want support", external_product_id="prod_sub",
            price=10000, is_pwyw=True, mode=Product.Modes.SUBSCRIPTION,
            delivery_type=Product.DeliveryTypes.SERVICE)
        cart = Cart.objects.create()

        line = CartProduct.objects.create(
            cart=cart, product=product, quantity=1, chosen_amount=2500)

        self.assertEqual(line.price_id, "price_recurring")
        kwargs = price_create.call_args.kwargs
        self.assertEqual(kwargs["unit_amount"], 2500)
        self.assertEqual(kwargs["recurring"], {"interval": "year"})
        self.assertNotIn("custom_unit_amount", kwargs)

    @mock.patch("main.models.Payments")
    def test_a_pwyw_line_mints_its_price_at_the_chosen_amount(self, payments):
        payments.create_price.return_value = "price_pwyw"
        product = Product.objects.create(
            name="E-book", external_product_id="prod_ebook", price=1500,
            is_pwyw=True, delivery_type=Product.DeliveryTypes.DIGITAL)
        cart = Cart.objects.create()

        CartProduct.objects.create(
            cart=cart, product=product, quantity=1, chosen_amount=250)

        self.assertEqual(payments.create_price.call_args.args[1], 250)

    @mock.patch("main.models.Payments")
    def test_a_pwyw_line_with_no_chosen_amount_falls_back_to_the_suggestion(
            self, payments):
        # Control: passes on 3090eab too, because there the suggestion was the
        # only amount there was. It is here to pin the null case, which is
        # what every cart row that predates the migration holds.
        payments.create_price.return_value = "price_pwyw"
        product = Product.objects.create(
            name="E-book", external_product_id="prod_ebook", price=1500,
            is_pwyw=True, delivery_type=Product.DeliveryTypes.DIGITAL)
        cart = Cart.objects.create()

        CartProduct.objects.create(cart=cart, product=product, quantity=1)

        self.assertEqual(payments.create_price.call_args.args[1], 1500)

    @mock.patch("main.models.Payments")
    def test_an_ordinary_product_ignores_a_chosen_amount(self, payments):
        # chosen_amount is only meaningful on a pay-what-you-want row. A value
        # on any other row must not reprice it.
        payments.create_price.return_value = "price_fixed"
        product = Product.objects.create(
            name="Print", external_product_id="prod_print", price=3000)
        cart = Cart.objects.create()

        line = CartProduct.objects.create(
            cart=cart, product=product, quantity=1, chosen_amount=1)

        self.assertEqual(payments.create_price.call_args.args[1], 3000)
        self.assertEqual(line.effective_unit_amount(), 3000)

    @mock.patch("main.models.Payments")
    def test_changing_the_amount_mints_a_new_price(self, payments):
        # A Stripe Price is immutable, so the old id would keep billing the
        # old amount.
        payments.create_price.side_effect = ["price_first", "price_second"]
        product = Product.objects.create(
            name="E-book", external_product_id="prod_ebook", price=1500,
            is_pwyw=True, delivery_type=Product.DeliveryTypes.DIGITAL)
        cart = Cart.objects.create()
        line = CartProduct.objects.create(
            cart=cart, product=product, quantity=1, chosen_amount=1500)

        line.set_chosen_amount(200)

        self.assertEqual(line.price_id, "price_second")
        self.assertEqual(payments.create_price.call_args.args[1], 200)


class PwywCheckoutShapeTest(TestCase):
    """What used to be refused, and now is not.

    Each of these was blocked by PR #62 because Stripe enforces it on a Price
    with a custom_unit_amount. Every one was re-run against the live Stripe
    test API as an ordinary fixed price and accepted; the quoted refusals are
    the errors the custom_unit_amount shape still returns.
    """

    def setUp(self):
        self.factory = RequestFactory()

    def _cart(self, *products_with_quantity):
        cart = Cart.objects.create()
        for product, quantity in products_with_quantity:
            cart_product = CartProduct.objects.create(
                cart=cart, product=product, quantity=quantity,
                price_id=f"price_{product.pk}")
            cart.products.add(cart_product)
        return cart

    def _pwyw(self):
        return Product.objects.create(
            name="E-book", external_product_id="prod_ebook", price=1500,
            is_pwyw=True, delivery_type=Product.DeliveryTypes.DIGITAL)

    def _fixed(self):
        return Product.objects.create(
            name="Print", external_product_id="prod_print", price=3000)

    def _second_pwyw(self):
        return Product.objects.create(
            name="PWYW poster", external_product_id="prod_pwyw_poster",
            price=2500, is_pwyw=True)

    def _checkout(self, cart, coupon=None):
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_x")
            Payments.checkout(self.factory.get("/checkout"), cart, coupon=coupon)
        return create.call_args.kwargs

    def test_a_pwyw_line_now_carries_adjustable_quantity(self):
        # Was: "You cannot specify `adjustable_quantity` when also using a
        # price with `custom_unit_amount`."
        params = self._checkout(self._cart((self._pwyw(), 1)))

        item, = params["line_items"]
        self.assertEqual(item["adjustable_quantity"], {"enabled": True})
        self.assertEqual(params["mode"], "payment")

    def test_fixed_price_lines_keep_adjustable_quantity_and_count(self):
        # Unchanged by this work, and kept for exactly that reason.
        first = self._fixed()
        second = Product.objects.create(
            name="Poster", external_product_id="prod_poster", price=2000)

        params = self._checkout(self._cart((first, 1), (second, 1)))

        by_price = {item["price"]: item for item in params["line_items"]}
        self.assertEqual(len(by_price), 2)
        self.assertEqual(
            by_price[f"price_{first.pk}"]["adjustable_quantity"],
            {"enabled": True})
        self.assertEqual(
            by_price[f"price_{second.pk}"]["adjustable_quantity"],
            {"enabled": True})

    def test_a_mixed_cart_is_one_session_with_two_line_items(self):
        # THE headline. Was: "You can only specify 1 line item when specifying
        # a price with `custom_unit_amount` configured."
        pwyw, fixed = self._pwyw(), self._fixed()

        params = self._checkout(self._cart((pwyw, 1), (fixed, 1)))

        prices = {item["price"] for item in params["line_items"]}
        self.assertEqual(
            prices, {f"price_{pwyw.pk}", f"price_{fixed.pk}"})

    def test_a_pwyw_line_may_have_a_quantity_above_one(self):
        # Was: "Quantity must be 1 when a price with `custom_unit_amount` is
        # specified."
        params = self._checkout(self._cart((self._pwyw(), 4)))

        item, = params["line_items"]
        self.assertEqual(item["quantity"], 4)

    def test_two_pwyw_lines_share_one_session(self):
        first, second = self._pwyw(), self._second_pwyw()

        params = self._checkout(self._cart((first, 1), (second, 1)))

        self.assertEqual(len(params["line_items"]), 2)

    def test_a_coupon_survives_to_stripe_on_a_pwyw_cart(self):
        # Was: "You cannot enable discounts when using a price with
        # `custom_unit_amount` configured." The coupon used to be stripped and
        # the buyer told why.
        params = self._checkout(
            self._cart((self._pwyw(), 1)), coupon="coupon_sale")

        self.assertEqual(params["discounts"], [{"coupon": "coupon_sale"}])

    def test_a_pwyw_product_may_ride_in_a_subscription_mode_session(self):
        # The session mode is a property of the whole cart, so one
        # subscription line used to drag the pay-what-you-want line into a
        # mode custom_unit_amount does not support. A fixed recurring Price
        # has no such problem.
        subscription = Product.objects.create(
            name="Support", external_product_id="prod_sub", price=10000,
            mode=Product.Modes.SUBSCRIPTION,
            delivery_type=Product.DeliveryTypes.SERVICE)
        cart = self._cart((self._pwyw(), 1), (subscription, 1))

        params = self._checkout(cart)

        self.assertEqual(params["mode"], "subscription")
        self.assertEqual(len(params["line_items"]), 2)


class PwywAmountIsServerOwnedTest(CartTestBase):
    """The amount that reaches Stripe is the one the cart row holds.

    Everything here is an attack. The amount arrives from a form on a public
    page, so the only useful question about it is what happens when it is not
    what the form was supposed to send.
    """

    def _cart_row(self):
        return CartProduct.objects.get(product_id=EBOOK_PK)

    def test_an_amount_chosen_on_the_product_page_is_stored(self):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "3.50"})

        self.assertEqual(self._cart_row().chosen_amount, 350)

    def test_zero_is_stored_as_zero_and_not_as_no_choice(self):
        # The distinction matters: None means "fall back to the suggestion",
        # so a 0 that round-tripped as None would bill 12.99.
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "0"})

        row = self._cart_row()
        self.assertEqual(row.chosen_amount, 0)
        self.assertEqual(row.effective_unit_amount(), 0)

    def test_a_negative_amount_is_refused_at_add_to_cart(self):
        response = self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "-5.00"},
            follow=True)

        self.assertContains(response, "cannot be negative")
        self.assertFalse(
            CartProduct.objects.filter(product_id=EBOOK_PK).exists())

    def test_a_fractional_cent_is_refused_at_add_to_cart(self):
        response = self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "1.0049"},
            follow=True)

        self.assertContains(response, "two decimal places")
        self.assertFalse(
            CartProduct.objects.filter(product_id=EBOOK_PK).exists())

    def test_a_string_is_refused_at_add_to_cart(self):
        response = self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "lots"},
            follow=True)

        self.assertContains(response, "not an amount")
        self.assertFalse(
            CartProduct.objects.filter(product_id=EBOOK_PK).exists())

    def test_a_huge_amount_is_refused_at_add_to_cart(self):
        response = self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "99999999"},
            follow=True)

        self.assertContains(response, "most that can be taken")
        self.assertFalse(
            CartProduct.objects.filter(product_id=EBOOK_PK).exists())

    def test_an_amount_posted_at_an_ordinary_product_is_ignored(self):
        self.client.post(f"/add-to-cart/{PRINT_PK}/1", {"chosen_amount": "0"})

        row = CartProduct.objects.get(product_id=PRINT_PK)
        self.assertIsNone(row.chosen_amount)
        self.assertEqual(
            row.effective_unit_amount(), Product.objects.get(pk=PRINT_PK).price)

    def test_an_amount_posted_to_checkout_cannot_undercut_the_cart_row(self):
        # The attack the whole design is arranged around: choose a real
        # amount, then inject a different one at the endpoint that talks to
        # Stripe.
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "9.00"})

        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_tampered")
            self.client.post(
                "/checkout", {"chosen_amount": "0", "amount": "0"})

        order = Order.objects.get(stripe_session_id="cs_tampered")
        item = order.items.get()
        self.assertEqual(item.unit_amount, 900)
        self.assertEqual(self._cart_row().chosen_amount, 900)

    def test_the_price_billed_is_minted_from_the_row_at_session_creation(self):
        # Even a price_id that has been made to point somewhere else is
        # replaced, because refresh_pwyw_price() mints from chosen_amount
        # rather than trusting whatever the row is carrying.
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "9.00"})
        CartProduct.objects.filter(product_id=EBOOK_PK).update(
            price_id="price_attacker_controlled")

        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_reminted")
            self.client.post("/checkout")

        params = create.call_args.kwargs
        item, = params["line_items"]
        self.assertNotEqual(item["price"], "price_attacker_controlled")

    def test_the_cart_editor_stores_a_new_amount(self):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "9.00"})
        row = self._cart_row()

        self.client.post(
            f"/cart/pwyw-amount/{row.pk}", {"chosen_amount": "2.25"})

        self.assertEqual(self._cart_row().chosen_amount, 225)

    def test_the_cart_editor_refuses_a_hostile_amount(self):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "9.00"})
        row = self._cart_row()

        response = self.client.post(
            f"/cart/pwyw-amount/{row.pk}", {"chosen_amount": "-1"},
            follow=True)

        self.assertContains(response, "cannot be negative")
        self.assertEqual(self._cart_row().chosen_amount, 900)

    def test_the_cart_editor_will_not_touch_somebody_elses_row(self):
        other_cart = Cart.objects.create()
        stranger = CartProduct.objects.create(
            cart=other_cart, product=Product.objects.get(pk=EBOOK_PK),
            quantity=1, chosen_amount=900)
        self.client.get("/cart")

        response = self.client.post(
            f"/cart/pwyw-amount/{stranger.pk}", {"chosen_amount": "0"})

        self.assertEqual(response.status_code, 404)
        stranger.refresh_from_db()
        self.assertEqual(stranger.chosen_amount, 900)

    def test_the_cart_editor_refuses_a_row_that_is_not_pwyw(self):
        self.client.post(f"/add-to-cart/{PRINT_PK}/1")
        row = CartProduct.objects.get(product_id=PRINT_PK)

        response = self.client.post(
            f"/cart/pwyw-amount/{row.pk}", {"chosen_amount": "0"})

        self.assertEqual(response.status_code, 400)

    def test_the_cart_editor_is_post_only(self):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "9.00"})
        row = self._cart_row()

        response = self.client.get(f"/cart/pwyw-amount/{row.pk}")

        self.assertEqual(response.status_code, 405)


class PwywOrderSnapshotTest(CartTestBase):
    """OrderItem.unit_amount is what the buyer chose, not the suggestion."""

    def test_the_snapshot_holds_the_chosen_amount(self):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "2.00"})

        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_chosen")
            self.client.post("/checkout")

        item = Order.objects.get(stripe_session_id="cs_chosen").items.get()
        self.assertEqual(item.unit_amount, 200)
        self.assertEqual(item.total_amount(), 200)

    def test_a_zero_order_snapshots_zero(self):
        # This is the bug in the brief: a 12.99 line above a 0.00 total.
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "0"})

        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_zero")
            self.client.post("/checkout")

        order = Order.objects.get(stripe_session_id="cs_zero")
        self.assertEqual(order.items.get().unit_amount, 0)
        self.assertEqual(order.amount_total, 0)

    def test_the_cart_total_is_the_chosen_amount(self):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/2", {"chosen_amount": "1.50"})

        response = self.client.get("/cart")

        self.assertEqual(
            CartProduct.objects.get(product_id=EBOOK_PK).total_price(), 300)
        self.assertContains(response, "3.00")


class ZeroTotalOrderTest(BookAssetRootMixin, OrderTestBase):
    """A $0 pay-what-you-want order is still an order.

    Verified against the live Stripe test API rather than assumed: a fixed
    Price of unit_amount=0 is created, and a payment-mode Checkout Session
    holding one is created with amount_total 0 and payment_method_collection
    "if_required". So $0 stays an ordinary Stripe checkout -- it needs no
    bypass, and the promise that nothing at all is a valid amount holds.

    Stripe creates no PaymentIntent for a zero total, so the session reports
    payment_status "no_payment_required" and can never report "paid". Treating
    only "paid" as sold left such an order PENDING forever: no record, no
    owner email, and no book.
    """

    def _zero_total_body(self, order):
        return self.event_body(
            order, payment_status="no_payment_required",
            amount_total=0, amount_subtotal=0,
            total_details={"amount_tax": 0, "amount_shipping": 0})

    def test_a_zero_total_order_is_recorded_as_paid(self):
        order = self.place_order(product_pk=EBOOK_PK, quantity=1)

        response = self.deliver(self._zero_total_body(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.amount_total, 0)
        self.assertIsNotNone(order.paid_at)

    def test_a_zero_total_order_still_tells_the_owner(self):
        order = self.place_order(product_pk=EBOOK_PK, quantity=1)

        self.deliver(self._zero_total_body(order))

        order.refresh_from_db()
        self.assertIsNotNone(order.notified_at)
        self.assertEqual(len(self.order_emails()), 1)

    def test_an_unpaid_session_is_still_left_pending(self):
        # The guard is widened, not removed: an ACH debit that has not settled
        # must still wait for async_payment_succeeded.
        order = self.place_order(product_pk=EBOOK_PK, quantity=1)

        response = self.deliver(
            self.event_body(order, payment_status="unpaid"))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(self.order_emails(), [])


def squashed(response):
    """Response body with runs of whitespace collapsed to single spaces.

    The notices below wrap across several template lines, so a raw substring
    search would miss them over the indentation.
    """
    return " ".join(response.content.decode().split())


# The card label and the receipt note, quoted once so a test cannot drift
# away from the template and still pass.
CARD_LABEL = "Pay what you want &mdash; suggested amount, or nothing at all"
RECEIPT_NOTE = "Pay what you want: this is the amount you paid."
# What the receipt used to say, back when unit_amount was the suggestion. It
# is a lie now, so it must not survive anywhere.
STALE_RECEIPT_NOTE = "shown at the suggested amount"
CART_FLOOR = "nothing at all is a valid amount"


class PwywIsLabelledWhereverAPriceIsShownTest(CartTestBase):
    """A bare price on a pay-what-you-want row reads as a fixed one.

    The product page already says the amount is a suggestion, the buyer
    chooses, and zero is allowed. The listing cards showed the number with
    nothing beside it, so they said the opposite by omission.

    CartTestBase for the Stripe stub: creating a Product and adding one to a
    cart both mint Stripe objects on save.
    """

    def test_the_products_listing_labels_the_pwyw_ebook(self):
        response = self.client.get("/products")

        self.assertEqual(response.status_code, 200)
        self.assertIn(CARD_LABEL, squashed(response))

    def test_the_products_listing_leaves_fixed_price_rows_alone(self):
        # The label is conditional, not decoration on every card.
        response = self.client.get("/products")

        self.assertEqual(squashed(response).count(CARD_LABEL), 1)

    def test_the_homepage_carousel_labels_a_pwyw_product(self):
        # The carousel shows the three dearest rows per category, and the
        # e-book is not one of them -- so price this above the fixtures.
        Product.objects.create(
            name="A Dear E-book", description="d", price=999999,
            is_pwyw=True, cat=Product.Categories.BOOKS,
            delivery_type=Product.DeliveryTypes.DIGITAL)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(CARD_LABEL, squashed(response))

    def test_the_cart_says_zero_is_allowed(self):
        # The cart already called the total a suggestion and said the buyer
        # chooses at checkout; it did not say the floor was zero.
        self.client.post(f"/add-to-cart/{EBOOK_PK}/1")

        response = self.client.get("/cart")

        self.assertIn(CART_FLOOR, squashed(response))


class PwywAmountFieldIsOnThePageTest(CartTestBase):
    """The amount is asked for here, so the field has to be here."""

    def test_the_product_page_offers_an_amount_field(self):
        response = self.client.get(f"/product/{EBOOK_PK}")

        body = squashed(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="chosen_amount"', body)
        # Pre-filled with the owner's suggestion, per the brief.
        self.assertIn('value="12.99"', body)

    def test_the_product_page_still_says_the_price_is_a_suggestion(self):
        # CONTROL -- passes on 3090eab too, and that is the point: the
        # copy from #57 must not regress, only stop pointing at Stripe.
        response = self.client.get(f"/product/{EBOOK_PK}")

        body = squashed(response)
        self.assertIn("<strong>Pay what you want.</strong>", body)
        self.assertIn("suggestion, not a price", body)
        self.assertIn(CART_FLOOR, body)

    def test_the_product_page_points_at_the_field_not_at_checkout(self):
        response = self.client.get(f"/product/{EBOOK_PK}")

        body = squashed(response)
        self.assertIn("you set your own amount below", body)
        self.assertNotIn("you set your own amount at checkout", body)

    def test_an_ordinary_product_page_has_no_amount_field(self):
        # CONTROL -- passes on 3090eab too, where no product has the
        # field at all. Here to scope the field, not to prove it exists.
        response = self.client.get(f"/product/{PRINT_PK}")

        self.assertNotIn('name="chosen_amount"', squashed(response))

    def test_the_cart_offers_an_amount_field_on_the_pwyw_row(self):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "4.00"})

        response = self.client.get("/cart")

        body = squashed(response)
        self.assertIn('name="chosen_amount"', body)
        # Showing the chosen amount back, not the suggestion.
        self.assertIn('value="4.00"', body)

    def test_the_cart_offers_no_amount_field_on_an_ordinary_row(self):
        # CONTROL -- passes on 3090eab too, for the same reason.
        self.client.post(f"/add-to-cart/{PRINT_PK}/1")

        response = self.client.get("/cart")

        self.assertNotIn('name="chosen_amount"', squashed(response))


# The owner's own wording, byte for byte. Quoted here so that any reword of
# main/templates/components/pwyw-round-down-notice.html fails the suite rather
# than shipping. Do not "fix" the punctuation, the ":D", or the hyphen in
# "e-mail" in either place.
OWNER_ROUND_DOWN_COPY = (
    "Every kid should know the joy of distributed systems if they want to "
    "(anything below a dollar costs more in payment processing so we'll just "
    "round down to $0. If Stripe is being difficult with $0 please e-mail us "
    "and we can e-mail you a copy too! :D)")
OWNER_MAILTO = 'href="mailto:holden@pigscanfly.ca"'


class OwnerRoundDownCopyTest(CartTestBase):
    """The owner's sentence, where the amount can be entered or edited."""

    def test_the_product_page_carries_the_owners_words_verbatim(self):
        response = self.client.get(f"/product/{EBOOK_PK}")

        # Not squashed: asserted against the raw bytes, which is why the
        # sentence is kept on one line in the template.
        self.assertContains(response, OWNER_ROUND_DOWN_COPY, html=False)

    def test_the_cart_carries_the_owners_words_verbatim(self):
        self.client.post(f"/add-to-cart/{EBOOK_PK}/1")

        response = self.client.get("/cart")

        self.assertContains(response, OWNER_ROUND_DOWN_COPY, html=False)

    def test_the_notice_offers_a_real_mailto(self):
        # A raw mailto, deliberately: the owner has accepted that it is
        # scrapeable and does not want a contact form here.
        response = self.client.get(f"/product/{EBOOK_PK}")

        self.assertContains(response, OWNER_MAILTO)
        self.assertContains(response, "holden@pigscanfly.ca")

    def test_an_ordinary_product_page_carries_no_round_down_notice(self):
        # Control: it is scoped to where an amount can be entered.
        response = self.client.get(f"/product/{PRINT_PK}")

        self.assertNotContains(response, OWNER_ROUND_DOWN_COPY, html=False)


class PwywChargedAmountIsShownBeforeCommitTest(CartTestBase):
    """Rounding must not be a surprise at the till."""

    def test_the_product_page_states_what_will_be_charged(self):
        response = self.client.get(f"/product/{EBOOK_PK}")

        body = squashed(response)
        self.assertIn("You will be charged:", body)
        # The suggestion is 12.99, which is above the band, so it stands.
        self.assertIn('<strong id="pwyw-charge-amount">$12.99</strong>', body)

    def test_the_page_hands_the_threshold_to_its_javascript(self):
        # So the running total rounds by the same rule the server does,
        # instead of the number being written out a second time.
        response = self.client.get(f"/product/{EBOOK_PK}")

        self.assertContains(
            response, f"< {PWYW_ROUND_DOWN_BELOW})")

    def test_the_cart_shows_a_rounded_entry_back_as_zero(self):
        # Entering 0.50 must read as 0.00 before checkout, not after.
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "0.50"})

        response = self.client.get("/cart")

        body = squashed(response)
        self.assertIn('value="0.00"', body)
        self.assertNotIn('value="0.50"', body)

    def test_the_cart_total_reflects_the_rounded_amount(self):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "0.99"})

        response = self.client.get("/cart")

        self.assertEqual(
            CartProduct.objects.get(product_id=EBOOK_PK).chosen_amount, 0)
        self.assertContains(response, "0.00")


class RoundedDownOrderTest(CartTestBase):
    """An amount under a dollar becomes a free order, end to end."""

    def _order(self, amount, session_id):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": amount})
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id=session_id)
            self.client.post("/checkout")
        self.create_call = create
        return Order.objects.get(stripe_session_id=session_id)

    def test_fifty_cents_is_snapshotted_as_zero(self):
        order = self._order("0.50", "cs_rounded")

        self.assertEqual(order.items.get().unit_amount, 0)
        self.assertEqual(order.amount_total, 0)

    def test_a_dollar_is_snapshotted_as_a_dollar(self):
        order = self._order("1.00", "cs_dollar")

        self.assertEqual(order.items.get().unit_amount, 100)
        self.assertEqual(order.amount_total, 100)

    def test_a_suggestion_below_the_band_cannot_reach_stripe(self):
        # The admin edits Product.price directly, so it never goes through
        # parse_pwyw_amount. A pay-what-you-want product priced at 25c would
        # otherwise bill 25c to any buyer who did not name an amount, and
        # Stripe refuses that outright (amount_too_small) -- a dead checkout
        # for every one of them. The band is applied on the way out too.
        Product.objects.filter(pk=EBOOK_PK).update(price=25)
        self.client.post(f"/add-to-cart/{EBOOK_PK}/1")

        row = CartProduct.objects.get(product_id=EBOOK_PK)

        self.assertIsNone(row.chosen_amount)
        self.assertEqual(row.effective_unit_amount(), 0)

    def test_a_suggestion_at_the_band_is_left_alone(self):
        Product.objects.filter(pk=EBOOK_PK).update(price=100)
        self.client.post(f"/add-to-cart/{EBOOK_PK}/1")

        self.assertEqual(
            CartProduct.objects.get(
                product_id=EBOOK_PK).effective_unit_amount(), 100)

    def test_an_ordinary_product_priced_low_is_not_rounded(self):
        # The band belongs to pay-what-you-want, not to the catalogue. A
        # cheap fixed-price item is the owner's pricing decision.
        Product.objects.filter(pk=PRINT_PK).update(price=25)
        self.client.post(f"/add-to-cart/{PRINT_PK}/1")

        self.assertEqual(
            CartProduct.objects.get(
                product_id=PRINT_PK).effective_unit_amount(), 25)

    def test_the_preset_never_leaks_into_a_rounded_order(self):
        # The original bug, in its round-down form: a 12.99 line above a
        # 0.00 total.
        order = self._order("0.25", "cs_nopreset")

        self.assertNotEqual(order.items.get().unit_amount, 1299)
        self.assertEqual(order.items.get().unit_amount, 0)


class PwywReceiptTest(BookAssetRootMixin, OrderTestBase):
    """The receipt line and the receipt total agree now.

    They used to disagree on purpose: OrderItem.unit_amount snapshotted
    Product.price, so a buyer who paid nothing saw a 12.99 line above a 0.00
    total, and both the receipt and the owner's email carried a sentence
    explaining the mismatch. The snapshot is the chosen amount now, so the
    mismatch is gone and so is the sentence that described it.
    """

    def _paid_for_nothing(self):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "0"})
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/session", id="cs_test_session")
            self.client.post("/checkout")
        order = Order.objects.get(stripe_session_id="cs_test_session")
        self.deliver(self.event_body(
            order, payment_status="no_payment_required",
            amount_total=0, amount_subtotal=0,
            total_details={"amount_tax": 0, "amount_shipping": 0}))
        order.refresh_from_db()
        return order

    def test_the_receipt_line_matches_the_receipt_total(self):
        order = self._paid_for_nothing()

        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")

        body = squashed(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Total: $0.00", body)
        # The line the mismatch used to produce.
        self.assertNotIn("12.99", body)
        self.assertIn(RECEIPT_NOTE, body)

    def test_the_receipt_no_longer_claims_the_line_is_the_suggestion(self):
        order = self._paid_for_nothing()

        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertNotIn(STALE_RECEIPT_NOTE, squashed(response))

    def test_a_fixed_price_receipt_carries_no_pwyw_note(self):
        order = self.place_order(product_pk=100, quantity=1)
        self.deliver(self.event_body(order))

        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertNotIn(RECEIPT_NOTE, squashed(response))

    def test_the_owner_email_reports_the_amount_actually_paid(self):
        order = self._paid_for_nothing()

        owner_email, = self.order_emails()

        self.assertIn("the amount the buyer paid", owner_email.body)
        self.assertNotIn("see the order total for what was paid",
                         owner_email.body)
        self.assertIn("@ 0.00", owner_email.body)


class ZeroOrderStillDeliversTheBookTest(BookAssetRootMixin, OrderTestBase):
    """A kid who pays nothing still gets the file.

    "The order reached a terminal state" is not the same claim as "the buyer
    received the book". A zero-total session settles as
    "no_payment_required" and can never report "paid", so anything in the
    completion path that keys off "paid" would leave a $0 buyer on a success
    page with no download and nobody any the wiser -- worse than refusing
    them. These assert the delivery itself, not the status.
    """

    @staticmethod
    def customer_emails():
        """The download mails, as test_digital.DigitalDeliveryTest counts them."""
        return [m for m in mail.outbox if "Your download" in m.subject]

    def _buy_at(self, amount, session_id):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": amount})
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/session", id=session_id)
            self.client.post("/checkout")
        return Order.objects.get(stripe_session_id=session_id)

    def _settle_free(self, order):
        return self.deliver(self.event_body(
            order, payment_status="no_payment_required",
            amount_total=0, amount_subtotal=0,
            total_details={"amount_tax": 0, "amount_shipping": 0}))

    def test_a_rounded_down_order_emails_the_download(self):
        order = self._buy_at("0.75", "cs_free_rounded")

        self._settle_free(order)

        order.refresh_from_db()
        # The order really is the rounded one, not a 12.99 order that happened
        # to settle at zero. Without this the test passes on 3090eab, where
        # the amount is ignored and delivery runs anyway.
        self.assertEqual(order.items.get().unit_amount, 0)
        self.assertIsNotNone(order.digital_delivery_sent_at)
        self.assertEqual(order.digital_delivery_error, "")
        customer_email, = self.customer_emails()
        self.assertIn("download", customer_email.body.lower())

    def test_an_explicit_zero_order_emails_the_download(self):
        order = self._buy_at("0", "cs_free_zero")

        self._settle_free(order)

        order.refresh_from_db()
        self.assertEqual(order.items.get().unit_amount, 0)
        self.assertIsNotNone(order.digital_delivery_sent_at)
        self.assertEqual(len(self.customer_emails()), 1)

    def test_a_paid_order_still_emails_the_download(self):
        # CONTROL -- passes on 3090eab too, and is meant to. Guards against
        # over-fitting to the free path: the ordinary case must keep working,
        # and it settles as "paid" rather than "no_payment_required".
        order = self._buy_at("5.00", "cs_paid_five")

        self.deliver(self.event_body(order, amount_total=500))

        order.refresh_from_db()
        self.assertIsNotNone(order.digital_delivery_sent_at)
        self.assertEqual(len(self.customer_emails()), 1)

    def test_the_free_order_reaches_fulfilment_not_merely_paid_status(self):
        # Pins the distinction the whole class exists for: PAID alone would
        # be satisfied by a status write that delivered nothing.
        order = self._buy_at("0.10", "cs_free_check")

        self._settle_free(order)

        order.refresh_from_db()
        self.assertEqual(order.items.get().unit_amount, 0)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertIsNotNone(order.digital_delivery_sent_at)
        self.assertIsNotNone(order.notified_at)

    def test_the_buyer_receipt_reads_sensibly_for_a_free_order(self):
        order = self._buy_at("0.50", "cs_free_receipt")
        self._settle_free(order)

        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")

        body = squashed(response)
        self.assertIn("Total: $0.00", body)
        self.assertIn("&mdash; $0.00", body)
        # The preset must not appear anywhere on the receipt.
        self.assertNotIn("12.99", body)

    def test_the_owner_email_reads_sensibly_for_a_free_order(self):
        order = self._buy_at("0.50", "cs_free_owner")

        self._settle_free(order)

        owner_email, = self.order_emails()
        self.assertIn("@ 0.00", owner_email.body)
        self.assertIn("Total: 0.00", owner_email.body)
        self.assertIn("the amount the buyer paid", owner_email.body)
        self.assertNotIn("12.99", owner_email.body)


class ZeroCheckoutFailsGracefullyTest(CartTestBase):
    """"If Stripe is being difficult with $0" -- the owner's clause.

    A free order has no payment behind it, so a buyer who hits a Stripe
    problem here has nothing to retry. They must land somewhere that tells
    them how to get the book, not on a stack trace.
    """

    def _checkout_with_stripe_down(self, amount):
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": amount})
        with mock.patch("main.payments.stripe.checkout.Session.create",
                        side_effect=RuntimeError("Stripe is being difficult")):
            with self.assertLogs("main.views", level="ERROR"):
                return self.client.post("/checkout", follow=True)

    def test_a_failed_free_checkout_lands_on_the_cart_with_the_mailto(self):
        response = self._checkout_with_stripe_down("0.50")

        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, "/cart")
        self.assertContains(response, "would not set up this free order")
        self.assertContains(response, OWNER_MAILTO)

    def test_a_failed_free_checkout_leaves_no_pending_order(self):
        self._checkout_with_stripe_down("0")

        self.assertFalse(
            Order.objects.filter(status=Order.Status.PENDING).exists())

    def test_a_failed_paid_checkout_still_raises(self):
        # CONTROL -- passes on 3090eab too. Unchanged, and deliberately
        # so: a paid order failing is a payment problem the buyer can act
        # on, and swallowing it would hide a real outage behind a
        # friendly sentence. Only the zero-total path is made graceful.
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "5.00"})
        with mock.patch("main.payments.stripe.checkout.Session.create",
                        side_effect=RuntimeError("Stripe is down")):
            with self.assertLogs("main.views", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    self.client.post("/checkout")


class PwywCopyStaysOutOfTheFeedTest(TestCase):
    """Google needs a number it can parse, not a sentence about choosing.

    A regression guard, not new behaviour: get_feed_price/get_feed_description
    are already separate plain-text paths and this passes before the change
    too. It is here because the presentation copy above is one careless
    template include away from the feed.
    """

    fixtures = ["initial_products"]

    def test_the_feed_prices_the_pwyw_ebook_as_a_bare_number(self):
        response = self.client.get("/google_products.xml")
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("<g:price>12.99 USD</g:price>", body)
        for fragment in ("Pay what you want", "pay-what-you-want",
                         "suggested amount", CART_FLOOR):
            self.assertNotIn(fragment, body)


class PwywBundleTest(CartTestBase):
    """The print book and the e-book, in one order. The point of the change."""

    def test_the_print_book_and_the_ebook_check_out_together(self):
        self.client.post(f"/add-to-cart/{PRINT_PK}/1")
        self.client.post(
            f"/add-to-cart/{EBOOK_PK}/1", {"chosen_amount": "5.00"})

        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_bundle")
            response = self.client.post("/checkout")

        self.assertEqual(response.status_code, 302)
        params = create.call_args.kwargs
        self.assertEqual(len(params["line_items"]), 2)
        order = Order.objects.get(stripe_session_id="cs_bundle")
        amounts = sorted(item.unit_amount for item in order.items.all())
        self.assertEqual(
            amounts, sorted([500, Product.objects.get(pk=PRINT_PK).price]))
