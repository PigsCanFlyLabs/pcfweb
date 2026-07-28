"""pigscanfly URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView

from main import views

urlpatterns = [
    path("accounts/", include("django.contrib.auth.urls")),

    # Everything staff-only lives under /timbit/ (he guards the lab, see the
    # about page). The pages below have to be declared *before* the admin
    # include: admin.site.urls ends in a catch-all <app_label> route, which
    # would otherwise swallow them and 404.
    path('timbit/admin/home', views.AdminHomeView.as_view(),
         name='admin-home'),
    path('timbit/admin/mailing-list/import',
         views.MailingListImportView.as_view(), name='mailing-list-import'),
    path('timbit/admin/mailing-list/send/<int:pk>',
         views.MailingListSendView.as_view(), name='mailing-list-send'),
    path('timbit/admin/', admin.site.urls),
    # The admin moved; keep the old path working rather than handing anyone
    # with a bookmark a 404. Temporary on purpose -- a permanent redirect is
    # cached by browsers indefinitely and would be a nuisance to undo.
    path('admin/', RedirectView.as_view(
        url='/timbit/admin/', permanent=False, query_string=True)),
    path('admin/<path:rest>', RedirectView.as_view(
        url='/timbit/admin/%(rest)s', permanent=False, query_string=True)),

    path('', include('main.urls')),
    path('newsletter/', include('newsletter.urls')),
    path("cookies/", include("cookie_consent.urls")),
]
