from django.core.management.color import no_style
from django.db import migrations


def reset_site_sequence(apps, schema_editor):
    """Move the Site id sequence past the row 0014 inserted by pk.

    0014 creates the Site with an explicit pk, which stops contrib.sites from
    creating its default -- and that default is what normally resets the
    sequence. On Postgres the sequence therefore stood at 1 on any database
    built from migrations, and the first site added in the admin collided
    with it. A no-op on sqlite, which has no sequences.
    """
    connection = schema_editor.connection
    Site = apps.get_model("sites", "Site")
    for sql in connection.ops.sequence_reset_sql(no_style(), [Site]):
        schema_editor.execute(sql)


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0026_productimage"),
        ("sites", "0002_alter_domain_unique"),
    ]

    operations = [
        migrations.RunPython(reset_site_sequence, migrations.RunPython.noop),
    ]
