from django.conf import settings
from django.conf.urls.static import static
from django.contrib.staticfiles import views as staticfiles_views
from django.http import Http404
from django.urls import path
from django.views.static import serve as static_serve

from main import views


def dev_static_serve(request, path):
    """Serve live source static files, falling back to generated STATIC_ROOT."""
    try:
        return staticfiles_views.serve(request, path)
    except Http404:
        return static_serve(request, path, document_root=settings.STATIC_ROOT)


urlpatterns = [
    # Products
    path('products', views.ProductsView.as_view(), name="products"),
    path('services', views.ServicesView.as_view(), name="services"),
    path('products/<str:category>', views.ProductsView.as_view(), name="products"),
    path('subscribe', views.SubscribeView.as_view(), name="subscribe"),
    path('product/<int:pk>', views.ProductView.as_view(), name="product"),
    # Stable alias for a book, so templates never hardcode a fixture pk.
    # Redirects to the canonical product/<int:pk>; see BookByIsbnView.
    path('book/<str:isbn>', views.BookByIsbnView.as_view(), name="book-by-isbn"),
    path('google_products.xml', views.GoogleProductFeed.as_view(), name='googleproducts'),

    # Cart
    path('cart', views.CartView.as_view(), name='cart'),
    # Both are POST-only (see views); <int:quantity> keeps non-numeric and
    # negative quantities from ever reaching the view.
    path('add-to-cart/<int:product_id>/<int:quantity>',
         views.AddToCartView.as_view(), name='add-to-cart'),
    path('remove-from-cart/<int:cart_product_id>',
         views.RemoveFromCartView.as_view(), name='remove-from-cart'),
    path('cart/pwyw-amount/<int:cart_product_id>',
         views.SetPwywAmountView.as_view(), name='set-pwyw-amount'),

    # Checkout flow
    path('checkout', views.CheckoutView.as_view(), name='checkout'),
    path('checkout/success', views.CheckoutSuccessView.as_view(),
         name='checkout-success'),
    path('checkout/cancel', views.CheckoutCancelView.as_view(),
         name='checkout-cancel'),
    # "What made you buy this?", posted from the success page. Unlike the
    # mailing list signup this one is CSRF-protected: it is only ever
    # rendered on a page of ours, and the order it writes to is named by the
    # Stripe session id rather than by anything guessable.
    path('checkout/feedback', views.PurchaseFeedbackView.as_view(),
         name='purchase-feedback'),
    # Stripe's callback. POST only and CSRF exempt; the signature on the body
    # is the only authentication. Register this URL in the Stripe Dashboard.
    path('stripe/webhook', views.StripeWebhookView.as_view(),
         name='stripe-webhook'),

    # Digital fulfilment. The token is a signed, expiring (order, product)
    # pair; this view is the only way to reach a book archive, which lives
    # outside every path nginx serves off disk.
    path('download/<str:token>', views.DigitalDownloadView.as_view(),
         name='digital-download'),

    # Accounts
    path('signup', views.SignupView.as_view(), name='signup'),
    path('login', views.LoginView.as_view(), name='login'),
    path('logout', views.LogoutView.as_view(), name='logout'),

    # Mailing list. Only the signup lives here and it is CSRF exempt (see the
    # view), because forms embedded on other sites post to it. Confirming and
    # unsubscribing are django-newsletter's pages, under /newsletter/.
    path('mailing-list/subscribe', views.MailingListSubscribeView.as_view(),
         name='mailing-list-subscribe'),
    path('mailing-list/subscribe-all',
         views.MailingListSubscribeAllView.as_view(),
         name='mailing-list-subscribe-all'),

    # General
    path('', views.HomeView.as_view(), name="home"),
    path('about', views.AboutView.as_view(), name="about"),
    path('family', views.FamilyView.as_view(), name="family"),
    path('contact', views.ContactView.as_view(), name="contact"),
    # Captcha-gated; the invite itself is only ever assembled in the browser.
    path('discord', views.DiscordJoinView.as_view(), name="discord"),
    path('tos', views.TosView.as_view(), name='tos'),
    path('privacy', views.PrivacyView.as_view(), name='privacy'),
    path('returns', views.ReturnView.as_view(), name='returns'),


]

if settings.MEDIA_URL is not None:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.STATIC_URL is not None:
    urlpatterns += static(settings.STATIC_URL, view=dev_static_serve)
