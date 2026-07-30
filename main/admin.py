# type: ignore
from django.contrib import admin
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

@admin.register(PurchaseFeedback)
class PurchaseFeedbackAdmin(admin.ModelAdmin):
    """Why people bought, in their own words.

    Unwritable and unamendable: this is somebody else's sentence. Editing it
    here would make a quote attributable to a buyer who did not write it, and
    the "may we quote you" answer beside it is a permission, not a setting to
    flip.

    Deletable, though, and deliberately so. Somebody who writes in asking for
    their words to be taken down needs a way to have that happen, and this
    row is the right granularity for it: the alternative is deleting the
    order, which is the accounting record and has to stay. "The row is the
    record" (see PurchaseFeedback.notify_owner) is about a failed
    notification email not losing the answer -- it was never a claim that
    staff cannot remove one on request.
    """

    list_display = ("order", "created_at", "may_quote", "quote_name",
                    "summary")
    list_filter = ("may_quote", "created_at")
    search_fields = ("reason", "quote_name", "order__pk",
                     "order__customer_email")
    date_hierarchy = "created_at"
    readonly_fields = tuple(f.name for f in PurchaseFeedback._meta.fields)

    def has_add_permission(self, request):
        # There is no such thing as feedback the owner wrote.
        return False

    @admin.display(description="what they said")
    def summary(self, obj):
        """The first line, so the changelist reads as a list of answers."""
        first = (obj.reason or "").strip().splitlines()
        text = first[0] if first else ""
        return text if len(text) <= 120 else f"{text[:117]}…"


@admin.register(SuppressedAddress)
class SuppressedAddressAdmin(admin.ModelAdmin):
    """The never-email list. Add one here; add many from the import page."""

    list_display = ("email", "reason", "created_at", "created_by")
    search_fields = ("email", "reason")
    readonly_fields = ("created_at", "created_by")
    date_hierarchy = "created_at"

    def save_model(self, request, obj, form, change):
        if obj.created_by is None:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


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
    readonly_fields = ("send_link", "status", "created_at", "updated_at",
                       "send_started_at", "sent_at", "created_by")
    inlines = [MailingListDeliveryInline]

    def save_model(self, request, obj, form, change):
        if obj.created_by is None:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="going to")
    def groups(self, obj):
        """Straight from the model, so this cannot drift from what gets sent."""
        return obj.audience_description()

    @admin.display(description="send")
    def send_link(self, obj):
        """The way to the send page, on the changelist and on the change form.

        Saving a mailing is not sending it, so without a link here the send
        page is reachable only by knowing the URL.
        """
        if obj.pk is None:
            return "Save this first, then a send link appears here."
        return format_html(
            '<a href="{}">send this mailing…</a>',
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
