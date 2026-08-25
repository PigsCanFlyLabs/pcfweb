import logging
from typing import Literal, Optional

import stripe
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

logger = logging.getLogger(__name__)
TaxBehavior = Literal["exclusive", "inclusive", "unspecified"]

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


# The free rate is built inline rather than being a fifth STRIPE_SHIPPING_RATES
# id, and that is the whole point of it: a shr_... is livemode-scoped, so a
# dashboard rate has to be created twice and configured per environment before
# the offer works anywhere -- and until it is, the site advertises free
# shipping that checkout does not offer. Inline shipping_rate_data needs
# nothing created in advance and is identical under a test and a live key.
FREE_SHIPPING_DISPLAY_NAME = "Free shipping"

# Stripe's tax code for shipping. Named in the Checkout Session API docs and
# required here because automatic tax is on: a rate with no tax behavior is
# rejected by a session that computes tax. The amount is zero, so this decides
# nothing about what is charged -- it decides whether the session is accepted.
SHIPPING_TAX_CODE = "txcd_92010001"


def free_shipping_option(currency: str = "usd") -> dict:
    """The inline Stripe shipping option for a cart that has earned it."""
    return {
        "shipping_rate_data": {
            "type": "fixed_amount",
            "display_name": FREE_SHIPPING_DISPLAY_NAME,
            "fixed_amount": {"amount": 0, "currency": currency},
            "tax_behavior": stripe_tax_behavior(),
            "tax_code": SHIPPING_TAX_CODE,
        },
    }


def stripe_tax_behavior() -> TaxBehavior:
    value = str(getattr(settings, "STRIPE_TAX_BEHAVIOR", "exclusive"))
    if value == "exclusive":
        return "exclusive"
    if value == "inclusive":
        return "inclusive"
    if value == "unspecified":
        return "unspecified"
    raise ImproperlyConfigured(
        "STRIPE_TAX_BEHAVIOR must be 'exclusive', 'inclusive', or "
        "'unspecified'.")


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
        """Mint an ordinary fixed Price for `price` cents.

        Pay-what-you-want no longer takes a different path. The amount the
        buyer named is collected on our own product page, validated by
        main.models.parse_pwyw_amount, and arrives here as a number like any
        other -- so a pay-what-you-want line is a fixed price that happens to
        have been chosen by the customer.

        What that buys is everything Stripe's custom_unit_amount forbade: a
        second line item in the same session, a quantity above one, an
        adjustable_quantity, a discount, and subscription mode.
        """
        # When automatic tax is enabled, Stripe requires prices to have
        # tax_behavior set. See STRIPE_TAX_BEHAVIOR in settings.py for the
        # business decision on exclusive vs. inclusive tax display.
        automatic_tax_enabled = bool(getattr(settings, "STRIPE_AUTOMATIC_TAX", True))
        tax_behavior = stripe_tax_behavior()

        if interval is None:
            if automatic_tax_enabled:
                product_price = stripe.Price.create(
                    unit_amount=price,
                    currency=currency,
                    product=product_id,
                    tax_behavior=tax_behavior,
                )
            else:
                product_price = stripe.Price.create(
                    unit_amount=price,
                    currency=currency,
                    product=product_id,
                )
        else:
            if automatic_tax_enabled:
                product_price = stripe.Price.create(
                    unit_amount=price,
                    currency=currency,
                    product=product_id,
                    recurring={"interval": interval},
                    tax_behavior=tax_behavior,
                )
            else:
                product_price = stripe.Price.create(
                    unit_amount=price,
                    currency=currency,
                    product=product_id,
                    recurring={"interval": interval},
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

    # A pay-what-you-want line used to be refused in four shapes -- mixed with
    # any other line, at a quantity above one, alongside a discount, and in
    # subscription mode -- because Stripe enforces all four on a Price with a
    # custom_unit_amount. It no longer has one. Verified against the live test
    # API: a fixed Price at the buyer's chosen amount is accepted in a session
    # with a second line item, with quantity 3, with adjustable_quantity, with
    # a coupon, and as a recurring price in subscription mode. The refusals
    # went with the constraint that caused them.

    @classmethod
    def checkout(cls, request, cart, coupon=None, order=None):
        """Start a Stripe Checkout session; returns (url, session_id).

        `order` is the local PENDING order this session is paying for. Its id
        rides along as client_reference_id (and in metadata) because that is
        the only thing tying a Stripe session back to anything of ours -- the
        webhook has no session cookie and so no way to find the cart.
        """
        from main.models import Product
        products = list(cart.products.select_related('product'))
        items = []
        for cart_product in products:
            item = {
                'price': cart_product.price_id,
                'quantity': cart_product.quantity,
                'adjustable_quantity': {"enabled": True},
            }
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
            shipping_options = [
                {"shipping_rate": rate} for rate in shipping_rates]
            # First in the list, because Stripe preselects the first option:
            # a buyer who has earned free shipping should not have to notice
            # it, and the paid rates stay available underneath for anyone who
            # wants the faster one. Cart.qualifies_for_free_shipping() is the
            # single definition of the offer -- the cart page advertises it
            # from the same method, so the page cannot promise a rate this
            # session will not carry.
            if cart.qualifies_for_free_shipping(products):
                shipping_options.insert(0, free_shipping_option())
            if shipping_options:
                extras["shipping_options"] = shipping_options
            # Stripe fixes shipping_options at session creation and never
            # recomputes them, but adjustable_quantity would let the buyer
            # change the total on Stripe's own page -- a $39 cart bumped to
            # two copies pays the paid rate on an order the site said ships
            # free, and a $40 cart dropped to one $15 copy keeps a free rate
            # it no longer earns. So while the offer is live, quantities are
            # settled in the cart: the stepper is the price of a threshold
            # Stripe cannot re-check. Digital and subscription carts carry no
            # shipping and keep it, as does everything when the offer is off.
            if getattr(settings, "FREE_SHIPPING_ENABLED", True):
                for item in items:
                    item.pop('adjustable_quantity', None)

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

        automatic_tax_enabled = bool(getattr(settings, "STRIPE_AUTOMATIC_TAX", True))
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
        # Stripe Tax not activated (stable error.code from Stripe).
        if error.param == "automatic_tax" or error.code == "stripe_tax_inactive":
            return True
        # Price missing tax_behavior (required when automatic_tax is enabled).
        # Stripe provides no stable error.code for this case, so this branch
        # matches the message. It only changes diagnostic logging and re-raises;
        # it never retries with tax disabled.
        message = str(error).lower()
        if "tax" in message and "behavior" in message:
            return True
        return False

    @staticmethod
    def _is_shipping_rate_error(error: stripe.InvalidRequestError) -> bool:
        # Stripe points param at the offending entry, e.g.
        # "shipping_options[0][shipping_rate]".
        return "shipping_rate" in (error.param or "")
