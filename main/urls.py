from django.conf import settings
from django.conf.urls.static import static
from django.urls import path

from main import views

urlpatterns = [
    # Products
    path('products', views.ProductsView.as_view(), name="products"),
    path('services', views.ServicesView.as_view(), name="services"),
    path('products/<str:category>', views.ProductsView.as_view(), name="products"),
    path('subscribe', views.SubscribeView.as_view(), name="subscribe"),
    path('product/<int:pk>', views.ProductView.as_view(), name="product"),
    path('product/<str:name>', views.ProductView.as_view(), name="product"),
    path('google_products.xml', views.GoogleProductFeed.as_view(), name='googleproducts'),

    # Cart
    path('cart', views.CartView.as_view(), name='cart'),
    path('add-to-cart/<int:product_id>/<quantity>',
         views.AddToCartView.as_view(), name='add-to-cart'),
    path('remove-from-cart/<int:product_id>',
         views.RemoveFromCartView.as_view(), name='remove-from-cart'),

    # Checkout flow
    path('checkout', views.CheckoutView.as_view(), name='checkout'),
    path('checkout/success', views.CheckoutSuccessView.as_view(),
         name='checkout-success'),
    path('checkout/cancel', views.CheckoutCancelView.as_view(),
         name='checkout-cancel'),

    # Accounts
    path('signup', views.SignupView.as_view(), name='signup'),
    path('login', views.LoginView.as_view(), name='login'),
    path('logout', views.LogoutView.as_view(), name='logout'),

    # General
    path('', views.HomeView.as_view(), name="home"),
    path('about', views.AboutView.as_view(), name="about"),
    path('contact', views.ContactView.as_view(), name="contact"),
    path('tos', views.TosView.as_view(), name='tos'),
    path('privacy', views.TosView.as_view(), name='privacy'),
    path('returns', views.ReturnView.as_view(), name='returns'),


]

if settings.MEDIA_URL is not None:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.STATIC_URL is not None:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
