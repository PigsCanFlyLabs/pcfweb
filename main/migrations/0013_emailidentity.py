from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.db.models.functions import Lower


def reserve_existing_emails(apps, schema_editor):
    User = apps.get_model("auth", "User")
    EmailIdentity = apps.get_model("main", "EmailIdentity")
    seen = set()
    for user in User.objects.exclude(email="").order_by("pk").iterator():
        normalized = user.email.strip().casefold()
        if normalized and normalized not in seen:
            EmailIdentity.objects.create(
                normalized_email=normalized, user_id=user.pk)
            seen.add(normalized)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("main", "0012_product_bookshop_ebook_link"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailIdentity",
            fields=[
                ("id", models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name="ID")),
                ("normalized_email", models.CharField(max_length=254)),
                ("user", models.OneToOneField(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="email_identity",
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "constraints": [models.UniqueConstraint(
                    Lower("normalized_email"),
                    name="unique_normalized_email")],
            },
        ),
        migrations.RunPython(
            reserve_existing_emails, migrations.RunPython.noop),
    ]
