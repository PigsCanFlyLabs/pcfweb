"""
Seed fixture-owned products without clobbering lazily-generated fields.

Replaces ``manage.py loaddata initial_products`` so that every deploy still
updates fixture-owned fields (name, description, price, links, tax_code, …)
but never overwrites fields that are lazily generated at runtime — most
importantly ``external_product_id`` (the Stripe product id).

The fixture file ``main/fixtures/initial_products.yaml`` is still the single
source of truth for fixture-owned fields; edit it to change product metadata.
"""

from __future__ import annotations

from typing import Any, Set

import yaml
from django.core.management.base import BaseCommand

from main.models import Product

# ---------------------------------------------------------------------------
# Fields that are NOT fixture-owned — they are populated lazily at runtime
# (e.g. Stripe product ids generated on first add-to-cart).  The seed command
# must never touch these on existing rows.  Add new generated fields here.
# ---------------------------------------------------------------------------
GENERATED_FIELDS: Set[str] = {
    "external_product_id",
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
        "preserving lazily-generated fields like external_product_id."
    )

    def handle(self, **options: Any) -> None:
        fixture_path = "main/fixtures/initial_products.yaml"
        entries = _load_fixture(fixture_path)

        created = 0
        updated = 0
        unchanged = 0

        for entry in entries:
            if entry.get("model") != "main.product":
                continue

            pk: int = entry["pk"]
            raw_fields: dict = entry["fields"]

            # Strip any generated fields that might sneak into the fixture.
            fixture_fields = {
                k: v for k, v in raw_fields.items() if k not in GENERATED_FIELDS
            }

            existing = Product.objects.filter(pk=pk).first()

            if existing is None:
                # Fresh DB — create the row via Product.objects.create() so
                # that Product.save() runs and auto-generates the Stripe
                # product id.
                Product.objects.create(pk=pk, **fixture_fields)
                created += 1
                self.stdout.write(f"Created product pk={pk}")
            else:
                # Existing row — update ONLY fixture-owned fields via a
                # queryset .update() so that Product.save() (and the Stripe
                # API call it triggers) is completely bypassed.
                rows = Product.objects.filter(pk=pk).update(**fixture_fields)
                if rows:
                    updated += 1
                    self.stdout.write(f"Updated product pk={pk}")
                else:
                    unchanged += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seed complete: {created} created, {updated} updated, "
                f"{unchanged} unchanged"
            )
        )

        if created == 0 and updated == 0:
            self.stdout.write("All fixture products already up to date.")
