from django.db import migrations


def set_service_delivery_type(apps, schema_editor):
    """Existing products default to PHYSICAL; the services are not.

    delivery_type replaces an inference that read "payment mode and not the
    services category". Everything already in the database predates the field,
    so the default lands PHYSICAL on all of it -- which is right for the books
    and the hardware and wrong for exactly one group. Fixing it here rather
    than in the fixture because services are created in the admin, not seeded.

    Mode is deliberately not consulted: a one-off consulting engagement is
    still a service, and the old expression only called it non-physical by
    way of the category.
    """
    Product = apps.get_model("main", "Product")
    Product.objects.filter(cat="S").update(delivery_type="SERVICE")


def unset_service_delivery_type(apps, schema_editor):
    Product = apps.get_model("main", "Product")
    Product.objects.filter(cat="S", delivery_type="SERVICE").update(
        delivery_type="PHYSICAL")


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0008_order_digital_delivery_error_and_more"),
    ]

    operations = [
        migrations.RunPython(
            set_service_delivery_type, unset_service_delivery_type),
    ]
