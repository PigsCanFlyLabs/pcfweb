"""Template context shared by every page.

Deliberately tiny. A context processor runs on every render, so anything that
queries the database or calls out to Stripe does not belong here; these are
settings reads.
"""

from typing import Any, Dict

from django.conf import settings


def free_shipping(request) -> Dict[str, Any]:
    """The free shipping offer, for the components that advertise it.

    The threshold reaches the templates from the same setting
    Cart.qualifies_for_free_shipping() and Payments.checkout() read, rather
    than being written into the copy: a number typed into a template goes on
    advertising an offer after FREE_SHIPPING_THRESHOLD has moved, and the
    buyer meets a different rate at the till.

    A processor rather than two view context additions because
    components/shipping-notice.html is included from the cart and from every
    product page, and a missing variable renders as empty -- so the failure
    mode of the view-by-view approach is an advertisement that quietly loses
    its price on whichever page was forgotten.
    """
    enabled = bool(getattr(settings, "FREE_SHIPPING_ENABLED", True))
    threshold = int(getattr(settings, "FREE_SHIPPING_THRESHOLD", 0))
    return {
        "free_shipping_enabled": enabled,
        "free_shipping_threshold": threshold,
        # Whole dollars when the threshold is whole dollars: "$40" is the
        # offer, "$40.00" is a receipt.
        "free_shipping_threshold_display": (
            f"{threshold // 100}" if threshold % 100 == 0
            else f"{threshold / 100:.2f}"),
    }
