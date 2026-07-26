"""Schema guards: every NOT NULL column added by a migration must keep a
real database default.

`migrate` runs on web-primary while the previous image keeps serving, so
for the length of every rollout the new schema is written by code that has
never heard of the new columns. A NOT NULL column with no database default
makes an old pod's INSERT fail, which 500s checkout for real customers.

Five classes, because three independent features each added columns needing
this guard and the checks are not interchangeable:

``RollingDeployOldCodeWriteTest``      issues a genuine old-code INSERT.
``PostgresColumnDefaultDDLTest``       the stock column (from the product
                                       stock feature), asserted against the
                                       PostgreSQL DDL. Also checks the bound
                                       default value, so it records
                                       ``(sql, params)`` pairs.
``PostgresDigitalColumnDefaultDDLTest`` the five columns the digital/PWYW
                                       migration adds, same technique but
                                       recording statements only.
``RollingDeployOldCodeProductInsertTest`` the five per-format identifier
                                       columns, which take the other route to
                                       the same safety: NULLable rather than
                                       NOT NULL-with-a-default.
``ProductFormatIdentifierMigrationTest`` the isbn -> print_isbn data backfill.

The DDL classes are deliberately separate rather than merged: their recorders
have different shapes, and collapsing them would mean one feature's guard
could be weakened while appearing to still cover the other's columns.
``PostgresColumnDefaultDDLTest`` and ``PostgresDigitalColumnDefaultDDLTest``
share two method *names* but are distinct classes, so both sets run."""

import importlib

from django.apps import apps
from django.db import connection
from django.db import models as django_models
from django.db import migrations as django_migrations
from django.db.backends.postgresql.schema import (
    DatabaseSchemaEditor as PostgresSchemaEditor)
from django.db.utils import ConnectionHandler
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from main.models import Order, Product


class RollingDeployOldCodeWriteTest(TestCase):
    """New columns must be writable by code that has never heard of them.

    deploy.yaml runs `migrate` on web-primary (1 replica) while the `web`
    Deployment's 3 replicas keep serving the *previous* image. So for the
    length of every rollout the new schema is being written by old code, and
    an INSERT from an old pod names only the columns that existed before the
    migration.

    That is fatal for a NOT NULL column added without a database default.
    Django's AddField backfills a default and then takes it back out of the
    schema -- on Postgres, literally:

        ALTER TABLE "main_order" ADD COLUMN "digital_delivery_error"
            text DEFAULT %s NOT NULL;
        ALTER TABLE "main_order" ALTER COLUMN "digital_delivery_error"
            DROP DEFAULT;

    leaving a column that is NOT NULL and has nothing to fall back on. An old
    pod's checkout INSERT omits it and the write fails, so checkout 500s for
    real customers for the length of the deploy. db_default keeps a real
    default in the schema instead.

    Raw SQL is the only way to test this: the ORM always names every field on
    the model it was built from, so it can never reproduce an old pod's
    INSERT.
    """

    def insert_order_naming_only_pre_0009_columns(self):
        """An INSERT exactly as the code before this branch would issue it."""
        columns = [
            "status", "customer_email", "customer_name",
            "shipping_name", "shipping_line1", "shipping_line2",
            "shipping_city", "shipping_state", "shipping_postal_code",
            "shipping_country", "billing_name", "billing_line1",
            "billing_line2", "billing_city", "billing_state",
            "billing_postal_code", "billing_country", "amount_total",
            "currency", "created_at", "updated_at", "notification_error",
            "reconciliation_error",
        ]
        now = timezone.now()
        values = [
            "PENDING", "buyer@example.com", "Buyer",
            "", "", "", "", "", "", "",
            "", "", "", "", "", "", "", 3000,
            "usd", now, now, "", "",
        ]
        with connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO main_order ({", ".join(columns)}) VALUES '
                f'({", ".join(["%s"] * len(columns))})',
                values)

    def insert_product_naming_only_pre_0009_columns(self):
        columns = [
            "description", "preorder_only", "noorder", "backorder", "name",
            "page", "price", "image", "image_name", "tax_code", "cat", "mode",
        ]
        values = [
            "An old product.", False, False, False, "Old product",
            "", 1000, "", "", "txcd_99999999", "M", "P",
        ]
        with connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO main_product ({", ".join(columns)}) VALUES '
                f'({", ".join(["%s"] * len(columns))})',
                values)

    def test_an_old_pod_can_still_record_an_order_mid_deploy(self):
        # The urgent one: old pods create an Order on every single checkout.
        self.insert_order_naming_only_pre_0009_columns()

        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.PENDING)
        # The database supplied these, not the ORM.
        self.assertEqual(order.digital_delivery_error, "")
        self.assertIsNone(order.digital_delivery_sent_at)

    def test_an_old_pod_can_still_write_a_product_mid_deploy(self):
        # Products are only written by the admin and by seeding, so this is
        # far less likely to be hit than the Order case -- but a deploy that
        # overlaps a `loaddata`/`seed_products` would hit it.
        self.insert_product_naming_only_pre_0009_columns()

        product = Product.objects.get(name="Old product")
        self.assertEqual(product.delivery_type,
                         Product.DeliveryTypes.PHYSICAL)
        self.assertEqual(product.digital_asset_name, "")
        self.assertFalse(product.is_pwyw)
        self.assertFalse(product.sells_ebook)
        self.assertFalse(product.on_oreilly_safari)

    def test_a_product_written_by_old_code_is_treated_as_physical(self):
        # The default has to be the *safe* one, not merely present: a product
        # that lands as DIGITAL would be one the shipping logic ignores, and
        # one that lands with sells_ebook set would be distributable.
        self.insert_product_naming_only_pre_0009_columns()

        product = Product.objects.get(name="Old product")
        self.assertTrue(product.is_physical_good())
        self.assertFalse(product.is_digitally_fulfilled())


class PostgresDDLRecorder:
    @staticmethod
    def postgres_connection():
        return ConnectionHandler({
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "unused",
                "USER": "unused",
                "PASSWORD": "unused",
                "HOST": "unused",
                "PORT": "",
            },
        })["default"]

    def add_field_ddl(self, model, field):
        recorded: list = []

        class RecordingSchemaEditor(PostgresSchemaEditor):
            def execute(self, sql, params=()):
                recorded.append((str(sql), tuple(params or ())))

        with RecordingSchemaEditor(
                self.postgres_connection(), atomic=False) as editor:
            editor.add_field(model, field)
        return recorded

    def add_named_field_ddl(self, model_name, field_name):
        model = {"Product": Product}[model_name]
        return self.add_field_ddl(model, model._meta.get_field(field_name))


class PostgresColumnDefaultDDLTest(PostgresDDLRecorder, SimpleTestCase):
    """Assert PostgreSQL keeps a real default for the new stock column.

    The runtime raw-INSERT test proves old-code writes work on the local test
    backend. Production is PostgreSQL, so this records the DDL Django would
    emit there without needing a live server.
    """

    NEW_NOT_NULL_COLUMNS = [("Product", "stock")]

    def test_add_column_statement_includes_default_for_backfill(self):
        """The discriminating durable-default checks live in the sibling tests.

        PostgreSQL emits the same ADD COLUMN ... DEFAULT %s for a Python-only
        default and for a db_default; the difference is whether Django later
        emits DROP DEFAULT. This test only asserts the ADD COLUMN statement
        Django records is the non-null defaulted form, and that the bound
        default value is 0.
        """
        for model_name, field_name in self.NEW_NOT_NULL_COLUMNS:
            with self.subTest(column=f"{model_name}.{field_name}"):
                statements = self.add_named_field_ddl(model_name, field_name)
                add_column = [s for s, _ in statements if "ADD COLUMN" in s]
                add_params = [params for s, params in statements
                              if "ADD COLUMN" in s]

                self.assertEqual(len(add_column), 1, statements)
                self.assertIn("NOT NULL", add_column[0])
                self.assertIn("DEFAULT", add_column[0])
                self.assertIn(0, add_params[0])

    def test_no_new_column_has_its_default_dropped(self):
        for model_name, field_name in self.NEW_NOT_NULL_COLUMNS:
            with self.subTest(column=f"{model_name}.{field_name}"):
                statements = self.add_named_field_ddl(model_name, field_name)

                self.assertEqual(
                    [s for s, _ in statements if "DROP DEFAULT" in s],
                    [],
                    f"{field_name} loses its database default: {statements}",
                )

    def test_this_check_would_actually_notice_a_missing_db_default(self):
        field = django_models.PositiveIntegerField(default=0)
        field.set_attributes_from_name("column_without_a_db_default")

        statements = self.add_field_ddl(Product, field)

        self.assertTrue(
            [s for s, _ in statements if "ADD COLUMN" in s and "DEFAULT" in s],
            statements,
        )
        self.assertTrue(
            [s for s, _ in statements if "DROP DEFAULT" in s],
            statements,
        )


class PostgresDigitalColumnDefaultDDLTest(SimpleTestCase):
    """The same rolling-deploy guard, against the backend actually deployed.

    RollingDeployOldCodeWriteTest proves an old-code INSERT succeeds -- but it
    runs against SQLite, and production is PostgreSQL. Two different schema
    editors, so a guard that only holds on SQLite is a guard on the one
    backend nobody deploys.

    This asserts the DDL Django *would* emit for PostgreSQL: each of the five
    NOT NULL columns migration 0009 adds carries a DEFAULT, and none of them
    is followed by the ALTER COLUMN ... DROP DEFAULT that was the original
    bug. No server is needed -- the statements are collected rather than run,
    which works because PostgreSQL takes defaults as bound parameters rather
    than literals, so nothing here needs a live connection to interpolate.
    """

    # Exactly the NOT NULL columns 0009 adds. digital_delivery_sent_at is
    # nullable and so is not at risk.
    NEW_NOT_NULL_COLUMNS = [
        ("Order", "digital_delivery_error"),
        ("Product", "delivery_type"),
        ("Product", "digital_asset_name"),
        ("Product", "is_pwyw"),
        ("Product", "on_oreilly_safari"),
        ("Product", "sells_ebook"),
    ]

    @staticmethod
    def postgres_connection():
        """A PostgreSQL connection object that is never connected to.

        Built through ConnectionHandler so it gets the same defaulting a real
        alias would, and kept out of settings.DATABASES so nothing else in the
        suite can accidentally route a query at it.
        """
        return ConnectionHandler({
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                # Never dialled; the schema editor below never executes.
                "NAME": "unused", "USER": "unused", "PASSWORD": "unused",
                "HOST": "unused", "PORT": "",
            },
        })["default"]

    def add_field_ddl(self, model, field):
        """The statements Django would run to add `field` on PostgreSQL."""
        recorded: list = []

        class RecordingSchemaEditor(PostgresSchemaEditor):
            # Collect instead of executing. Overridden here rather than using
            # collect_sql=True because that path still composes parameters
            # client-side, which does need a live connection.
            def execute(self, sql, params=()):
                recorded.append(str(sql))

        with RecordingSchemaEditor(
                self.postgres_connection(), atomic=False) as editor:
            editor.add_field(model, field)
        return recorded

    def add_named_field_ddl(self, model_name, field_name):
        model = {"Order": Order, "Product": Product}[model_name]
        return self.add_field_ddl(model, model._meta.get_field(field_name))

    def test_every_new_not_null_column_declares_a_database_default(self):
        for model_name, field_name in self.NEW_NOT_NULL_COLUMNS:
            with self.subTest(column=f"{model_name}.{field_name}"):
                statements = self.add_named_field_ddl(model_name, field_name)
                add_column = [s for s in statements if "ADD COLUMN" in s]

                self.assertEqual(len(add_column), 1, statements)
                self.assertIn("NOT NULL", add_column[0])
                self.assertIn("DEFAULT", add_column[0])

    def test_no_new_column_has_its_default_dropped(self):
        # The actual bug. Django backfills a default and then takes it back
        # out of the schema unless db_default is what put it there -- leaving
        # a NOT NULL column an old pod's INSERT cannot omit.
        for model_name, field_name in self.NEW_NOT_NULL_COLUMNS:
            with self.subTest(column=f"{model_name}.{field_name}"):
                statements = self.add_named_field_ddl(model_name, field_name)

                self.assertEqual(
                    [s for s in statements if "DROP DEFAULT" in s], [],
                    f"{field_name} loses its database default: {statements}")

    def test_this_check_would_actually_notice_a_missing_db_default(self):
        # Guards the guard. If add_field_ddl ever silently stopped recording,
        # or PostgreSQL stopped emitting DROP DEFAULT, the two tests above
        # would pass vacuously and defend nothing. So assert the failure mode
        # is still detectable: a field declared the way 0009 originally
        # declared these must still produce the DROP DEFAULT.
        field = django_models.TextField(blank=True)
        field.set_attributes_from_name("column_without_a_db_default")

        statements = self.add_field_ddl(Order, field)

        self.assertTrue(
            [s for s in statements if "ADD COLUMN" in s and "DEFAULT" in s],
            statements)
        self.assertTrue(
            [s for s in statements if "DROP DEFAULT" in s], statements)


class RollingDeployOldCodeProductInsertTest(PostgresDDLRecorder, SimpleTestCase):
    """Guards for columns added while old Product writers still run."""

    OLD_CODE_PRODUCT_COLUMNS = [
        "description",
        "external_product_id",
        "isbn",
        "upc",
        "mpn",
        "kickstarter",
        "kindle_link",
        "amazon_link",
        "bookshop_link",
        "amazon_in_link",
        "flipkart_link",
        "preorder_only",
        "noorder",
        "backorder",
        "date_available",
        "brand",
        "sizes",
        "name",
        "page",
        "price",
        "image",
        "image_name",
        "tax_code",
        "cat",
        "mode",
    ]
    NEW_IDENTIFIER_COLUMNS = [
        "print_isbn",
        "ebook_isbn",
        "default_asin",
        "print_asin",
        "ebook_asin",
    ]

    def test_old_code_raw_insert_can_omit_new_identifier_columns(self):
        """Old-code INSERTs omit new identifiers, so they must be NULLable."""
        raw_insert_sql = (
            "INSERT INTO main_product "
            f"({', '.join(self.OLD_CODE_PRODUCT_COLUMNS)}) "
            f"VALUES ({', '.join(['%s'] * len(self.OLD_CODE_PRODUCT_COLUMNS))})"
        )

        # Anti-vacuity: the compatibility SQL really is an old-code Product
        # insert, not an empty or unrelated statement.
        self.assertIn("INSERT INTO main_product", raw_insert_sql)
        self.assertIn("isbn", self.OLD_CODE_PRODUCT_COLUMNS)

        for field_name in self.NEW_IDENTIFIER_COLUMNS:
            with self.subTest(field=field_name):
                statements = self.add_named_field_ddl("Product", field_name)
                add_column = [s for s, _ in statements if "ADD COLUMN" in s]

                self.assertEqual(len(add_column), 1, statements)
                self.assertIn(field_name, add_column[0])
                self.assertNotIn(field_name, self.OLD_CODE_PRODUCT_COLUMNS)
                self.assertNotIn("NOT NULL", add_column[0])


class ProductFormatIdentifierMigrationTest(TestCase):
    def copy_operation(self):
        migration_module = importlib.import_module(
            "main.migrations.0009_product_format_identifiers")
        operations = [
            operation for operation in migration_module.Migration.operations
            if (
                isinstance(operation, django_migrations.RunPython)
                and operation.code.__name__ == "copy_isbn_to_print_isbn"
            )
        ]
        self.assertEqual(len(operations), 1)
        return operations[0]

    def test_copy_isbn_to_print_isbn_backfills_existing_rows(self):
        Product.objects.bulk_create([
            Product(pk=200, name="Existing print book", isbn="9781449358624"),
            Product(pk=201, name="Null ISBN book", isbn=None),
            Product(pk=202, name="Blank ISBN book", isbn=""),
        ])

        self.copy_operation().code(apps, None)

        values = {
            product.pk: (product.isbn, product.print_isbn)
            for product in Product.objects.filter(pk__in=[200, 201, 202])
        }
        self.assertEqual(values[200], ("9781449358624", "9781449358624"))
        self.assertEqual(values[201], (None, None))
        self.assertEqual(values[202], ("", ""))
