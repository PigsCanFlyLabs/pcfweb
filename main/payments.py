import logging

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
                     interval: Optional[Literal["day", "week", "month", "year"]] = None) -> str:
        product_price = None
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
    def checkout(cls, request, cart, coupon=None, order=None):
        """Start a Stripe Checkout session; returns (url, session_id).

        `order` is the local PENDING order this session is paying for. Its id
        rides along as client_reference_id (and in metadata) because that is
        the only thing tying a Stripe session back to anything of ours -- the
        webhook has no session cookie and so no way to find the cart.
        """
        from main.models import Product
        products = cart.products.all()
        items = [
            {
                'price': product.price_id,
                'quantity': product.quantity,
                "adjustable_quantity": {"enabled": True},
            } for product in products
        ]
        # Add shipping if only physical products. Stripe checkout does not support
        # shipping with subscriptions so for now free shipping with any subscription
        extras = {}
        if coupon is not None:
            extras["discounts"] = [{"coupon": coupon}]
        mode = "subscription"
        product_modes = list(map(lambda x: x.product.mode, products))
        if all(map (lambda x: x == Product.Modes.PAYMENT, product_modes)):
            mode="payment"
            # Livemode-scoped ids, so they come from settings rather than
            # being hardcoded here; see STRIPE_SHIPPING_RATES.
            shipping_rates = getattr(settings, "STRIPE_SHIPPING_RATES", [])
            if shipping_rates:
                extras["shipping_options"] = [
                    {"shipping_rate": rate} for rate in shipping_rates]

        if any(map (lambda x: x == Product.Modes.PAYMENT, product_modes)) or mode == "payment":
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
