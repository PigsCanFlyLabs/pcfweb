"""Regression tests for stale cart prices missing Stripe tax_behavior."""

from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase, override_settings

from main.models import Cart, CartProduct, Order, OrderItem, Product
from main.views import CheckoutView


class StalePriceCheckoutFixTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _stale_non_pwyw_cart(self):
        product = Product.objects.create(
            name="Learning Spark",
            description="A book about Spark.",
            external_product_id="prod_test_spark",
            price=2500,
            mode=Product.Modes.PAYMENT,
            is_pwyw=False,
        )
        cart = Cart.objects.create()
        cart_product = CartProduct.objects.create(
            cart=cart,
            product=product,
            quantity=1,
            price_id="price_old_without_tax_behavior",
        )
        cart.products.add(cart_product)
        return cart, cart_product

    def _post_checkout(self, cart):
        request = self.factory.post("/checkout")
        request.user = AnonymousUser()
        view = CheckoutView()
        with mock.patch.object(view, "get_cart", return_value=cart):
            return view.start_checkout(request)

    @mock.patch("main.payments.stripe.checkout.Session.create")
    @mock.patch("main.payments.stripe.Price.create")
    def test_stale_non_pwyw_price_is_replaced_before_checkout(
        self, create_price, create_session
    ):
        """A pre-deploy non-PWYW price id is not sent to Stripe under tax."""
        cart, cart_product = self._stale_non_pwyw_cart()
        create_price.return_value = {"id": "price_new_with_tax_behavior"}

        def create_checkout(**params):
            self.assertEqual(
                params["line_items"][0]["price"], "price_new_with_tax_behavior")
            return mock.Mock(
                url="https://checkout.stripe.com/c/pay/cs_fixed",
                id="cs_fixed",
            )

        create_session.side_effect = create_checkout

        response = self._post_checkout(cart)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], "https://checkout.stripe.com/c/pay/cs_fixed")
        cart_product.refresh_from_db()
        self.assertEqual(cart_product.price_id, "price_new_with_tax_behavior")
        create_price.assert_called_once()
        price_params = create_price.call_args.kwargs
        self.assertEqual(price_params["product"], "prod_test_spark")
        self.assertEqual(price_params["unit_amount"], 2500)
        self.assertEqual(price_params["tax_behavior"], "exclusive")
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.items.get().price_id, "price_new_with_tax_behavior")

    @mock.patch("main.payments.stripe.checkout.Session.create")
    @override_settings(STRIPE_AUTOMATIC_TAX=False)
    def test_escape_hatch_leaves_stale_non_pwyw_price_untouched(
        self, create_session
    ):
        cart, cart_product = self._stale_non_pwyw_cart()
        create_session.return_value = mock.Mock(
            url="https://checkout.stripe.com/c/pay/cs_no_tax",
            id="cs_no_tax",
        )

        response = self._post_checkout(cart)

        self.assertEqual(response.status_code, 302)
        cart_product.refresh_from_db()
        self.assertEqual(cart_product.price_id, "price_old_without_tax_behavior")
        params = create_session.call_args.kwargs
        self.assertEqual(
            params["line_items"][0]["price"],
            "price_old_without_tax_behavior",
        )
        self.assertNotIn("automatic_tax", params)

    def test_order_item_price_id_is_untouched_by_price_refresh(self):
        product = Product.objects.create(
            name="Historical Book",
            description="Already purchased.",
            external_product_id="prod_historical",
            price=1000,
            mode=Product.Modes.PAYMENT,
        )
        order = Order.objects.create(amount_total=1000, status=Order.Status.PAID)
        order_item = OrderItem.objects.create(
            order=order,
            product=product,
            product_name=product.name,
            unit_amount=1000,
            quantity=1,
            snapshot_quantity=1,
            price_id="price_historical_must_never_change",
        )
        cart = Cart.objects.create()
        cart_product = CartProduct.objects.create(
            cart=cart,
            product=product,
            quantity=1,
            price_id="price_old_without_tax_behavior",
        )

        with mock.patch("main.payments.stripe.Price.create") as create_price:
            create_price.return_value = {"id": "price_new_with_tax_behavior"}
            cart_product.refresh_pwyw_price()

        order_item.refresh_from_db()
        self.assertEqual(order_item.price_id, "price_historical_must_never_change")
