"""Tests for pay-what-you-want pricing.

The Stripe Price a pay-what-you-want product mints, the checkout session
it may and may not appear in, and the zero-total order that results when
a customer takes the offer literally."""

from unittest import mock

from django.test import RequestFactory, TestCase

from main.models import Cart, CartProduct, Order, Product
from main.payments import Payments
from main.tests.base import (
    EBOOK_PK, BookAssetRootMixin, CartTestBase, OrderTestBase)


class PwywPriceTest(TestCase):
    """Part 3: the Stripe Price a pay-what-you-want product mints."""

    @mock.patch("main.payments.stripe.Price.create")
    def test_a_pwyw_price_uses_custom_unit_amount_with_no_minimum(self, create):
        create.return_value = {"id": "price_pwyw"}

        Payments.create_price("prod_x", 1500, pay_what_you_want=True)

        kwargs = create.call_args.kwargs
        self.assertEqual(
            kwargs["custom_unit_amount"], {"enabled": True, "preset": 1500})
        # Mutually exclusive with custom_unit_amount, and the floor is zero on
        # the owner's instruction, so neither may appear.
        self.assertNotIn("unit_amount", kwargs)
        self.assertNotIn("minimum", kwargs["custom_unit_amount"])

    @mock.patch("main.payments.stripe.Price.create")
    def test_a_fixed_price_is_unchanged(self, create):
        create.return_value = {"id": "price_fixed"}

        Payments.create_price("prod_x", 3000)

        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["unit_amount"], 3000)
        self.assertNotIn("custom_unit_amount", kwargs)

    @mock.patch("main.payments.stripe.Price.create")
    def test_a_recurring_pwyw_price_is_refused(self, create):
        # custom_unit_amount is payment-mode only. Refusing here says why;
        # silently minting a fixed recurring price would bill the suggestion
        # every year as though the buyer had agreed to it.
        with self.assertRaises(ValueError) as raised:
            Payments.create_price(
                "prod_x", 1500, interval="year", pay_what_you_want=True)

        self.assertIn("one-off payments", str(raised.exception))
        create.assert_not_called()

    @mock.patch("main.models.Payments")
    def test_a_pwyw_product_mints_a_pwyw_price(self, payments):
        payments.create_price.return_value = "price_pwyw"
        product = Product.objects.create(
            name="E-book", external_product_id="prod_ebook", price=1500,
            is_pwyw=True, delivery_type=Product.DeliveryTypes.DIGITAL)
        cart = Cart.objects.create()

        CartProduct.objects.create(cart=cart, product=product, quantity=1)

        self.assertTrue(
            payments.create_price.call_args.kwargs["pay_what_you_want"])

    @mock.patch("main.models.Payments")
    def test_an_ordinary_product_does_not(self, payments):
        payments.create_price.return_value = "price_fixed"
        product = Product.objects.create(
            name="Print", external_product_id="prod_print", price=3000)
        cart = Cart.objects.create()

        CartProduct.objects.create(cart=cart, product=product, quantity=1)

        self.assertFalse(
            payments.create_price.call_args.kwargs["pay_what_you_want"])


class PwywCheckoutTest(TestCase):
    """Part 3: adjustable_quantity and custom_unit_amount cannot coexist."""

    def setUp(self):
        self.factory = RequestFactory()

    def _cart(self, *products):
        cart = Cart.objects.create()
        for product in products:
            cart_product = CartProduct.objects.create(
                cart=cart, product=product, quantity=1,
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

    def _checkout(self, cart):
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_x")
            Payments.checkout(self.factory.get("/checkout"), cart)
        return create.call_args.kwargs

    def test_a_pwyw_line_carries_no_adjustable_quantity(self):
        # Stripe rejects the combination outright, so with this the e-book
        # simply cannot be bought.
        params = self._checkout(self._cart(self._pwyw()))

        item, = params["line_items"]
        self.assertNotIn("adjustable_quantity", item)
        self.assertEqual(params["mode"], "payment")

    def test_a_mixed_cart_keeps_adjustable_quantity_on_the_fixed_line_only(self):
        pwyw, fixed = self._pwyw(), self._fixed()

        params = self._checkout(self._cart(pwyw, fixed))

        by_price = {item["price"]: item for item in params["line_items"]}
        self.assertEqual(len(by_price), 2)
        self.assertNotIn(
            "adjustable_quantity", by_price[f"price_{pwyw.pk}"])
        self.assertEqual(
            by_price[f"price_{fixed.pk}"]["adjustable_quantity"],
            {"enabled": True})

    def test_a_pwyw_product_is_never_routed_into_subscription_mode(self):
        # The session mode is a property of the whole cart, so one
        # subscription line would drag the pay-what-you-want line into a mode
        # custom_unit_amount does not support.
        subscription = Product.objects.create(
            name="Support", external_product_id="prod_sub", price=10000,
            mode=Product.Modes.SUBSCRIPTION,
            delivery_type=Product.DeliveryTypes.SERVICE)
        cart = self._cart(self._pwyw(), subscription)

        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            with self.assertRaises(ValueError) as raised:
                Payments.checkout(self.factory.get("/checkout"), cart)

        self.assertIn("subscription mode", str(raised.exception))
        create.assert_not_called()


class ZeroTotalOrderTest(BookAssetRootMixin, OrderTestBase):
    """Part 3: a $0 pay-what-you-want order is still an order.

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
RECEIPT_NOTE = (
    "Pay what you want: shown at the suggested amount &mdash; see the "
    "order total for what was paid.")
CART_FLOOR = "nothing at all is a valid amount"


class PwywIsLabelledWhereverAPriceIsShownTest(CartTestBase):
    """Part 3: a bare price on a pay-what-you-want row reads as a fixed one.

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


class PwywReceiptTest(BookAssetRootMixin, OrderTestBase):
    """Part 3: the receipt line and the receipt total disagree on purpose.

    OrderItem.unit_amount snapshots Product.price, which on a
    pay-what-you-want row is the suggestion -- so a buyer who paid nothing
    sees a 12.99 line above a 0.00 total. The owner's email has explained
    that since order_summary_text; the buyer's copy had not.
    """

    def _paid_for_nothing(self):
        order = self.place_order(product_pk=EBOOK_PK, quantity=1)
        self.deliver(self.event_body(
            order, payment_status="no_payment_required",
            amount_total=0, amount_subtotal=0,
            total_details={"amount_tax": 0, "amount_shipping": 0}))
        order.refresh_from_db()
        return order

    def test_the_receipt_explains_the_line_the_buyer_was_not_charged(self):
        order = self._paid_for_nothing()

        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")

        body = squashed(response)
        self.assertEqual(response.status_code, 200)
        # The mismatch this note exists to explain: a 12.99 line, a 0.00 total.
        self.assertIn("12.99", body)
        self.assertIn("Total: $0.00", body)
        self.assertIn(RECEIPT_NOTE, body)

    def test_a_fixed_price_receipt_carries_no_pwyw_note(self):
        order = self.place_order(product_pk=100, quantity=1)
        self.deliver(self.event_body(order))

        response = self.client.get(
            f"/checkout/success?session_id={order.stripe_session_id}")

        self.assertNotIn(RECEIPT_NOTE, squashed(response))


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
