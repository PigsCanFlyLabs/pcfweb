"""
Seed fixture-owned products without clobbering runtime-owned fields.

Replaces ``manage.py loaddata initial_products`` so that every deploy still
updates fixture-owned fields (name, description, price, links, tax_code, ...)
but never overwrites fields that are owned outside the fixture -- generated
runtime fields like ``external_product_id`` and admin-edited fields like
``stock``, ``print_asin`` and ``default_asin``.

The fixture file ``main/fixtures/initial_products.yaml`` is still the single
source of truth for fixture-owned fields; edit it to change product metadata.

Many-to-many fields take a second pass
--------------------------------------
Neither of the two write paths below can carry an M2M. ``Product(pk=pk,
**fields)`` raises ``TypeError: Product() got unexpected keyword arguments``
for a related-manager name, and ``QuerySet.update()`` rejects it too -- an M2M
lives in its own table, so there is no column for either to write. A fixture
key like ``x_links: [105, 106]`` handed to either one stops the seed, and
``scripts/start-server.sh`` runs it under ``set -euo pipefail``, so that stops
the primary pod booting.

So M2M keys are split out of the field dict before the create/update, held
aside, and applied afterwards with ``.set()`` once every row named by the
fixture exists -- which is also what makes forward references work, since pk
104 names 105 and 106 before either has been read. ``.set()`` computes the
difference against what is already stored, so it adds nothing on a re-run:
that is the whole idempotency argument, and it is pinned end to end by
``SeedProductsCrossLinkTest``.

The same fixture-owned/admin-owned split applies as for ordinary fields. A row
whose fixture entry declares an M2M key has that relation reset from the
fixture on every deploy, so an admin-added link on those rows is dropped; a
row that omits the key is never touched and keeps whatever the admin set.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set

import yaml
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from main.models import Product


def _product_m2m_field_names() -> Set[str]:
    """The names of Product's own many-to-many fields.

    Read off the model rather than hardcoded, so that a fifth M2M added to
    Product later is handled by this command automatically instead of taking
    the deploy down the first time somebody puts it in the fixture.

    ``auto_created`` filters out reverse relations pointing *at* Product,
    which are not assignable here and are not fixture keys.
    """
    return {
        field.name
        for field in Product._meta.get_fields()
        if field.many_to_many and not field.auto_created
    }


def _is_symmetrical(field_name: str) -> bool:
    """Whether *field_name* is a symmetrical self-referential M2M."""
    field = Product._meta.get_field(field_name)
    return bool(getattr(field.remote_field, "symmetrical", False))

# ---------------------------------------------------------------------------
# Fields that are NOT fixture-owned.  The seed command must never touch these
# on existing rows: some are generated lazily at runtime, and some are owned by
# the Django admin after initial fixture creation.
# ---------------------------------------------------------------------------
#
# ``ebook_asin`` deliberately is NOT in this set: the Kindle ASIN is now
# fixture-owned so that a clean deploy seeds it instead of requiring manual
# admin entry on every fresh database.  Rows the fixture leaves blank are
# unaffected -- an omitted key is never passed to .update() below, so an
# admin-entered ASIN on such a row survives.
SEED_PROTECTED_FIELDS: Set[str] = {
    "default_asin",
    "external_product_id",
    "print_asin",
    "stock",
}


def _validate_m2m_links(
    declared: Dict[int, Dict[str, List[int]]],
    known_pks: Set[int],
) -> None:
    """Reject a cross-link graph that ``.set()`` would apply incorrectly.

    Checked here, up front and for the whole fixture at once, rather than
    discovered as an IntegrityError halfway through the apply pass -- the
    seed runs during pod startup, and a clear message beats a traceback from
    inside the M2M descriptor.

    Three ways the fixture can be wrong:

    * A target pk that names no product. ``.set()`` on a missing pk fails
      anyway; naming the offending row makes the fix obvious.
    * A row pointing at itself. Every consumer filters self-links back out,
      so storing one is at best inert and at worst a page offering the
      visitor the page they are on.
    * An asymmetric declaration on a *symmetrical* field, which is the
      subtle one and the reason this function exists. ``.set()`` replaces
      one row's entire link set, and on a symmetrical field it edits both
      ends. So if 104 declares ``[105]`` while 105 declares ``[]``, seeding
      creates the pair and then deletes it, and which of the two you get
      depends on fixture order. Both halves have to agree, and then the
      result is order-independent. A row that omits the key entirely is
      fine -- it is not being reset, so ``.set()`` on the other side simply
      adds the link.
    """
    for pk, fields in sorted(declared.items()):
        for field_name, targets in sorted(fields.items()):
            for target in targets:
                if target not in known_pks:
                    raise CommandError(
                        f"Product fixture pk={pk} field {field_name} links to "
                        f"pk={target}, which is neither in the fixture nor in "
                        "the database."
                    )
                if target == pk:
                    raise CommandError(
                        f"Product fixture pk={pk} field {field_name} links to "
                        "itself."
                    )
                if not _is_symmetrical(field_name):
                    continue
                counterpart = declared.get(target, {}).get(field_name)
                if counterpart is not None and pk not in counterpart:
                    raise CommandError(
                        f"Product fixture pk={pk} field {field_name} links to "
                        f"pk={target}, but pk={target} declares "
                        f"{field_name} without pk={pk}. {field_name} is "
                        "symmetrical, so both rows must list each other or "
                        "the second one seeded wins."
                    )


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
        "preserving runtime/admin-owned fields like external_product_id, "
        "stock, print_asin and default_asin."
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
        linked = 0

        m2m_field_names = _product_m2m_field_names()
        # pk -> {field name: [target pk, ...]}, for the second pass below.
        # Only rows whose fixture entry actually declares an M2M key appear
        # here; an omitted key means "leave this relation alone", exactly as
        # for an omitted ordinary field.
        declared_m2m: Dict[int, Dict[str, List[int]]] = {}

        # All-or-nothing: a failure partway through must not leave the
        # database half-seeded for the deploy that follows.
        with transaction.atomic():
            to_create = []

            for entry in entries:
                pk: int = entry["pk"]
                raw_fields: dict = entry["fields"]
                protected_fields = sorted(
                    set(raw_fields) & SEED_PROTECTED_FIELDS)
                if protected_fields:
                    raise CommandError(
                        f"Product fixture pk={pk} contains protected "
                        f"field(s): {', '.join(protected_fields)}. "
                        "Remove them from the fixture; those fields are "
                        "managed outside seed_products."
                    )

                # Split the M2M keys out before either write path sees them:
                # the constructor raises on one and .update() cannot write
                # one. They are applied after every row exists -- see the
                # module docstring.
                m2m_keys = sorted(set(raw_fields) & m2m_field_names)
                if m2m_keys:
                    declared_m2m[pk] = {
                        key: list(raw_fields[key] or []) for key in m2m_keys}
                fixture_fields = {
                    name: value for name, value in raw_fields.items()
                    if name not in m2m_field_names
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

            # Second pass: the M2M keys held aside above. It runs here, after
            # bulk_create, because .set() needs both ends to exist -- pk 104
            # names pk 106 several entries before 106 is read.
            if declared_m2m:
                # Targets may legitimately be rows the fixture does not own
                # (an admin-created product cross-linked from a fixture row),
                # so validate against the database as well as the fixture.
                known_pks = {entry["pk"] for entry in entries}
                known_pks |= set(
                    Product.objects.values_list("pk", flat=True))
                _validate_m2m_links(declared_m2m, known_pks)

                products = Product.objects.in_bulk(declared_m2m.keys())
                for pk, fields in sorted(declared_m2m.items()):
                    product = products[pk]
                    for field_name, targets in sorted(fields.items()):
                        # .set() diffs against what is stored: on a re-run
                        # with unchanged fixture data it issues no writes, so
                        # links are never duplicated. It also goes nowhere
                        # near Product.save(), so this pass cannot reach
                        # Stripe.
                        getattr(product, field_name).set(targets)
                    linked += 1
                    self.stdout.write(f"Cross-linked product pk={pk}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Seed complete: {created} created, {updated} updated, "
                f"{unchanged} unchanged, {linked} cross-linked"
            )
        )

        if created == 0 and updated == 0:
            self.stdout.write("All fixture products already up to date.")
