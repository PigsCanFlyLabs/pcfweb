"""Schema guards for rolling-deploy-safe product stock."""

from django.db import models as django_models
from django.db.backends.postgresql.schema import (
    DatabaseSchemaEditor as PostgresSchemaEditor,
)
from django.db.utils import ConnectionHandler
from django.test import SimpleTestCase

from main.models import Product


class PostgresColumnDefaultDDLTest(SimpleTestCase):
    """Assert PostgreSQL keeps a real default for the new stock column.

    The runtime raw-INSERT test proves old-code writes work on the local test
    backend. Production is PostgreSQL, so this records the DDL Django would
    emit there without needing a live server.
    """

    NEW_NOT_NULL_COLUMNS = [("Product", "stock")]

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
