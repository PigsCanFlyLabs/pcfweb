import logging
from types import SimpleNamespace

import stripe
from django.conf import settings
from django.urls import reverse
from typing import Literal, Optional

logger = logging.getLogger(__name__)

COUPON_ERROR_CODES = {
    "coupon_expired",
    "promotion_code_customer_missing_first_time",
    "promotion_code_customer_not_first_time",
}
TAX_CONFIGURATION_ERROR = (
    "Stripe automatic tax checkout failed. Stripe Tax must be activated in "
    "the Stripe Dashboard with an origin address set, and tax behavior/codes "
    "configured on Products. If the site owner intentionally needs to disable "
    "automatic tax temporarily, set STRIPE_AUTOMATIC_TAX=false."
)


SHIPPING_RATE_ERROR = (
    "Stripe rejected a configured shipping rate. Shipping rate ids are "
    "livemode-scoped, so a rate created in test mode does not exist under a "
    "live key (and vice versa). Check STRIPE_SHIPPING_RATES against the "
    "Stripe Dashboard for the key this deployment is using."
)


class Payments:
    # mypy can't find this but it does exist. idk.
    API_KEY = settings.STRIPE_API_KEY # type: ignore
    stripe.api_key = API_KEY

    # Bound every call made through the module-level `stripe.*` helpers. The
    # SDK ships an 80s timeout and retries twice, so a Stripe outage could
    # otherwise hold a request open for four minutes -- long past the point
    # gunicorn kills the worker out from under it. Two of these calls sit on
    # the add-to-cart path (Product.create, Price.create) and one on checkout
    # (Session.create), so this is a customer-facing budget, not a background
    # one.
    # Same story as API_KEY above: django-configurations settings are opaque
    # to the plugin.
    stripe.default_http_client = stripe.RequestsClient(
        timeout=settings.STRIPE_TIMEOUT)  # type: ignore[misc]

    @classmethod
    def create_product(cls, name: str, description: str, price: int, currency: str = "usd",
                       tax_code: Optional[str] = None) -> str:
        tax_code = (tax_code or "").strip()
        if tax_code:
            product = stripe.Product.create(
                name=name, description=description, tax_code=tax_code)
        else:
            product = stripe.Product.create(
                name=name, description=description)
        return product['id']

    @classmethod
    def create_price(cls, product_id: str, price: int, currency: str = "usd",
                     interval: Optional[Literal["day", "week", "month", "year"]] = None,
                     pay_what_you_want: bool = False) -> str:
        product_price = None
        if pay_what_you_want:
            if interval is not None:
                # custom_unit_amount is documented as payment-mode only.
                # Stripe would reject this anyway; refusing here says why.
                raise ValueError(
                    "A pay-what-you-want price uses Stripe's "
                    "custom_unit_amount, which only works for one-off "
                    "payments, so it cannot be recurring. Set the product's "
                    "mode to payment or clear is_pwyw.")
            # No `minimum`: the owner's decision is an explicit floor of zero,
            # so any amount down to nothing is accepted. `preset` is what the
            # buyer sees pre-filled, i.e. Product.price as a suggestion. Note
            # unit_amount must be absent -- the two are mutually exclusive.
            product_price = stripe.Price.create(
                currency=currency, product=product_id,
                custom_unit_amount={"enabled": True, "preset": price},
            )
            return product_price['id']
        if interval is None:
            product_price = stripe.Price.create(
                unit_amount=price, currency=currency, product=product_id
            )
        else:
            product_price = stripe.Price.create(
                unit_amount=price, currency=currency, product=product_id,
                recurring = {"interval": interval}
            )
        return product_price['id']

    # Seconds. The SDK's default is ~80, far longer than Stripe's own webhook
    # delivery window: a hung connection would pin a gunicorn worker until
    # Stripe had already given up and queued a re-delivery.
    LINE_ITEM_TIMEOUT = 5

    @classmethod
    def list_line_items(cls, session_id: str, limit: int = 100):
        """What Stripe actually billed for a Checkout session.

        Called from inside a webhook response, so it is bounded on both axes:
        no retries and a short timeout. The caller treats any failure as "keep
        the snapshot" rather than an error, so giving up quickly is strictly
        better than holding the response open.

        The timeout lives on the HTTP client rather than on the request, so
        this needs its own client -- a module-level one would drag every other
        Stripe call in the app down to the same budget, and checkout legitimately
        wants longer. Built per call rather than cached: paid orders are rare
        enough that the pooling would not pay for the shared mutable state.
        """
        client = stripe.StripeClient(
            api_key=cls.API_KEY or "",
            max_network_retries=0,
            http_client=stripe.RequestsClient(
                timeout=cls.LINE_ITEM_TIMEOUT),
        )
        return client.checkout.sessions.line_items.list(
            session_id, {"limit": limit})

    @classmethod
    def _pwyw_cart_message(cls, products) -> Optional[str]:
        """Why these line items cannot share a checkout with a PWYW line."""
        from main.models import Product
        pwyw_lines = [
            cart_product for cart_product in products
            if cart_product.product.is_pwyw
        ]
        if not pwyw_lines:
            return None
        product_modes = [cart_product.product.mode for cart_product in products]
        mode = "subscription"
        if all(m == Product.Modes.PAYMENT for m in product_modes):
            mode = "payment"
        if mode == "subscription":
            return (
                "This cart mixes a pay-what-you-want product with a "
                "subscription, so the whole session would have to be created "
                "in subscription mode -- which Stripe's custom_unit_amount "
                "does not support. They have to be bought separately."
            )
        if any(cart_product.quantity != 1 for cart_product in pwyw_lines):
            return (
                "Stripe requires pay-what-you-want items to be checked out "
                "one at a time. Remove that item from the cart and add it "
                "again once."
            )
        if len(products) != 1:
            other_names = ", ".join(
                cart_product.product.name for cart_product in products
                if not cart_product.product.is_pwyw
            )
            if other_names:
                return (
                    "Stripe requires a pay-what-you-want item to be the only "
                    "line in its checkout. Remove these other items and buy "
                    f"them separately: {other_names}."
                )
            return (
                "Stripe requires a pay-what-you-want item to be the only "
                "line in its checkout. Remove the other pay-what-you-want "
                "items and buy them separately."
            )
        return None

    @classmethod
    def pwyw_checkout_blocker(cls, cart, coupon=None) -> Optional[str]:
        """Why this cart cannot be sent to Stripe with a PWYW item, if any."""
        products = list(cart.products.select_related('product'))
        return cls._pwyw_cart_message(products)

    @classmethod
    def pwyw_add_to_cart_blocker(cls, cart, product, quantity) -> Optional[str]:
        """Why adding *product* x *quantity* would make the cart invalid."""
        products = list(cart.products.select_related('product'))
        prospective = []
        matched = False
        for cart_product in products:
            next_quantity = cart_product.quantity
            if cart_product.product_id == product.pk:
                next_quantity += quantity
                matched = True
            prospective.append(
                SimpleNamespace(product=cart_product.product, quantity=next_quantity))
        if not matched:
            prospective.append(SimpleNamespace(product=product, quantity=quantity))
        return cls._pwyw_cart_message(prospective)

    @classmethod
    def pwyw_coupon_warning(cls, cart, coupon=None) -> Optional[str]:
        """Coupon warning to show while still allowing checkout."""
        if coupon is None:
            return None
        products = list(cart.products.select_related('product'))
        if any(cart_product.product.is_pwyw for cart_product in products):
            return (
                "Coupon and promotion-code discounts do not apply to "
                "pay-what-you-want items, so the code was removed and "
                "checkout will continue without it."
            )
        return None

    @classmethod
    def checkout(cls, request, cart, coupon=None, order=None):
        """Start a Stripe Checkout session; returns (url, session_id).

        `order` is the local PENDING order this session is paying for. Its id
        rides along as client_reference_id (and in metadata) because that is
        the only thing tying a Stripe session back to anything of ours -- the
        webhook has no session cookie and so no way to find the cart.
        """
        from main.models import Product
        if cls.pwyw_coupon_warning(cart, coupon=coupon) is not None:
            coupon = None
        pwyw_blocker = cls.pwyw_checkout_blocker(cart, coupon=coupon)
        if pwyw_blocker is not None:
            raise ValueError(pwyw_blocker)
        products = list(cart.products.select_related('product'))
        items = []
        for cart_product in products:
            item = {
                'price': cart_product.price_id,
                'quantity': cart_product.quantity,
            }
            # Stripe rejects adjustable_quantity on a line whose Price carries
            # a custom_unit_amount -- the two are mutually exclusive at session
            # creation. Setting it unconditionally, as this used to, means a
            # pay-what-you-want product cannot be checked out at all.
            if not cart_product.product.is_pwyw:
                item["adjustable_quantity"] = {"enabled": True}
            items.append(item)
        extras = {}
        if coupon is not None:
            extras["discounts"] = [{"coupon": coupon}]
        mode = "subscription"
        product_modes = [cart_product.product.mode for cart_product in products]
        if all(m == Product.Modes.PAYMENT for m in product_modes):
            mode = "payment"

        # Shipping is about whether anything actually has to be posted, not
        # about how it is billed. Asking the buyer for a mailing address so
        # they can be offered media mail on a PDF is what the old
        # payment-mode test did.
        has_physical = any(
            cart_product.product.is_physical_good()
            for cart_product in products)
        if has_physical and mode == "payment":
            # Stripe Checkout does not support shipping options with
            # subscriptions, so a mixed cart gets free shipping for now.
            #
            # Livemode-scoped ids, so they come from settings rather than
            # being hardcoded here; see STRIPE_SHIPPING_RATES.
            shipping_rates = getattr(settings, "STRIPE_SHIPPING_RATES", [])
            if shipping_rates:
                extras["shipping_options"] = [
                    {"shipping_rate": rate} for rate in shipping_rates]

        if has_physical:
            extras["shipping_address_collection"] = {"allowed_countries": ["US", "CA"]}

        if order is not None:
            extras["client_reference_id"] = str(order.pk)
            extras["metadata"] = {"order_id": str(order.pk)}

        reserved_tax_keys = {"automatic_tax", "billing_address_collection"}
        shadowed_keys = reserved_tax_keys & extras.keys()
        if shadowed_keys:
            raise ValueError(
                "Checkout extras must not override tax settings: "
                f"{', '.join(sorted(shadowed_keys))}")

        automatic_tax_enabled = settings.STRIPE_AUTOMATIC_TAX
        if not automatic_tax_enabled:
            logger.warning(
                "Stripe automatic tax is disabled by STRIPE_AUTOMATIC_TAX; "
                "checkout sessions will be created without automatic tax.")

        session_params = {
            **extras,
            "line_items": items,
            "mode": mode,
            # The session id placeholder is substituted by Stripe on the
            # redirect. It is what lets the success page find the order, and
            # what stops a bare cross-site GET of that URL from emptying
            # somebody's cart -- see CheckoutSuccessView.
            "success_url": (
                request.build_absolute_uri(reverse('checkout-success'))
                + "?session_id={CHECKOUT_SESSION_ID}"),
            "cancel_url": request.build_absolute_uri(reverse('checkout-cancel')),
        }
        if automatic_tax_enabled:
            session_params.update({
                "automatic_tax": {
                    "enabled": True,
                },
                "billing_address_collection": "required",
            })

        # Fall back for invalid coupons
        try:
            checkout = stripe.checkout.Session.create(**session_params)
            return checkout.url, checkout.id
        except stripe.InvalidRequestError as error:
            if cls._is_tax_configuration_error(error):
                logger.error(TAX_CONFIGURATION_ERROR, exc_info=True)
                raise stripe.InvalidRequestError(
                    TAX_CONFIGURATION_ERROR,
                    error.param,
                    code=error.code,
                    http_body=error.http_body,
                    http_status=error.http_status,
                    json_body=error.json_body,
                    headers=error.headers,
                ) from error
            if cls._is_shipping_rate_error(error):
                # Stripe reports this as a bare resource_missing on a
                # shipping_options index, which reads like a transient Stripe
                # problem rather than the config mistake it almost always is.
                # Say which it is, because this breaks *every* physical
                # checkout and nothing else.
                logger.error(SHIPPING_RATE_ERROR, exc_info=True)
                raise stripe.InvalidRequestError(
                    SHIPPING_RATE_ERROR,
                    error.param,
                    code=error.code,
                    http_body=error.http_body,
                    http_status=error.http_status,
                    json_body=error.json_body,
                    headers=error.headers,
                ) from error
            # Only retry when a coupon could have been the problem.
            if "discounts" not in session_params or not cls._is_coupon_error(error):
                raise
            retry_params = {**session_params}
            retry_params.pop("discounts", None)
            checkout = stripe.checkout.Session.create(**retry_params)
            return checkout.url, checkout.id

    @staticmethod
    def _is_coupon_error(error: stripe.InvalidRequestError) -> bool:
        param = error.param or ""
        code = error.code or ""
        if "coupon" in param or "discount" in param:
            return True
        if code in COUPON_ERROR_CODES:
            return True
        return code == "resource_missing" and not param

    @staticmethod
    def _is_tax_configuration_error(error: stripe.InvalidRequestError) -> bool:
        return error.param == "automatic_tax" or error.code == "stripe_tax_inactive"

    @staticmethod
    def _is_shipping_rate_error(error: stripe.InvalidRequestError) -> bool:
        # Stripe points param at the offending entry, e.g.
        # "shipping_options[0][shipping_rate]".
        return "shipping_rate" in (error.param or "")
