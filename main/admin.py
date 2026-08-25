# type: ignore
from django.contrib import admin
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html

from main.models import *
from django.apps import apps

# Register your models here.
admin.site.register(Cart)
admin.site.register(CartProduct)


class ProductImageInline(admin.TabularInline):
    """The extra pictures of one product, edited on the product itself.

    Inline rather than a standalone page because an image only means anything
    next to the product it is of: adding one from its own form means picking
    the product out of a dropdown of near-identical edition names, which is
    how the Executive Edition ends up carrying the paperback's photograph.
    """

    model = ProductImage
    extra = 1
    fields = ("position", "image_name", "image", "alt_text")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """Deliberately still the default form, plus the images inline.

    Product has a lot of columns and no curated admin; listing fields here
    would freeze that set, so a column added to the model would silently stop
    being editable. The inline is the only thing this class exists for.
    """

    inlines = [ProductImageInline]


@admin.register(ProductGroup)
class ProductGroupAdmin(admin.ModelAdmin):
    """One work, several format SKUs.

    The inline is the point of this page: a group's members and their order
    are what the group *is*, and editing them one product page at a time is
    how two SKUs end up both claiming to be the first format.

    Two things about saving here, both of which the page states in words to
    whoever is using it (see FIXTURE_OWNED_NOTE and save_formset):

    * for a fixture-owned row, an edit made here lasts until the next deploy
      re-seeds it. The fixture is the source of truth for the shipped books;
      this page is the source of truth for groups created in the admin.
    * member rows are written with .update(), never Product.save(), because
      save() mints a Stripe product for any row that has not got one yet.
    """

    FIXTURE_OWNED_NOTE = (
        "Products seeded from main/fixtures/initial_products.yaml (the "
        "shipped books) have their group, format label and format order "
        "re-applied from that file on every deploy. Edit the fixture to "
        "change those permanently; edits made here to a seeded row last "
        "only until the next deploy."
    )

    class MemberInline(admin.TabularInline):
        model = Product
        extra = 0
        fields = ("name", "format_label", "format_order", "price")
        # Everything else about a product is edited on the product itself;
        # this page is only about how the formats present as a set.
        readonly_fields = ("name", "price")
        can_delete = False
        # Product.format_rank() is the rule -- (format_order, pk) -- and this
        # is the page for arranging it, so it reads the rule rather than
        # restating it. A tuple here would be the one surface still ordering
        # members its own way if that rule ever gains a component.
        ordering = Product.FORMAT_RANK_ORDERING

        def has_add_permission(self, request, obj=None):
            # Adding a member here would mean creating a Product from a
            # blank row, and a Product needs far more than these four
            # fields. Set `group` on the product instead.
            return False

    inlines = [MemberInline]
    list_display = ("pk", "name", "member_count")
    search_fields = ("name", "products__name")

    def get_queryset(self, request):
        # Annotated rather than counted per row: member_count on the
        # changelist would otherwise issue one COUNT query per group listed.
        #
        # distinct=True is load-bearing, not decoration. search_fields above
        # includes the multi-valued products__name, and the search adds a
        # SECOND join over the same relation -- so without it a search whose
        # term matches k member names multiplies the count and the column
        # reads 3*k for a three-format group.
        return super().get_queryset(request).annotate(
            _member_count=Count("products", distinct=True))

    def save_formset(self, request, form, formset, change):
        """Write member rows with .update(), never Product.save().

        Product.save() calls Payments.create_product for any row whose
        external_product_id is empty -- which is every seeded row until
        somebody adds it to a cart, since seed_products deliberately leaves
        it blank for lazy minting. So reordering two formats on this page,
        which is what the page is FOR, used to issue live Stripe writes: a
        500 mid-edit when Stripe is unreachable or the key has rotated, and
        a Stripe product created as a side effect of a presentation change
        when it is not.

        Only the two fields this inline can edit are written, so a concurrent
        edit to the rest of the product is not clobbered by a stale form.

        Bypassing formset.save() means bypassing its bookkeeping too, and
        that bookkeeping is not optional: the admin calls
        construct_change_message() straight after save_related(), and that
        reads formset.new_objects, .changed_objects and .deleted_objects --
        attributes only save_existing_objects()/save_new_objects() ever
        assign. Without them the request raises AttributeError, and since the
        change view runs in a transaction the UPDATEs above are rolled back
        with it, so the page cannot save at all. They are populated here by
        hand, which also keeps the admin's LogEntry an accurate record of
        what moved.
        """
        if formset.model is not Product:
            return super().save_formset(request, form, formset, change)

        # Adds and deletes are both turned off on this inline, so these two
        # are empty by construction rather than by omission.
        formset.new_objects = []
        formset.deleted_objects = []
        formset.changed_objects = []

        for member_form in formset.forms:
            if not member_form.has_changed() or not member_form.instance.pk:
                continue
            changed = {
                name: member_form.cleaned_data[name]
                for name in ("format_label", "format_order")
                if name in member_form.changed_data
            }
            # Nothing this inline owns actually moved -- do not issue an
            # UPDATE with no columns in it.
            if not changed:
                continue
            Product.objects.filter(
                pk=member_form.instance.pk).update(**changed)
            # The fields actually written, not member_form.changed_data:
            # the log should say what this page changed, and this page can
            # only change these two.
            formset.changed_objects.append(
                (member_form.instance, sorted(changed)))
        # No formset.save_m2m() to pair with the above: this inline edits no
        # M2M field, and save_m2m only exists after a save(commit=False).

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Said on the page rather than only in this docstring: an owner
        # reordering the shipped book's formats has no way to know from the
        # UI that the fixture will win on the next deploy.
        form.base_fields["name"].help_text = self.FIXTURE_OWNED_NOTE
        return form

    def member_count(self, obj):
        return obj._member_count

    member_count.short_description = "formats"
    member_count.admin_order_field = "_member_count"


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
