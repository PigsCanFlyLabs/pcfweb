import json
import logging
import re

from typing import *
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.http import (
    FileResponse, Http404, HttpResponse, HttpResponseBadRequest, JsonResponse)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.encoding import iri_to_uri
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt

import stripe

from main import captcha
from main.digital import (
    BadSignature, DigitalAssetError, SignatureExpired, link_lifetime_days,
    open_asset, parse_download_token)
from main.forms import (
    MailingListImportForm, MailingListSendForm, MailingListSignupForm)
from main.mailing import CsvImportError, import_csv
from main.models import (
    Cart, CartProduct, InterestArea, MailingListDelivery, MailingListMessage,
    MailingListSubscription, Order, Product)
from main.payments import Payments
from main.utils import (
    generate_username, get_country_code, get_storable_client_ip)

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


class DiscordJoinView(View):
    """The captcha-gated door to the Discord invite.

    A Discord invite URL is a bearer token: whatever reads it can join, and
    every link-following crawler on the internet reads pages like this one. So
    the URL is never in the page as a URL. It lives as two halves (see
    Base.DISCORD_INVITE_PART_ONE/TWO), the halves are only put in a response
    once a captcha has been answered, and the joining happens in the visitor's
    browser.

    None of that is a wall -- a determined scraper runs JavaScript. It is a
    speed bump sized to the actual threat, and if the invite starts filling up
    with bots anyway the fallback is already here: unset the halves and this
    page becomes "e-mail us and we'll invite you", which is also what a
    misconfigured pair renders.
    """

    # The joined halves must look like this or the page will not offer them.
    # Two jobs: it catches a bad ConfigMap edit (a half dropped, an invite
    # pasted with a trailing newline) before it becomes a dead button, and it
    # keeps anything that is not an https discord.gg link -- a javascript:
    # URL, an attacker-chosen host -- from being handed to the browser to open.
    INVITE_PATTERN = re.compile(r"^https://discord\.gg/[A-Za-z0-9-]{2,64}$")

    # The name is bait for form-filling bots and deliberately looks worth
    # filling in; real visitors never see the field. See the template.
    HONEYPOT_FIELD = "email_confirm"

    def invite_parts(self, request) -> Optional[Tuple[str, str]]:
        """The two halves, or None when they don't make a usable invite."""
        first = settings.DISCORD_INVITE_PART_ONE  # type: ignore[misc]
        second = settings.DISCORD_INVITE_PART_TWO  # type: ignore[misc]
        if not first or not second:
            # Both halves have to be set, even though one of them could hold
            # a complete-looking URL on its own: an empty half is how a
            # dropped ConfigMap key looks, and there is no way to tell that
            # from a deliberate one-sided split. Fail to the e-mail page.
            logger.warning(
                "Only one half of the Discord invite is set; serving the "
                "e-mail fallback. Set both DISCORD_INVITE_PART_ONE and "
                "DISCORD_INVITE_PART_TWO.")
            return None
        if not self.INVITE_PATTERN.match(first + second):
            logger.warning(
                "The Discord invite halves do not join into a "
                "https://discord.gg/... link; serving the e-mail fallback.")
            return None
        return first, second

    def page(self, request, *, solved: bool, error: Optional[str] = None):
        parts = self.invite_parts(request)
        context = {
            "title": "Join our Discord",
            "support_email": settings.DISCORD_SUPPORT_EMAIL,  # type: ignore[misc]
            "invite_configured": parts is not None,
            "error": error,
        }
        if parts is None:
            # Nothing to gate, so don't make anyone answer a question first.
            return render(request, "discord.html", context=context)
        if solved:
            context["invite_part_one"], context["invite_part_two"] = parts
        else:
            context["challenge"] = captcha.new_challenge(request.session)
        context["solved"] = solved
        return render(request, "discord.html", context=context)

    def get(self, request):
        return self.page(request, solved=False)

    def post(self, request):
        # A filled honeypot is a bot; answer exactly as a wrong answer does so
        # it learns nothing about which field gave it away.
        if request.POST.get(self.HONEYPOT_FIELD, "").strip():
            return self.page(
                request, solved=False,
                error="That didn't match — here's a new question.")
        if not captcha.check_answer(request.session,
                                    request.POST.get("captcha_answer", "")):
            return self.page(
                request, solved=False,
                error=("That didn't match (or the question timed out) — "
                       "here's a new one."))
        return self.page(request, solved=True)


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
        return render(request, 'subscribe_page.html', context={
            'title': 'Subscribe for updates',
            'areas': list(InterestArea.signup_choices()),
        })


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
        # The displayed total is the sum of list prices, which for a
        # pay-what-you-want line is only a suggestion. Say so rather than
        # showing a number the buyer is not going to be charged.
        has_pwyw = any(cp.product.is_pwyw for cp in cart_products)
        return render(request, 'cart.html', context={
            'title': 'Cart',
            'products': cart_products,
            'total_price': total_display_price,
            'has_physical': has_physical,
            'has_pwyw': has_pwyw,
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
    # Payment statuses that mean the sale is done and fulfilment should run.
    # "no_payment_required" is what a zero-total session reports: Stripe
    # creates no PaymentIntent at all, so it can never become "paid". That is
    # a real case here rather than a curiosity -- the e-book is
    # pay-what-you-want with a floor of zero, and the owner's decision is that
    # a $0 order is still an order and the book still gets delivered.
    PAID_PAYMENT_STATUSES = frozenset({"paid", "no_payment_required"})

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
        if session.get("payment_status") not in self.PAID_PAYMENT_STATUSES:
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
        # runs once per order -- which is exactly what keeps a webhook
        # re-delivery from emailing the customer their book a second time.
        # All three calls are best-effort by construction: the payment is
        # already recorded and must not be undone by a secondary lookup, a
        # missing file or an SMTP outage.
        order.reconcile_line_items()
        # Before notify_owner, so the owner's email can report whether the
        # customer actually got their download.
        order.deliver_digital_goods()
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


class DigitalDownloadView(View):
    """Serve a purchased book from a signed, expiring link.

    Unauthenticated by design: the buyer typically has no account, so the
    signature on the token *is* the authorisation. It carries the order and
    the product, and both are re-checked against the database here -- a token
    only works for an order that was actually paid, actually contains that
    product, and whose product is still one we are licensed to hand out.

    Nothing about the path comes from the URL. The token yields two integers;
    the filename is rebuilt from the product's stem through
    digital.resolve_asset_path, which is what keeps an admin-typed
    "../../etc/passwd" from becoming a file read.
    """

    EXPIRED_MESSAGE = (
        "This download link has expired.\n\n"
        "Links are good for {days} days. Write to {email} quoting your order "
        "number and we will send you a fresh one.\n")

    def get(self, request, token):
        try:
            order_pk, product_pk = parse_download_token(token)
        except SignatureExpired:
            # Distinguished from a bad signature: this is a real customer with
            # a real receipt, and telling them "404" would be a lie.
            return HttpResponse(
                self.EXPIRED_MESSAGE.format(
                    days=link_lifetime_days(),
                    email=settings.DEFAULT_FROM_EMAIL),
                status=410, content_type="text/plain; charset=utf-8")
        except BadSignature:
            logger.warning("Rejected a download token that did not verify.")
            raise Http404("No such download.")

        order = Order.objects.filter(pk=order_pk).first()
        if order is None or order.status not in (
                Order.Status.PAID, Order.Status.FULFILLED):
            # Includes an order that was never paid for, so a guessed-at
            # (unforgeable) pairing still gets nothing.
            raise Http404("No such download.")
        item = order.items.select_related('product').filter(
            product_id=product_pk).first()
        if item is None or item.product is None:
            raise Http404("No such download.")
        if not item.product.is_digitally_fulfilled():
            # The rights interlock, applied at serve time as well as at send
            # time: if sells_ebook has since been cleared, old links stop
            # working too.
            logger.warning(
                "Refusing to serve product %s for order #%s: it is not a "
                "product we are licensed to distribute.", product_pk, order_pk)
            raise Http404("No such download.")

        try:
            handle = open_asset(item.product.digital_asset_name)
        except DigitalAssetError as e:
            # A customer-visible 404, but the reason is only ever logged --
            # it names server paths.
            logger.error(
                "Order #%s: cannot serve the download for %r: %s",
                order_pk, item.product_name, e)
            raise Http404("No such download.")
        return FileResponse(
            handle, as_attachment=True,
            filename=f"{item.product.digital_asset_name}.zip",
            content_type="application/zip")


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


class MailingListMixin:
    """Shared plumbing for the public mailing list endpoints."""

    def submitted(self, request) -> Dict[str, Any]:
        """The submitted fields, from a form post or a JSON body.

        A JSON body leaves request.POST empty, so without this a fetch()
        sending JSON -- the obvious thing to write against an endpoint that
        answers JSON -- would look to us like a submission with no email in
        it. Cached because a view is instantiated per request and the body
        can only be read once.
        """
        if not hasattr(self, "_submitted"):
            self._submitted = self._parse_body(request)
        return self._submitted

    @staticmethod
    def _parse_body(request) -> Dict[str, Any]:
        if request.content_type == "application/json":
            try:
                data = json.loads(request.body or b"{}")
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
        return request.POST

    def wants_json(self, request) -> bool:
        return (request.content_type == "application/json"
                or self.submitted(request).get("format") == "json"
                or request.GET.get("format") == "json"
                or "application/json" in request.headers.get("Accept", ""))

    def json_response(self, payload: Dict[str, Any], status: int = 200):
        response = JsonResponse(payload, status=status)
        # Read by scripts on other origins. Safe as a wildcard precisely
        # because this endpoint has no session and no credentials: it does
        # nothing that depends on who is asking.
        response["Access-Control-Allow-Origin"] = "*"
        return response

    def safe_next(self, request) -> Optional[str]:
        """The `next` the form asked for, if we are willing to send a browser
        there.

        Embedded forms live on other sites and want the visitor bounced back
        to their own thank-you page, so this cannot be same-origin only -- but
        an unchecked `next` on an endpoint anybody can post to is an open
        redirect, so the host has to be one we were told about.
        """
        target = self.submitted(request).get("next") or request.GET.get("next")
        if not target:
            return None
        allowed = set(getattr(settings, "MAILING_LIST_REDIRECT_HOSTS", ()))
        allowed.update(
            host for host in settings.ALLOWED_HOSTS if "*" not in host)
        if url_has_allowed_host_and_scheme(
                target, allowed_hosts=allowed,
                require_https=request.is_secure()):
            return iri_to_uri(target)
        logger.info("Refusing to redirect a mailing list signup to %r.",
                    target)
        return None

    def redirect_back(self, request, outcome: str):
        target = self.safe_next(request)
        if target is None:
            return None
        separator = "&" if "?" in target else "?"
        return redirect(f"{target}{separator}subscribed={outcome}")


@method_decorator(csrf_exempt, name='dispatch')
class MailingListSubscribeView(MailingListMixin, View):
    """The signup endpoint. CSRF exempt on purpose.

    The whole point is that a plain <form> pasted onto another site can post
    here, and such a form has no token to send. Nothing here reads the session
    or acts on behalf of a logged-in user, so there is no authority for a
    forged request to borrow: the worst it can do is put an address into
    PENDING, which does nothing until that address clicks the link.
    """

    def get(self, request):
        # Somebody following the action URL by hand, or a form that lost its
        # method. Send them to the real page rather than 405ing.
        return redirect('subscribe')

    def options(self, request, *args, **kwargs):
        # Preflight for a cross-origin fetch() posting JSON. A plain form post
        # never gets here.
        response = HttpResponse(status=204)
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Content-Type, Accept"
        response["Access-Control-Max-Age"] = "86400"
        return response

    def post(self, request):
        form = MailingListSignupForm(self.submitted(request))
        if not form.is_valid():
            error = "That does not look like an email address."
            if self.wants_json(request):
                return self.json_response(
                    {"ok": False, "error": error,
                     "errors": form.errors}, status=400)
            back = self.redirect_back(request, "invalid")
            if back is not None:
                return back
            return render(request, 'mailing_list_result.html', context={
                'title': 'Subscribe for updates',
                'ok': False,
                'message': error,
            }, status=400)

        if form.is_bot():
            # Answered exactly like a success, minus the database row: telling
            # a bot it was caught only teaches whoever wrote it.
            logger.info("Dropped a honeypotted mailing list signup.")
            return self.success(request, None, honeypot=True)

        interest = form.interest_area()
        subscription = MailingListSubscription.subscribe(
            email=form.cleaned_data["email"],
            interest=interest,
            name=form.cleaned_data.get("name", ""),
            source=(form.cleaned_data.get("source")
                    or request.headers.get("Referer", "")[:200]),
            ip=get_storable_client_ip(request))
        if subscription.status == MailingListSubscription.Status.PENDING:
            if self.over_rate_limit(request):
                # The row is kept -- if this is a real person they can sign up
                # again in an hour -- but no mail goes out. Otherwise an
                # endpoint anybody can post to is a way to have us send
                # hundreds of confirmations to an address somebody else
                # chose, from our domain, at our sending reputation's expense.
                logger.warning(
                    "Not sending a mailing list confirmation to %s: the "
                    "signup rate limit for this address's source was hit.",
                    subscription.email)
            else:
                subscription.send_confirmation_email(request)
        return self.success(request, subscription)

    def over_rate_limit(self, request) -> bool:
        """Whether this source has already had its hour's confirmations.

        Deliberately crude: the cache is per worker process, so the real
        ceiling is this times the worker count. That is fine -- the point is
        to bound a flood, not to police an exact number -- and it keeps this
        from needing a shared cache the site does not otherwise run.
        """
        limit = getattr(settings, "MAILING_LIST_SIGNUP_RATE_LIMIT", 20)
        if not limit:
            return False
        ip = get_storable_client_ip(request)
        if ip is None:
            return False
        key = f"mailing-list-signups:{ip}"
        try:
            count = cache.get_or_set(key, 0, 3600)
            # incr is atomic where the backend supports it; a missing key
            # means it expired between the two calls, which is a reset, not
            # an error.
            count = cache.incr(key) if count is not None else 1
        except ValueError:
            cache.set(key, 1, 3600)
            return False
        return count > limit

    def success(self, request, subscription, honeypot: bool = False):
        pending = (honeypot or subscription is None
                   or subscription.status
                   == MailingListSubscription.Status.PENDING)
        message = (
            "Almost there — check your email for a link to confirm."
            if pending else
            "You are already on that list. Nothing else to do.")
        if self.wants_json(request):
            return self.json_response(
                {"ok": True, "pending": pending, "message": message})
        back = self.redirect_back(request, "1")
        if back is not None:
            return back
        return render(request, 'mailing_list_result.html', context={
            'title': 'Subscribe for updates',
            'ok': True,
            'message': message,
        })


class MailingListConfirmView(MailingListMixin, View):
    """Where the link in the confirmation email lands.

    A GET, because that is what clicking a link does. Confirming is the one
    thing here that a mail scanner following the link on the subscriber's
    behalf can only get right: it means "yes", and the address it says yes for
    is the one the mail was sent to.
    """

    def get(self, request, token):
        subscription = get_object_or_404(MailingListSubscription, token=token)
        if subscription.status == MailingListSubscription.Status.UNSUBSCRIBED:
            # Only PENDING confirms. Unsubscribing does not rotate the token,
            # so the original confirmation email still carries a working link
            # -- and a forwarded copy of it, or a link scanner getting to it
            # late, must not be able to undo somebody leaving the list.
            # Signing up again is the way back, and that mails a fresh link.
            logger.info(
                "Ignoring a confirmation link for %s: they unsubscribed.",
                subscription.email)
            return render(request, 'mailing_list_result.html', context={
                'title': 'Not subscribed',
                'ok': False,
                'message': (
                    f"{subscription.email} unsubscribed from "
                    f"{subscription.interest}, so that link no longer does "
                    "anything. You are welcome back any time."),
            })
        if subscription.status == MailingListSubscription.Status.PENDING:
            subscription.mark_subscribed()
        return render(request, 'mailing_list_result.html', context={
            'title': 'Subscribed',
            'ok': True,
            'message': (f"You are subscribed to {subscription.interest}. "
                        "Thanks!"),
            'unsubscribe_url': subscription.unsubscribe_url(request),
        })


@method_decorator(csrf_exempt, name='dispatch')
class MailingListUnsubscribeView(MailingListMixin, View):
    """Leaving the list.

    CSRF exempt because mail clients implementing one-click unsubscribe
    (RFC 8058) POST here themselves, from no origin, with no token. That is
    fine: the token in the URL is the authorisation, and the only thing a
    forged request can achieve is unsubscribing somebody who already has the
    link -- which they are entitled to do anyway.

    The GET is a confirmation page rather than the unsubscribe itself, so a
    link-prefetching mail client cannot silently take somebody off the list.
    """

    def get(self, request, token):
        subscription = get_object_or_404(MailingListSubscription, token=token)
        return render(request, 'mailing_list_unsubscribe.html', context={
            'title': 'Unsubscribe',
            'subscription': subscription,
            'done': subscription.status
            == MailingListSubscription.Status.UNSUBSCRIBED,
        })

    def post(self, request, token):
        subscription = get_object_or_404(MailingListSubscription, token=token)
        if subscription.status != MailingListSubscription.Status.UNSUBSCRIBED:
            subscription.unsubscribe()
        if self.wants_json(request) or "List-Unsubscribe" in self.submitted(request):
            return self.json_response({"ok": True, "unsubscribed": True})
        return render(request, 'mailing_list_unsubscribe.html', context={
            'title': 'Unsubscribed',
            'subscription': subscription,
            'done': True,
        })


@method_decorator(xframe_options_exempt, name='dispatch')
class MailingListEmbedView(View):
    """A standalone signup form, for iframing into another site.

    Frame-options exempt: it is useless if a site cannot embed it, and there
    is nothing here to clickjack -- no session, no logged-in state, and the
    only action is a signup that still has to be confirmed by email.
    """

    def get(self, request, slug=None):
        area = None
        if slug:
            area = InterestArea.objects.filter(slug=slug, active=True).first()
            if area is None:
                raise Http404(f"No such interest area: {slug}")
        return render(request, 'mailing_list_embed.html', context={
            'title': 'Subscribe',
            'area': area or InterestArea.get_default(),
            'action': request.build_absolute_uri(
                reverse('mailing-list-subscribe')),
        })


class MailingListEmbedCodeView(View):
    """The copy-and-paste markup for putting the signup on another site."""

    def get(self, request):
        areas = list(InterestArea.signup_choices())
        selected_slug = request.GET.get("interest")
        # Falls back to the general group rather than to whichever area
        # happens to sort first, so copying the snippet without choosing
        # produces a form that agrees with the site's own default.
        area = (next((a for a in areas if a.slug == selected_slug), None)
                or InterestArea.get_default())
        return render(request, 'mailing_list_embed_code.html', context={
            'title': 'Embeddable signup form',
            'areas': areas,
            'area': area,
            'action': request.build_absolute_uri(
                reverse('mailing-list-subscribe')),
            'iframe_src': request.build_absolute_uri(
                reverse('mailing-list-embed', args=[area.slug])),
            'snippet_url': staticfiles_storage.url(
                'mailing-list/signup-form.html'),
            # So whoever is pasting the form can see whether their site is
            # already set up to have visitors sent back to it, rather than
            # finding out by watching the `next` be ignored.
            'redirect_hosts': [
                host for host in getattr(
                    settings, "MAILING_LIST_REDIRECT_HOSTS", [])
                if not host.startswith("www.")],
        })


@method_decorator(staff_member_required, name='dispatch')
class AdminHomeView(View):
    """One page listing where everything in the admin actually lives.

    The Django admin index only lists model changelists, so the things that
    are not a changelist -- the CSV import, the send page, the embeddable
    form -- are unfindable unless something like this points at them.
    """

    def get(self, request):
        return render(request, 'admin/home.html', context={
            'title': 'Admin',
            'sections': self.sections(),
        })

    @staticmethod
    def link(url_name, label, description, args=None):
        try:
            return {"url": reverse(url_name, args=args or []),
                    "label": label, "description": description}
        except NoReverseMatch:
            # A URL that is not wired up (an app removed, a rename) should
            # cost one missing row, not the whole page.
            logger.warning("Admin home: no URL named %s.", url_name)
            return None

    def sections(self):
        sections = [
            ("Mailing list", [
                self.link('admin:main_mailinglistsubscription_changelist',
                          "Subscribers",
                          "Every address, which group it is in, and whether "
                          "it has confirmed."),
                self.link('mailing-list-import', "Import subscribers from CSV",
                          "Bulk upload, with a dry run so you can see what a "
                          "file would do first."),
                self.link('admin:main_mailinglistmessage_changelist',
                          "Mailings",
                          "Write a mailing and send it to everyone or to "
                          "particular groups."),
                self.link('admin:main_interestarea_changelist',
                          "Interest areas",
                          "The groups people can subscribe to. Anyone who "
                          "does not pick one is in the general group."),
                self.link('mailing-list-embed-code', "Embeddable signup form",
                          "Markup to paste into another site so it can sign "
                          "people up here."),
            ]),
            ("Store", [
                self.link('admin:main_order_changelist', "Orders",
                          "Paid orders to pick, pack and mark fulfilled."),
                self.link('admin:main_product_changelist', "Products",
                          "Prices, stock and the links on each product page."),
            ]),
            ("Site", [
                self.link('admin:index', "Django admin",
                          "Everything else, model by model."),
                self.link('admin:password_change', "Change your password", ""),
            ]),
        ]
        return [(name, [link for link in links if link])
                for name, links in sections]


@method_decorator(staff_member_required, name='dispatch')
class MailingListImportView(View):
    """Upload a CSV of subscribers.

    Staff only and behind the admin's login, because it is the one way into
    this app to add an address without that address agreeing to it.
    """

    def get(self, request):
        return self.render_form(request, MailingListImportForm())

    def post(self, request):
        form = MailingListImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return self.render_form(request, form)
        upload = form.cleaned_data["csv_file"]
        try:
            result = import_csv(
                upload,
                interest=form.interest_area(),
                default_status=form.cleaned_data["status"],
                source=form.cleaned_data.get("source", "")
                or f"import:{getattr(upload, 'name', 'csv')}"[:200],
                dry_run=form.cleaned_data.get("dry_run", False))
        except CsvImportError as e:
            form.add_error("csv_file", str(e))
            return self.render_form(request, form)
        if not result.dry_run:
            messages.success(request, result.summary())
        return self.render_form(request, MailingListImportForm(), result=result)

    def render_form(self, request, form, result=None):
        return render(request, 'admin/mailing_list_import.html', context={
            'title': 'Import mailing list subscribers',
            'form': form,
            'result': result,
        })


@method_decorator(staff_member_required, name='dispatch')
class MailingListSendView(View):
    """Send a mailing, to everyone or to the groups it names.

    Sending is done a batch at a time from here rather than all at once,
    because this runs inside a request with a worker timeout on it: a list
    long enough to outlive that timeout would otherwise be half-sent with no
    record of how far it got. Each batch records a delivery per recipient, so
    clicking send again continues rather than starting over. `send_mailing`
    does the same thing from the command line for a list too long to click
    through.
    """

    def get(self, request, pk):
        message = get_object_or_404(MailingListMessage, pk=pk)
        return self.render_page(request, message, MailingListSendForm())

    def post(self, request, pk):
        message = get_object_or_404(MailingListMessage, pk=pk)
        form = MailingListSendForm(request.POST)
        if not form.is_valid():
            return self.render_page(request, message, form)

        if "send_test" in request.POST:
            address = (form.cleaned_data.get("test_address")
                       or request.user.email)
            if not address:
                form.add_error(
                    "test_address",
                    "Your account has no email address, so say where the "
                    "test should go.")
                return self.render_page(request, message, form)
            try:
                message.send_test(address, request)
            except Exception as e:
                logger.exception("Test send of message %s failed.", message.pk)
                messages.error(request, f"Could not send the test: {e}")
            else:
                messages.success(request, f"Test sent to {address}.")
            return redirect('mailing-list-send', pk=message.pk)

        if "send_batch" in request.POST:
            if not message.pending_recipients().exists():
                messages.info(
                    request, "Everyone on that list already has this one.")
                return redirect('mailing-list-send', pk=message.pk)
            sent, failed = message.send_batch(request=request)
            remaining = message.pending_count()
            note = f"Sent {sent}."
            if failed:
                note += (f" {failed} could not be delivered; see the "
                         "deliveries below.")
            if remaining:
                note += f" {remaining} still to go — click send again."
            messages.success(request, note)
            return redirect('mailing-list-send', pk=message.pk)

        return self.render_page(request, message, form)

    def render_page(self, request, message, form):
        return render(request, 'admin/mailing_list_send.html', context={
            'title': f'Send: {message.subject}',
            'message': message,
            'form': form,
            'groups': list(message.interests.all()),
            'recipient_count': message.recipient_count(),
            'pending_count': message.pending_count(),
            'sent_count': message.sent_count(),
            'failed_count': message.failed_count(),
            'batch_size': getattr(
                settings, "MAILING_LIST_SEND_BATCH_SIZE", 100),
            'failures': message.deliveries.filter(
                status=MailingListDelivery.Status.FAILED)[:20],
        })
