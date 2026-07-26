import logging

from typing import *
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

import stripe

from main.models import Cart, CartProduct, Order, Product
from main.payments import Payments
from main.utils import generate_username, get_country_code

logger = logging.getLogger(__name__)


# /healthz is served by main.middleware.HealthCheckMiddleware rather than a
# view here, so it can answer ahead of the HTTPS redirect, the ALLOWED_HOSTS
# check and the cookie-consent middleware's database query.


# Create your views here.
class HomeView(View):
    def get(self, request):
        highlights = map(
            lambda cat: ((cat, cat.label), list(Product.objects.filter(cat = cat).exclude(noorder=True).order_by('-price')[:3])),
            Product.Categories)
        # Only show categories with elements in them.
        highlights = list(filter(lambda x: len(x[1]) != 0, highlights))
        return render(
            request, 'index.html',
            context={
                'title': 'Pigs Can Fly Labs',
                'highlights': highlights,
            })


class AboutView(View):
    def get(self, request):
        return render(request, 'about.html', context={'title': 'About Us'})



class FamilyView(View):
    def get(self, request):
        # The family is a mix of things, not all of them separate companies:
        # one is a sibling company with its own site, another is the same
        # company as Pigs Can Fly Labs but with its own site. The `kind` line
        # says which each one is so the page does not flatten that distinction.
        projects = [
            {
                "name": "Pigs Can Fly Labs",
                "kind": "Parent company",
                "description": (
                    "The company behind this site — "
                    "value-priced queer and nerdy stuff."
                ),
                "url": None,
                "coming_soon": False,
            },
            {
                "name": "Fight Health Insurance",
                "kind": "Separate company and project",
                "description": (
                    "A separate company and project that helps people "
                    "appeal health insurance denials."
                ),
                "url": "https://www.fighthealthinsurance.com/",
                "coming_soon": False,
            },
            {
                "name": "Liberated Bread",
                "kind": "Same company, its own site",
                "description": (
                    "The same company as Pigs Can Fly Labs, with its own "
                    "site — not a separate company. Coming soon."
                ),
                "url": "https://www.liberatedbread.com/",
                "coming_soon": True,
            },
        ]
        return render(request, "family.html", context={
            "title": "Our Family of Projects",
            "projects": projects,
        })
class PrivacyView(View):
    def get(self, request):
        return render(request, 'privacy.html', context={'title': 'Privacy Policy'})

class TosView(View):
    def get(self, request):
        return render(request, 'tos.html', context={'title': 'TOS'})

class ReturnView(View):
    def get(self, request):
        return render(request, 'return.html', context={'title': 'TOS'})

class ContactView(View):
    def get(self, request):
        return render(request, 'contact.html', context={'title': 'Contact Us'})


class ProductsView(View):
    def get(self, request, category=None):
        if "category" not in request.GET and category is None:
            return render(request, 'products.html', context={
                'title': 'Products',
                'type': 'producs',
                'products': Product.objects.exclude(noorder=True)
            })
        else:
            cat = category or request.GET["category"]
            try:
                cat_name = Product.Categories(cat).label
            except ValueError:
                # Categories are stored upper-cased ("B", "OE"); be forgiving
                # about the case in the URL, but 404 on anything else rather
                # than letting the ValueError become a 500.
                try:
                    cat = cat.upper()
                    cat_name = Product.Categories(cat).label
                except ValueError:
                    raise Http404(f"No such product category: {cat}")
            extra_style = None
            bg_img_name = f"assets/images/{cat_name}.jpg".lower()
            if finders.find(f"{bg_img_name}"):
                extra_style = f"background-image: url('/static/{bg_img_name}');"
            return render(request, 'products.html', context={
                'title': f'Products - {cat_name}',
                'type': cat_name,
                'products': Product.objects.filter(cat=cat).exclude(noorder=True),
                'extra_style': extra_style
            })


class ServicesView(View):
    def get(self, request):
        products = Product.objects.filter(cat=Product.Categories.SERVICES).exclude(noorder=True)
        return render(request, 'products.html', context={
            'title': 'Services',
            'type': "Services",
            'products': products})


class SubscribeView(View):
    def get(self, request):
        return render(request, 'subscribe_page.html', context={'title': 'Subscribe for updates'})


class ProductView(View):
    def get(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        return render(request, 'single-product.html', context={
            'title': product.name,
            'product': product,
            'alt_links': product.get_alt_links(country=get_country_code(request)),
        })

class BaseCartView():
    """Common base cart view."""

    def get_cart(self, request) -> Cart:
        """Return the cart belonging to *this* requester.

        Every cart lookup in this app goes through here, so this is also the
        ownership boundary: an anonymous requester only ever gets the cart
        their session points at, and a logged-in requester only ever gets the
        cart attached to their user row.
        """
        user_cart = None
        session_cart = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            user_cart, _ = Cart.objects.get_or_create(user=user)
        # An anonymous cart can still be attached to the session even for a
        # logged-in user -- they filled it before logging in -- so always look,
        # and merge below if both exist.
        cart_id = request.session.get("cart_id")
        if cart_id is not None:
            # user__isnull keeps a stale/forged cart_id from ever resolving to
            # some user's persistent cart.
            session_cart = Cart.objects.filter(
                cart_id=cart_id, user__isnull=True).first()
            if session_cart is None:
                # Stale cookie pointing at a cart that no longer exists;
                # drop it instead of 500ing on every cart page from now on.
                del request.session["cart_id"]

        if user_cart is None:
            if session_cart is None:
                session_cart = Cart.objects.create()
                request.session["cart_id"] = session_cart.cart_id
            return session_cart
        if session_cart is None:
            return user_cart
        # Ok we have two carts time to merge them.
        self._merge_cart(request, session_cart, user_cart)
        return user_cart

    def _merge_cart(self, request, session_cart: Cart, user_cart: Cart) -> None:
        """Fold an anonymous session cart into the logged-in user's cart.

        Rows for a product the user cart already holds have their quantity
        added on and are dropped; the rest are reparented. Either way we never
        end up with two rows for the same (cart, product).

        The whole thing is one transaction: adding a quantity onto the
        surviving row and deleting the row it came from are two statements,
        and a crash between them would lose the quantity for good. All or
        nothing means a failed merge leaves the session cart untouched and
        the next request can just retry it.
        """
        with transaction.atomic():
            # Rows are linked to a cart both by FK and by the M2M; take the
            # union so a row that only made it into one still gets merged.
            session_products = CartProduct.objects.filter(
                Q(cart=session_cart) | Q(cart_products=session_cart)).distinct()
            for cart_product in session_products:
                session_cart.products.remove(cart_product)
                existing = CartProduct.objects.filter(
                    cart=user_cart, product=cart_product.product).first()
                if existing is not None:
                    existing.quantity += cart_product.quantity
                    existing.save()
                    user_cart.products.add(existing)
                    cart_product.delete()
                else:
                    cart_product.cart = user_cart
                    cart_product.save()
                    user_cart.products.add(cart_product)
            session_cart.delete()
        # The session lives outside the transaction, so drop the pointer only
        # once the merge has actually committed. If the block above rolled
        # back, the session still points at an intact cart.
        del request.session["cart_id"]

class SignupView(View):
    def get(self, request):
        in_use = request.GET.get('in_use', 'false')
        invalid = request.GET.get('invalid')
        return render(request, 'signup.html', context={
            'title': 'Sign Up', 'in_use': in_use, 'invalid': invalid})

    def post(self, request):
        # Both are required. Missing values used to reach generate_username()
        # as None and 500 on the AttributeError, and a missing password
        # reached set_password(None), which silently creates an account with
        # an unusable password that can never be logged into.
        email = (request.POST.get('email') or '').strip()
        password = request.POST.get('password') or ''
        if not email or not password:
            return redirect(reverse('signup') + '?invalid=missing')
        try:
            validate_email(email)
        except ValidationError:
            return redirect(reverse('signup') + '?invalid=email')

        # email is not unique on auth.User, so this can legitimately match
        # more than one row; either way the address is taken.
        if User.objects.filter(email=email).exists():
            return redirect(reverse('signup') + '?in_use=true')

        username = generate_username(email)
        user = User.objects.create(email=email, username=username)
        user.set_password(password)
        user.save()

        # No cart is created here: get_cart() owns that, and creating one
        # unconditionally on a OneToOneField is an unprotected insert.

        login(request, user)
        return redirect('home')


class GoogleProductFeed(View):
    def get(self, request):
        everything_but_services = (Product.objects.exclude(
            cat=Product.Categories.SERVICES)
            .exclude(noorder=True))
        return render(request, "google_products.xml", context={'products': everything_but_services}, content_type="text/xml")



class CartView(View, BaseCartView):
    def get(self, request):
        cart = self.get_cart(request)
        cart_products = cart.products.select_related("product")
        total_price = sum(map(lambda x: x.total_price(), cart_products))
        total_display_price = "{0:.2f}".format(total_price / 100)
        has_physical = any(cp.product.is_physical_good() for cp in cart_products)
        return render(request, 'cart.html', context={
            'title': 'Cart',
            'products': cart_products,
            'total_price': total_display_price,
            'has_physical': has_physical,
        })


class AddToCartView(View, BaseCartView):
    # What a PositiveBigIntegerField can physically hold. Python ints are
    # arbitrary precision, so without this a 20-digit quantity parses happily
    # and then 500s at write time on BIGINT overflow. This is a storage
    # capacity guard, not a purchase limit -- whether there should be a
    # product-level cap on quantity is a separate, still-open decision.
    MAX_QUANTITY = 9223372036854775807

    # POST only: a GET here is triggerable cross-site by an <img> tag or a
    # link prefetch, with no CSRF token involved.
    def post(self, request, product_id: int, quantity: int):
        # The quantity is a real form field, so the buy form works with no
        # JavaScript at all; the URL segment stays as the fallback for
        # existing links.
        posted_quantity = request.POST.get('quantity')
        if posted_quantity is not None:
            try:
                quantity = int(posted_quantity)
            except ValueError:
                return HttpResponseBadRequest("Quantity must be a number.")
        # Both paths converge here, so neither can disagree about the bounds.
        if quantity < 1:
            return HttpResponseBadRequest("Quantity must be at least 1.")
        if quantity > self.MAX_QUANTITY:
            return HttpResponseBadRequest(
                f"Quantity must be at most {self.MAX_QUANTITY}.")
        product = get_object_or_404(Product, pk=product_id)
        if not product.is_purchasable():
            return HttpResponseBadRequest("Product is not purchasable.")
        cart = self.get_cart(request)

        cart_product, created = CartProduct.objects.get_or_create(
            cart=cart, product=product, defaults={'quantity': quantity})
        if not created:
            # Adding a product that's already in the cart adds to what's
            # there rather than silently discarding the new quantity.
            cart_product.quantity += quantity
            cart_product.save()

        cart.products.add(cart_product)
        return redirect('cart')


class RemoveFromCartView(View, BaseCartView):
    # POST only, for the same reason as AddToCartView.
    def post(self, request, cart_product_id: int):
        cart = self.get_cart(request)
        # Scoped to the requester's own cart: a row belonging to somebody
        # else's cart is a 404, never a delete.
        cart_product = get_object_or_404(
            CartProduct, pk=cart_product_id, cart=cart)
        cart.products.remove(cart_product)
        cart_product.delete()
        return redirect('cart')


class CheckoutView(View, BaseCartView):
    # POST only. A GET here creates a PENDING order and a Stripe session as a
    # side effect, which an <img> tag or a link prefetch could trigger
    # cross-site with no CSRF token involved. cart.html posts a real form.
    def post(self, request):
        coupon = request.POST.get("coupon") or None
        return self.start_checkout(request, coupon=coupon)

    def start_checkout(self, request, coupon=None):
        """Record the order, then hand the customer to Stripe.

        The PENDING order has to exist *before* Session.create, because its id
        is what gets passed as client_reference_id -- that is the only thread
        back from a webhook to this cart, which will be gone by then.
        """
        cart = self.get_cart(request)
        if not cart.products.exists():
            # Stripe rejects a session with no line items anyway; bailing here
            # keeps empty checkouts from leaving orphan PENDING orders behind.
            return redirect('cart')
        # Stock is re-checked here, not just at add-to-cart: a cart can sit for
        # days, and stock is edited by hand in the admin, so what was
        # purchasable when it went in may not be now. Bounce back to the cart
        # rather than billing for something that cannot be shipped.
        unavailable = [
            cart_product.product.name
            for cart_product in cart.products.select_related("product")
            if not cart_product.product.is_purchasable()
        ]
        if unavailable:
            logger.info(
                "Blocked checkout for cart %s: %s no longer purchasable.",
                cart.pk, ", ".join(unavailable))
            messages.error(
                request,
                "Sorry, these are no longer available and need to be removed "
                "before checking out: " + ", ".join(unavailable) + ".")
            return redirect('cart')
        user = request.user if request.user.is_authenticated else None
        order = Order.create_from_cart(cart, user=user)
        try:
            redirect_url, session_id = Payments.checkout(
                request, cart, coupon=coupon, order=order)
        except Exception:
            # No session was ever created, so nothing will ever arrive for
            # this order -- not even checkout.session.expired. Close it out
            # here or it stays PENDING in the admin forever.
            logger.exception(
                "Stripe checkout failed for order #%s; cancelling it.",
                order.pk)
            Order.objects.filter(
                pk=order.pk, status=Order.Status.PENDING).update(
                    status=Order.Status.CANCELLED)
            raise
        order.stripe_session_id = session_id
        order.save(update_fields=['stripe_session_id', 'updated_at'])
        return redirect(redirect_url)


class CheckoutSuccessView(View, BaseCartView):
    """Where Stripe sends the customer after a completed Checkout session.

    Has to stay a GET -- Stripe redirects the browser here -- so it is
    reachable cross-site. It is therefore deliberately *not* the source of
    payment truth: the order shown is whatever the webhook already recorded,
    and its status is displayed as-is rather than asserted to be paid.
    """

    def get(self, request):
        # Only a session id that resolves to a real order empties the cart.
        # Stripe substitutes it into success_url (see Payments.checkout), so
        # the genuine redirect always carries one; a bare cross-site GET of
        # this URL does not, and so can no longer clear a stranger's cart.
        order = None
        session_id = request.GET.get("session_id")
        if session_id:
            order = Order.objects.filter(
                stripe_session_id=session_id).prefetch_related('items').first()
        if order is not None:
            self.get_cart(request).clear()
        return render(request, 'checkout_success.html', context={
            'title': 'Success! - Checkout',
            'order': order,
        })


@method_decorator(csrf_exempt, name='dispatch')
class StripeWebhookView(View):
    """Stripe's server-to-server callback. The only thing that marks a
    payment as real.

    POST only, CSRF exempt (Stripe has no token to send), and authenticated
    solely by the signature on the body. Every path out of here that is not a
    verified payload is a 400 before any state is touched.

    Everything runs synchronously: the work is one UPDATE and one email, and
    a queue would be more moving parts than the whole feature.
    """

    # Both mean "the money arrived". async_payment_succeeded is the delayed
    # follow-up for payment methods that settle after checkout (ACH, some
    # bank debits), where the original completed event arrives unpaid.
    PAID_EVENTS = frozenset({
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    })
    # Terminal failures. Only ever applied to a still-PENDING order, so they
    # can never undo a payment that already landed.
    CANCELLED_EVENTS = {
        "checkout.session.async_payment_failed": "the payment failed",
        "checkout.session.expired": "the checkout session expired",
    }

    def post(self, request):
        secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
        if not secret:
            # Failing closed: with no secret nothing can be verified, so
            # nothing may be processed.
            logger.error(
                "STRIPE_WEBHOOK_SECRET is not set; rejecting a Stripe "
                "webhook delivery. Orders will stay PENDING until it is.")
            return HttpResponseBadRequest("Webhook secret is not configured.")

        signature = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        try:
            event = stripe.Webhook.construct_event(
                request.body, signature, secret)
        except stripe.SignatureVerificationError:
            logger.warning("Rejected a Stripe webhook: bad signature.")
            return HttpResponseBadRequest("Invalid signature.")
        except ValueError:
            logger.warning("Rejected a Stripe webhook: malformed payload.")
            return HttpResponseBadRequest("Invalid payload.")

        event_type = event.get("type")
        session = (event.get("data") or {}).get("object") or {}

        if event_type in self.PAID_EVENTS:
            self.handle_paid(session)
        elif event_type in self.CANCELLED_EVENTS:
            self.handle_cancelled(session, self.CANCELLED_EVENTS[event_type])
        else:
            # 200 with no action, so Stripe stops redelivering event types
            # this endpoint has no opinion about.
            logger.debug("Ignoring Stripe event type %s.", event_type)
        return HttpResponse(status=200)

    def handle_paid(self, session) -> None:
        if session.get("payment_status") != "paid":
            # e.g. a completed session whose ACH debit has not settled. The
            # matching async_payment_succeeded will arrive later.
            logger.info(
                "Stripe session %s completed but is not paid (%s); leaving "
                "the order pending.",
                session.get("id"), session.get("payment_status"))
            return

        order = self.find_order(session)
        if order is None:
            # Nothing to attach it to and a retry would not change that, so
            # do not make Stripe keep trying.
            logger.warning(
                "No local order for Stripe session %s (ref %s).",
                session.get("id"), session.get("client_reference_id"))
            return

        fields = self.paid_fields(session)
        with transaction.atomic():
            # select_for_update serialises concurrent deliveries on Postgres;
            # the guarded UPDATE below is what actually makes this idempotent,
            # and it holds on any backend. Two simultaneous deliveries both
            # try to move PENDING -> PAID and exactly one row is affected, so
            # exactly one of them goes on to send the email.
            Order.objects.select_for_update().filter(pk=order.pk).first()
            updated = Order.objects.filter(
                pk=order.pk, status=Order.Status.PENDING).update(**fields)

        if not updated:
            logger.info(
                "Order #%s is already past PENDING; ignoring a duplicate "
                "delivery of Stripe session %s.", order.pk, session.get("id"))
            return

        order.refresh_from_db()
        # Only the delivery that actually moved the row gets here, so this
        # runs once per order. Both calls are best-effort by construction --
        # the payment is already recorded and must not be undone by a
        # secondary lookup or an SMTP outage.
        order.reconcile_line_items()
        order.notify_owner()

    def handle_cancelled(self, session, reason: str) -> None:
        order = self.find_order(session)
        if order is None:
            return
        updated = Order.objects.filter(
            pk=order.pk, status=Order.Status.PENDING).update(
                status=Order.Status.CANCELLED)
        if updated:
            logger.info("Order #%s cancelled: %s.", order.pk, reason)

    @staticmethod
    def find_order(session) -> Optional[Order]:
        """Map a Stripe session back to a local order.

        The stored stripe_session_id *is* the binding between an order and
        the payment for it, so the client_reference_id fallback may only ever
        reach an order that is not yet bound, or one bound to this very
        session. An order bound to a different session is never returned: the
        binding gets verified, not overwritten.
        """
        session_id = session.get("id")
        if session_id:
            order = Order.objects.filter(stripe_session_id=session_id).first()
            if order is not None:
                return order
        reference = (session.get("client_reference_id")
                     or (session.get("metadata") or {}).get("order_id"))
        if not reference:
            return None
        try:
            order = Order.objects.filter(pk=int(reference)).first()
        except (TypeError, ValueError):
            logger.warning(
                "Stripe session %s carried an unusable order reference %r.",
                session_id, reference)
            return None
        if order is None:
            return None
        if order.stripe_session_id and order.stripe_session_id != session_id:
            # Nothing legitimate produces this. Refuse rather than re-point a
            # paid-for order at a different session.
            logger.error(
                "Refusing Stripe session %s for order #%s: that order is "
                "already bound to session %s. Not marking it paid.",
                session_id, order.pk, order.stripe_session_id)
            return None
        return order

    @staticmethod
    def paid_fields(session) -> Dict[str, Any]:
        """The order columns a paid Stripe session dictates.

        Note this writes stripe_session_id, which is the second code path
        that touches that column. It is only safe because find_order() has
        already established that the order is either unbound or bound to this
        very session, so the write is a legitimate late binding or a no-op --
        never a re-pointing. Weaken that guard and this quietly becomes an
        overwrite again.
        """
        customer = session.get("customer_details") or {}
        billing = customer.get("address") or {}
        # Newer Stripe API versions moved shipping under collected_information;
        # older ones report it top level. Accept whichever this account sends.
        collected = session.get("collected_information") or {}
        shipping = (collected.get("shipping_details")
                    or session.get("shipping_details") or {})
        shipping_address = shipping.get("address") or {}
        totals = session.get("total_details") or {}

        fields: Dict[str, Any] = {
            "status": Order.Status.PAID,
            "paid_at": timezone.now(),
            "stripe_session_id": session.get("id"),
            "customer_email": customer.get("email") or "",
            "customer_name": customer.get("name") or "",
            "amount_total": session.get("amount_total") or 0,
            "amount_subtotal": session.get("amount_subtotal"),
            "amount_tax": totals.get("amount_tax"),
            "currency": (session.get("currency") or "usd").lower()[:3],
        }
        fields.update({
            "billing_name": customer.get("name") or "",
            "billing_line1": billing.get("line1") or "",
            "billing_line2": billing.get("line2") or "",
            "billing_city": billing.get("city") or "",
            "billing_state": billing.get("state") or "",
            "billing_postal_code": billing.get("postal_code") or "",
            "billing_country": billing.get("country") or "",
            "shipping_name": shipping.get("name") or "",
            "shipping_line1": shipping_address.get("line1") or "",
            "shipping_line2": shipping_address.get("line2") or "",
            "shipping_city": shipping_address.get("city") or "",
            "shipping_state": shipping_address.get("state") or "",
            "shipping_postal_code": shipping_address.get("postal_code") or "",
            "shipping_country": shipping_address.get("country") or "",
        })
        return fields


class CheckoutCancelView(View, BaseCartView):
    def get(self, request):
        return render(request, 'checkout_cancel.html', context={'title': 'Cancelled! - Checkout'})

class LoginView(View):
    def get(self, request):
        valid = request.GET.get('valid')
        return render(request, 'login.html', context={'title': 'Log In', 'valid': valid})

    def post(self, request):
        email = (request.POST.get('email') or '').strip()
        password = request.POST.get('password') or ''
        if not email or not password:
            return redirect(reverse('login') + '?valid=false')

        # email is not unique on auth.User, so .get() here could raise
        # MultipleObjectsReturned and 500 the login page. Try each match
        # instead: only the one whose password checks out authenticates.
        for candidate in User.objects.filter(email=email):
            user = authenticate(request, email=email,
                                username=candidate.username, password=password)
            if user is not None:
                login(request, user)
                return redirect('home')
        return redirect(reverse('login') + '?valid=false')


@method_decorator(login_required, name='dispatch')
class LogoutView(View):
    def get(self, request):
        logout(request)
        return redirect('login')
