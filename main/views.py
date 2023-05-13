from typing import *
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from main.models import Cart, CartProduct, Product
from main.payments import Payments
from main.utils import generate_username


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
                'products': Product.objects.all()
            })
        else:
            cat = category or request.GET["category"]
            try:
                cat_name = Product.Categories(cat).label
            except:
                cat = cat.upper()
                cat_name = Product.Categories(cat).label
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
        products = Product.objects.filter(cat=Product.Categories.SERVICES)
        return render(request, 'products.html', context={
            'title': 'Services',
            'type': "Services",
            'products': products})


class SubscribeView(View):
    def get(self, request):
        return render(request, 'subscribe_page.html', context={'title': 'Subscribe for updates'})


class ProductView(View):
    def get(self, request, pk):
        product = Product.objects.get(pk=pk)
        return render(request, 'single-product.html', context={'title': product.name, 'product': product})

class BaseCartView():
    """Common base cart view."""
    def get_cart(self, request) -> Optional[Cart]:
        user_cart = None
        session_cart = None
        if hasattr(request, "user") and request.user is not None and request.user is User:
            try:
                user_cart = Cart.objects.get(user=request.user)
            except:
                user_cart = Cart.objects.create(user=request.user)
                user_cart.save()
        else:
            if "cart_id" in request.session and request.session["cart_id"] is not None:
                session_cart = Cart.objects.get(cart_id=request.session["cart_id"])
            else:
                session_cart = Cart.objects.create()
                request.session["cart_id"] = session_cart.cart_id
        if user_cart is None:
            return session_cart
        if session_cart is None:
            return user_cart
        # Ok we have two carts time to merge them
        for product in session_cart.products.all():
            product.cart = user_cart
        del request.session["cart_id"]
        return user_cart

class SignupView(View):
    def get(self, request):
        in_use = request.GET.get('in_use', 'false')
        print(in_use)
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

            cart = Cart.objects.create(user=user)
            cart.save()

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
        print(f"Got cart {cart} with {cart.products.all()}")
        total_price = sum(map(lambda x: x.total_price(), cart.products.all()))
        total_display_price = "{0:.2f}".format(total_price / 100)
        return render(request, 'cart.html', context={'title': 'Cart', 'products': cart.products.all(), 'total_price': total_display_price})


class AddToCartView(View, BaseCartView):
    def get(self, request, product_id, quantity):
        product = Product.objects.get(pk=product_id)
        cart = self.get_cart(request)
        quantity = quantity

        try:
            cart_product = CartProduct.objects.get(cart=cart, product=product)
        except CartProduct.DoesNotExist:
            cart_product = CartProduct.objects.create(
                cart=cart, product=product, quantity=quantity)
            cart_product.save()

        cart.products.add(cart_product)
        cart.save()
        return redirect('cart')


class RemoveFromCartView(View, BaseCartView):
    def get(self, request, product_id):
        cart_product = CartProduct.objects.get(pk=product_id)
        cart = self.get_cart(request)
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

