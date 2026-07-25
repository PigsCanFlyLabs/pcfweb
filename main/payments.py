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


class Payments:
    # mypy can't find this but it does exist. idk.
    API_KEY = settings.STRIPE_API_KEY # type: ignore
    stripe.api_key = API_KEY

    @classmethod
    def create_product(cls, name: str, description: str, price: int, currency: str = "usd") -> str:
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

    @classmethod
    def checkout(cls, request, cart, coupon=None):
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
            # options
            shipping_options = map(lambda x: {"shipping_rate": x},
                                   [
                                       "shr_0MJrPInkDnSOC1s7tidX8eMN", # YOLO
                                       "shr_0MJrIYnkDnSOC1s7fthNSlhb", # sf only
                                       "shr_0MJrL4nkDnSOC1s7cPSy15CO", #media mail
                                       "shr_0MNOZrnkDnSOC1s7TSLZig6Z", #faster
                                   ])
            extras["shipping_options"] = list(shipping_options)

        if any(map (lambda x: x == Product.Modes.PAYMENT, product_modes)) or mode == "payment":
            extras["shipping_address_collection"] = {"allowed_countries": ["US", "CA"]}

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
            "success_url": request.build_absolute_uri(
                reverse('checkout-success')),
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
            return checkout.url
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
            # Only retry when a coupon could have been the problem.
            if "discounts" not in session_params or not cls._is_coupon_error(error):
                raise
            retry_params = {**session_params}
            retry_params.pop("discounts", None)
            checkout = stripe.checkout.Session.create(**retry_params)
            return checkout.url

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
