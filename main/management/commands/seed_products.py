"""
Seed fixture-owned products without clobbering runtime-owned fields.

Replaces ``manage.py loaddata initial_products`` so that every deploy still
updates fixture-owned fields (name, description, price, links, tax_code, ...)
but never overwrites fields that are owned outside the fixture -- generated
runtime fields like ``external_product_id`` and admin-edited fields like
``stock``.

The fixture file ``main/fixtures/initial_products.yaml`` is still the single
source of truth for fixture-owned fields; edit it to change product metadata.
"""

from __future__ import annotations

from typing import Any, Set

import yaml
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from main.models import Product

# ---------------------------------------------------------------------------
# Fields that are NOT fixture-owned.  The seed command must never touch these
# on existing rows: some are generated lazily at runtime, and some are owned by
# the Django admin after initial fixture creation.
# ---------------------------------------------------------------------------
SEED_PROTECTED_FIELDS: Set[str] = {
    "external_product_id",
    "stock",
}


def _load_fixture(path: str) -> list[dict[str, Any]]:
    """Parse *path* as a Django fixture (JSON or YAML)."""
    with open(path, "rb") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return []
    return data


class Command(BaseCommand):
    help = (
        "Upsert fixture-owned products from main/fixtures/initial_products.yaml, "
        "preserving runtime/admin-owned fields like external_product_id and stock."
    )

    def handle(self, **options: Any) -> None:
        fixture_path = "main/fixtures/initial_products.yaml"
        entries = [
            entry
            for entry in _load_fixture(fixture_path)
            if entry.get("model") == "main.product"
        ]

        # A truncated or empty fixture must not look like a successful seed:
        # start-server.sh runs under `set -e`, so failing here stops the pod
        # from coming up with no products rather than silently serving none.
        if not entries:
            raise CommandError(
                f"{fixture_path} contains no main.product entries; "
                "refusing to report a successful seed."
            )

        created = 0
        updated = 0
        unchanged = 0

        # All-or-nothing: a failure partway through must not leave the
        # database half-seeded for the deploy that follows.
        with transaction.atomic():
            to_create = []

            for entry in entries:
                pk: int = entry["pk"]
                raw_fields: dict = entry["fields"]

                # Strip any non-fixture-owned fields that might sneak in.
                fixture_fields = {
                    k: v for k, v in raw_fields.items()
                    if k not in SEED_PROTECTED_FIELDS
                }

                if Product.objects.filter(pk=pk).exists():
                    # Existing row — update ONLY fixture-owned fields via a
                    # queryset .update() so that Product.save() (and the Stripe
                    # API call it triggers) is completely bypassed.
                    rows = Product.objects.filter(pk=pk).update(**fixture_fields)
                    if rows:
                        updated += 1
                        self.stdout.write(f"Updated product pk={pk}")
                    else:
                        unchanged += 1
                else:
                    to_create.append(Product(pk=pk, **fixture_fields))

            if to_create:
                # bulk_create() writes the rows without going through
                # Product.save(), which would call Stripe to mint a product id
                # for every row whose external_product_id is empty -- i.e. all
                # of them.  Seeding runs during pod startup, so a Stripe
                # outage or an expired key would stop the primary from
                # booting.  Leave the id empty and let it be generated lazily
                # on first add-to-cart, as the fixture documents.
                Product.objects.bulk_create(to_create)
                created = len(to_create)
                for product in to_create:
                    self.stdout.write(f"Created product pk={product.pk}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Seed complete: {created} created, {updated} updated, "
                f"{unchanged} unchanged"
            )
        )

        if created == 0 and updated == 0:
            self.stdout.write("All fixture products already up to date.")
