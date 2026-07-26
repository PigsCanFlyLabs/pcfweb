# type: ignore
import csv

from django.contrib import admin
from django.http import HttpResponse
from django.urls import reverse
from django.utils.html import format_html

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

@admin.register(InterestArea)
class InterestAreaAdmin(admin.ModelAdmin):
    """The groups people can subscribe to."""

    list_display = ("name", "slug", "sort_order", "catch_all", "active",
                    "subscriber_count", "embed_link")
    list_filter = ("active", "catch_all")
    list_editable = ("sort_order",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

    @admin.display(description="embeddable form")
    def embed_link(self, obj):
        return format_html(
            '<a href="{}?interest={}">get the markup</a>',
            reverse("mailing-list-embed-code"), obj.slug)


@admin.register(MailingListSubscription)
class MailingListSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "interest", "status", "source",
                    "created_at", "confirmed_at")
    list_filter = ("status", "interest", "created_at")
    search_fields = ("email", "name", "source")
    date_hierarchy = "created_at"
    # The token authenticates the confirm and unsubscribe links, and the
    # timestamps are the record of what the subscriber did and when. Neither
    # is something to type over by hand.
    readonly_fields = ("token", "created_at", "updated_at", "confirmed_at",
                       "unsubscribed_at", "ip")
    actions = ["mark_subscribed", "mark_unsubscribed", "export_csv"]
    change_list_template = "admin/main/mailinglistsubscription/change_list.html"

    @admin.action(description="Mark selected as subscribed (they consented)")
    def mark_subscribed(self, request, queryset):
        for subscription in queryset:
            subscription.mark_subscribed()
        self.message_user(request, f"{queryset.count()} now subscribed.")

    @admin.action(description="Unsubscribe selected")
    def mark_unsubscribed(self, request, queryset):
        for subscription in queryset:
            subscription.unsubscribe()
        self.message_user(request, f"{queryset.count()} unsubscribed.")

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        """The other half of the importer: the columns it writes are the
        columns that page reads back."""
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="mailing-list.csv"')
        writer = csv.writer(response)
        writer.writerow(["email", "name", "interest", "status", "source",
                         "created_at", "confirmed_at"])
        for subscription in queryset.select_related("interest"):
            writer.writerow([
                subscription.email, subscription.name,
                subscription.interest.slug,
                subscription.get_status_display(), subscription.source,
                subscription.created_at.isoformat(),
                subscription.confirmed_at.isoformat()
                if subscription.confirmed_at else ""])
        return response


class MailingListDeliveryInline(admin.TabularInline):
    """Who this mailing actually reached. Read-only: it is a record of what
    happened, and editing it away would mean sending somebody a second copy."""

    model = MailingListDelivery
    extra = 0
    can_delete = False
    fields = ("email", "subscription", "status", "error", "created_at")
    readonly_fields = fields
    # A finished mailing has one row per recipient; the whole list would make
    # the change page unusable.
    max_num = 50

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(MailingListMessage)
class MailingListMessageAdmin(admin.ModelAdmin):
    list_display = ("subject", "status", "groups", "created_at", "sent_at",
                    "send_link")
    list_filter = ("status", "created_at")
    search_fields = ("subject", "body")
    filter_horizontal = ("interests",)
    readonly_fields = ("status", "created_at", "updated_at", "sent_at",
                       "created_by")
    inlines = [MailingListDeliveryInline]

    def save_model(self, request, obj, form, change):
        if obj.created_by is None:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="groups")
    def groups(self, obj):
        names = [interest.name for interest in obj.interests.all()]
        return ", ".join(names) if names else "everyone"

    @admin.display(description="send")
    def send_link(self, obj):
        return format_html(
            '<a href="{}">send…</a>',
            reverse("mailing-list-send", args=[obj.pk]))


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
