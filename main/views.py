from typing import *
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from main.models import Cart, CartProduct, Product
from main.payments import Payments
from main.utils import generate_username, get_country_code


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
        return render(request, 'signup.html', context={'title': 'Sign Up', 'in_use': in_use})

    def post(self, request):
        email = request.POST.get('email')
        password = request.POST.get('password')

        try:
            user = User.objects.get(email=email)
            return redirect(reverse('signup') + '?in_use=true')

        except User.DoesNotExist:
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
    def post(self, request):
        coupon = request.POST.get("coupon") or None
        cart = self.get_cart(request)
        redirect_url = Payments.checkout(request, cart, coupon=coupon)
        return redirect(redirect_url)

    def get(self, request):
        cart = self.get_cart(request)
        redirect_url = Payments.checkout(request, cart)
        return redirect(redirect_url)


class CheckoutSuccessView(View, BaseCartView):
    def get(self, request):
        cart = self.get_cart(request)
        cart.clear()
        return render(request, 'checkout_success.html', context={'title': 'Success! - Checkout'})


class CheckoutCancelView(View, BaseCartView):
    def get(self, request):
        return render(request, 'checkout_cancel.html', context={'title': 'Cancelled! - Checkout'})

class LoginView(View):
    def get(self, request):
        valid = request.GET.get('valid')
        return render(request, 'login.html', context={'title': 'Log In', 'valid': valid})

    def post(self, request):
        email = request.POST.get('email')
        password = request.POST.get('password')

        try:
            user = User.objects.get(email=email)
            user = authenticate(request, email=email,
                                username=user.username, password=password)
            if user is not None:
                login(request, user)
                return redirect('home')
            else:
                return redirect(reverse('login') + '?valid=false')
        except User.DoesNotExist:
            return redirect(reverse('login') + '?valid=false')


@method_decorator(login_required, name='dispatch')
class LogoutView(View):
    def get(self, request):
        logout(request)
        return redirect('login')

