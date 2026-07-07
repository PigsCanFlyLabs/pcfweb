# type: ignore
from django.contrib import admin

from main.models import *
from django.apps import apps

# Register your models here.
admin.site.register(Cart)
admin.site.register(Product)
admin.site.register(CartProduct)

# Auto magic
models = apps.get_models()

for model in models:
    # A bit ugly but auto register everything which has not exploded when auto registering cauze I'm lazy
    if ("django.contrib" not in model.__module__ and
        "newsletter" not in model.__module__ and
        "cookie_consent" not in model.__module__):

        try:
            admin.site.register(model)
        except admin.sites.AlreadyRegistered:
            pass
