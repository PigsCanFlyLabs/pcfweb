"""Tests for Stripe tax codes, on checkout and on products."""

from io import StringIO
from unittest import mock

import stripe
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings

from main.models import Cart, CartProduct, Product
from main.payments import Payments


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

    @mock.patch("main.payments.stripe.checkout.Session.create")
    def test_missing_tax_behavior_error_is_detected_and_reported(self, create_session):
        """Regression test for production bug: prices without tax_behavior.

        When STRIPE_AUTOMATIC_TAX is enabled, Stripe requires prices to have
        tax_behavior set. Prices created without it trigger a checkout error
        that says the price "does not have a tax behavior set". This should be
        caught as a tax configuration error and given a helpful message.
        """
        create_session.side_effect = stripe.InvalidRequestError(
            "The price `price_123` does not have a tax behavior set, which is "
            "required for automatic tax computation.",
            None,
        )

        with mock.patch("main.payments.logger.error") as log_error:
            with self.assertRaises(stripe.InvalidRequestError) as error:
                self._checkout()

        # The error should be detected as a tax configuration issue
        self.assertIn("tax", str(error.exception).lower())
        log_error.assert_called_once()
        # The logged message should help diagnose the problem
        self.assertIn("tax", log_error.call_args.args[0].lower())
        # Should not retry (not a coupon error)
        create_session.assert_called_once()

    @mock.patch("main.payments.stripe.Price.create")
    def test_create_price_adds_tax_behavior_when_automatic_tax_enabled(self, create_price):
        """Prices minted with automatic_tax enabled must have tax_behavior."""
        create_price.return_value = {"id": "price_test123"}

        price_id = Payments.create_price("prod_test", 1000)

        self.assertEqual(price_id, "price_test123")
        create_price.assert_called_once()
        params = create_price.call_args.kwargs
        self.assertEqual(params["tax_behavior"], "exclusive")
        self.assertEqual(params["unit_amount"], 1000)
        self.assertEqual(params["product"], "prod_test")

    @override_settings(STRIPE_AUTOMATIC_TAX=False)
    @mock.patch("main.payments.stripe.Price.create")
    def test_create_price_omits_tax_behavior_when_automatic_tax_disabled(self, create_price):
        """When automatic tax is off, tax_behavior is optional."""
        create_price.return_value = {"id": "price_test456"}

        price_id = Payments.create_price("prod_test", 1000)

        self.assertEqual(price_id, "price_test456")
        create_price.assert_called_once()
        params = create_price.call_args.kwargs
        self.assertNotIn("tax_behavior", params)
        self.assertEqual(params["unit_amount"], 1000)


class BackfillStripeProductTaxCodesTest(TestCase):
    def _product(
        self,
        name,
        external_product_id,
        tax_code=Product.TaxTypes.BOOKS,
    ):
        return Product.objects.create(
            name=name,
            description=f"{name} description.",
            price=1000,
            external_product_id=external_product_id,
            tax_code=tax_code,
        )

    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.modify")
    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.retrieve")
    def test_dry_run_performs_no_writes(self, retrieve, modify):
        self._product("Book", "prod_book", Product.TaxTypes.BOOKS)
        retrieve.return_value = {"tax_code": Product.TaxTypes.GOODS}
        out = StringIO()

        call_command("backfill_stripe_product_tax_codes", stdout=out)

        modify.assert_not_called()
        self.assertIn(
            "DRY RUN: no changes were written to Stripe. Re-run with --apply to write.",
            out.getvalue(),
        )
        self.assertIn("DRY-RUN would-change", out.getvalue())
        self.assertIn("SUMMARY examined=1 would-change=1", out.getvalue())

    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.modify")
    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.retrieve")
    def test_apply_modifies_only_when_tax_code_differs(self, retrieve, modify):
        self._product("Matching book", "prod_matching", Product.TaxTypes.BOOKS)
        self._product("Wrong service", "prod_wrong", Product.TaxTypes.SERVICES)
        retrieve.side_effect = [
            {"tax_code": Product.TaxTypes.BOOKS},
            {"tax_code": Product.TaxTypes.GOODS},
        ]
        out = StringIO()

        call_command("backfill_stripe_product_tax_codes", "--apply", stdout=out)

        modify.assert_called_once_with(
            "prod_wrong",
            tax_code=Product.TaxTypes.SERVICES,
        )
        self.assertIn("OK tax-code-matches", out.getvalue())
        self.assertNotIn("DRY RUN: no changes were written", out.getvalue())
        self.assertIn("SUMMARY examined=2 changed=1", out.getvalue())

    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.modify")
    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.retrieve")
    def test_backfill_reads_tax_code_from_object_attribute(self, retrieve, modify):
        class StripeProduct:
            tax_code = Product.TaxTypes.GOODS

        self._product("Attribute response", "prod_attribute", Product.TaxTypes.BOOKS)
        retrieve.return_value = StripeProduct()
        out = StringIO()

        call_command("backfill_stripe_product_tax_codes", "--apply", stdout=out)

        modify.assert_called_once_with(
            "prod_attribute",
            tax_code=Product.TaxTypes.BOOKS,
        )
        self.assertIn("SUMMARY examined=1 changed=1", out.getvalue())

    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.modify")
    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.retrieve")
    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.logger")
    def test_modify_error_does_not_print_success_and_is_counted(self, logger, retrieve, modify):
        self._product("Modify fails", "prod_modify_fails", Product.TaxTypes.BOOKS)
        retrieve.return_value = {"tax_code": Product.TaxTypes.GOODS}
        modify.side_effect = stripe.APIConnectionError("connection failed")
        out = StringIO()
        err = StringIO()

        call_command(
            "backfill_stripe_product_tax_codes",
            "--apply",
            stdout=out,
            stderr=err,
        )

        modify.assert_called_once_with(
            "prod_modify_fails",
            tax_code=Product.TaxTypes.BOOKS,
        )
        self.assertNotIn("APPLY changed", out.getvalue())
        self.assertIn("APPLY failed-change", err.getvalue())
        self.assertIn("ERROR stripe-product-tax-code", err.getvalue())
        logger.error.assert_called_once()
        self.assertIn("SUMMARY examined=1 changed=1", out.getvalue())
        self.assertIn("errored=1", out.getvalue())

    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.modify")
    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.retrieve")
    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.logger")
    def test_stripe_error_does_not_abort_and_is_counted(self, logger, retrieve, modify):
        self._product("Broken", "prod_broken", Product.TaxTypes.BOOKS)
        self._product("Still checked", "prod_checked", Product.TaxTypes.SERVICES)
        retrieve.side_effect = [
            stripe.InvalidRequestError("No such product", "id"),
            {"tax_code": Product.TaxTypes.GOODS},
        ]
        out = StringIO()
        err = StringIO()

        call_command(
            "backfill_stripe_product_tax_codes",
            "--apply",
            stdout=out,
            stderr=err,
        )

        modify.assert_called_once_with(
            "prod_checked",
            tax_code=Product.TaxTypes.SERVICES,
        )
        self.assertIn("ERROR stripe-product-tax-code", err.getvalue())
        logger.error.assert_called_once()
        self.assertIn("SUMMARY examined=1 changed=1", out.getvalue())
        self.assertIn("errored=1", out.getvalue())

    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.modify")
    @mock.patch("main.management.commands.backfill_stripe_product_tax_codes.stripe.Product.retrieve")
    def test_missing_external_id_and_local_tax_code_are_skipped(self, retrieve, modify):
        Product.objects.bulk_create(
            [
                Product(
                    name="Never synced",
                    description="No Stripe id.",
                    price=1000,
                    external_product_id="",
                    tax_code=Product.TaxTypes.BOOKS,
                ),
                Product(
                    name="No local tax code",
                    description="Blank local tax code.",
                    price=1000,
                    external_product_id="prod_blank_tax",
                    tax_code="",
                ),
            ]
        )
        out = StringIO()

        call_command("backfill_stripe_product_tax_codes", stdout=out)

        retrieve.assert_not_called()
        modify.assert_not_called()
        self.assertIn("skipped-no-external-id=1", out.getvalue())
        self.assertIn("skipped-no-local-code=1", out.getvalue())
