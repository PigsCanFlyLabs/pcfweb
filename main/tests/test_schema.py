"""Schema guards for rolling-deploy-safe product fields."""

import importlib

from django.apps import apps
from django.db import models as django_models
from django.db import migrations as django_migrations
from django.db.backends.postgresql.schema import (
    DatabaseSchemaEditor as PostgresSchemaEditor,
)
from django.db.utils import ConnectionHandler
from django.test import SimpleTestCase, TestCase

from main.models import Product


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
