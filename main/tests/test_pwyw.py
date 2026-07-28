"""Tests for pay-what-you-want pricing.

The Stripe Price a pay-what-you-want product mints, the checkout session
it may and may not appear in, and the zero-total order that results when
a customer takes the offer literally."""

from unittest import mock

from django.test import RequestFactory, TestCase

from main.models import Cart, CartProduct, Order, Product
from main.payments import Payments
from main.tests.base import EBOOK_PK, BookAssetRootMixin, OrderTestBase


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
    """Part 3: Stripe-enforced PWYW checkout constraints."""

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

    def _checkout(self, cart, coupon=None):
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            create.return_value = mock.Mock(
                url="https://checkout.example/s", id="cs_x")
            Payments.checkout(self.factory.get("/checkout"), cart, coupon=coupon)
        return create.call_args.kwargs

    def _checkout_error(self, cart, coupon=None):
        with mock.patch("main.payments.stripe.checkout.Session.create") as create:
            with self.assertRaises(ValueError) as raised:
                Payments.checkout(self.factory.get("/checkout"), cart, coupon=coupon)
        return str(raised.exception), create

    def test_a_pwyw_line_carries_no_adjustable_quantity(self):
        # Stripe rejects the combination outright, so with this the e-book
        # simply cannot be bought.
        params = self._checkout(self._cart((self._pwyw(), 1)))

        item, = params["line_items"]
        self.assertNotIn("adjustable_quantity", item)
        self.assertEqual(params["mode"], "payment")

    def test_a_mixed_cart_is_refused_before_stripe_checkout(self):
        pwyw, fixed = self._pwyw(), self._fixed()
        message, create = self._checkout_error(
            self._cart((pwyw, 1), (fixed, 1)))

        self.assertIn("only line in its checkout", message)
        create.assert_not_called()

    def test_a_pwyw_line_with_quantity_above_one_is_refused(self):
        message, create = self._checkout_error(self._cart((self._pwyw(), 2)))

        self.assertIn("checked out one at a time", message)
        create.assert_not_called()

    def test_a_pwyw_line_cannot_be_checked_out_with_a_coupon(self):
        message, create = self._checkout_error(
            self._cart((self._pwyw(), 1)), coupon="coupon_sale")

        self.assertIn("does not allow coupon", message)
        create.assert_not_called()

    def test_a_pwyw_product_is_never_routed_into_subscription_mode(self):
        # The session mode is a property of the whole cart, so one
        # subscription line would drag the pay-what-you-want line into a mode
        # custom_unit_amount does not support.
        subscription = Product.objects.create(
            name="Support", external_product_id="prod_sub", price=10000,
            mode=Product.Modes.SUBSCRIPTION,
            delivery_type=Product.DeliveryTypes.SERVICE)
        cart = self._cart((self._pwyw(), 1), (subscription, 1))

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
