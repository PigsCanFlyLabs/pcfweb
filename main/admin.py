# type: ignore
from django.contrib import admin

from main.models import *
from django.apps import apps

# Register your models here.
admin.site.register(Cart)
admin.site.register(Product)
admin.site.register(CartProduct)


class OrderItemInline(admin.TabularInline):
    """The snapshotted lines, shown with the order so the owner can pick and
    pack from one page. Read-only: these are a record of what was charged,
    not something to edit after the fact."""
    model = OrderItem
    extra = 0
    can_delete = False
    # snapshot_quantity is shown next to quantity so an adjustment the
    # customer made on Stripe's page is visible rather than just overwritten.
    fields = ("product_name", "quantity", "snapshot_quantity",
              "unit_display_price", "total_display_price", "product",
              "price_id")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """Marking an order FULFILLED here is the entire fulfilment workflow."""

    inlines = [OrderItemInline]
    list_display = ("pk", "created_at", "status", "customer_email",
                    "total_display_price", "shipping_country", "notified_at",
                    "digital_delivery_sent_at")
    # Filtering on digital_delivery_sent_at puts "paid, includes a download,
    # never sent one" a click away -- that is the queue of people to resend to.
    list_filter = ("status", "created_at", "notified_at",
                   "digital_delivery_sent_at")
    # status is the one field the owner is meant to change.
    list_editable = ("status",)
    search_fields = ("pk", "customer_email", "customer_name",
                     "stripe_session_id", "shipping_name",
                     "shipping_postal_code")
    date_hierarchy = "created_at"
    # Everything else is Stripe's record of what happened, not ours to edit.
    readonly_fields = tuple(
        f.name for f in Order._meta.fields if f.name != "status")

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
