"""The launch-stock rule, defined once.

Two callers need this rule and must agree forever:

* ``main/migrations/0017_backfill_book_stock.py`` applies it to a database
  that already has a catalogue -- production and every existing environment.
* ``seed_products`` applies it to rows it is *creating*, which is what a
  brand-new environment needs: start-server.sh runs ``migrate`` before
  ``seed_products``, so on a fresh database the migration runs against zero
  rows and cannot help. Without the create path, a new environment came up
  with every print book showing "Out of Stock".

They are kept here rather than written out twice because a second copy would
drift, and the specific way it would drift is bad: the scope exists to keep a
meaningless inventory count off the digital row and off products we do not
sell, so a stale copy is how pk 106 quietly acquires stock nobody intended.

Deliberately importable from a migration, which is why nothing here imports
``main.models`` and the field values are written as the literals the database
stores rather than as ``Product.Categories.BOOKS`` and friends -- a migration
runs against a historical model, not today's class. The cost of sharing is
that editing ``LAUNCH_STOCK`` also changes what the historical migration does
on a replay; that is accepted here because the alternative is the drift above,
and because the migration is a one-shot backfill whose only job is to get a
gate open.
"""

from __future__ import annotations

from typing import Any, Dict

from django.db.models import Q

# The launch stock. Not a real warehouse count -- `stock` "manually gates
# whether physical books can be purchased here; it does not cap order quantity
# or decrement automatically" (Product.stock's own help_text), so this is the
# number that turns the gate on, nothing more.
LAUNCH_STOCK = 4

# Which rows the launch stock applies to. This mirrors is_out_of_stock()'s own
# clauses, because the rows worth stocking are precisely the ones that
# predicate blocks, plus a noorder exclusion on top. Field-by-field reasoning
# lives in the migration's docstring; the short version:
#
#   cat "B" + PHYSICAL   -- the print books, the only things the stock gate
#                           actually applies to. A DIGITAL row (pk 106) is
#                           exempt via is_physical_good() and has no unit
#                           count to run out of.
#   not preorder/backorder -- already purchasable, and a title that has not
#                           been printed has no copies to count.
#   not noorder          -- not sold here at all, so stock cannot make it
#                           purchasable (pk 107).
LAUNCH_STOCK_SCOPE: Dict[str, Any] = {
    "cat": "B",
    "delivery_type": "PHYSICAL",
    "preorder_only": False,
    "backorder": False,
    "noorder": False,
}

# "Carries no stock figure." The column is NOT NULL (PositiveIntegerField,
# default 0, db_default 0), so the isnull arm matches nothing today; it states
# the intent rather than relying on the column's nullability staying put.
NO_STOCK_FIGURE = Q(stock=0) | Q(stock__isnull=True)


def in_launch_stock_scope(product: Any) -> bool:
    """Whether *product* is one of the rows the launch stock applies to.

    Takes an instance rather than a queryset so the create path can ask the
    question before the row exists. Compares against the same dict the
    queryset filter uses, so the two answers cannot disagree.
    """
    return all(
        getattr(product, field) == value
        for field, value in LAUNCH_STOCK_SCOPE.items()
    )


def apply_launch_stock(product: Any) -> bool:
    """Set the launch stock on an unsaved *product*. Returns whether it did.

    CREATE ONLY -- see the caller in seed_products. Safe by construction there
    and nowhere else: a row being created has no inventory yet, so there is no
    count to destroy. The same write on an update path would resurrect a title
    that had been marked sold out in the admin, which is an overselling
    incident, so this must never be reached from one.

    Guarded on the row carrying no stock figure as well, so that a caller that
    somehow does hand it a stocked row still cannot clobber the number.
    """
    if in_launch_stock_scope(product) and not product.stock:
        product.stock = LAUNCH_STOCK
        return True
    return False


def backfill_launch_stock(product_model: Any) -> int:
    """Fill the launch stock in on existing rows. Returns the row count.

    Takes the model class so a migration can pass its historical one.
    Fill-in-only: filtered on the row carrying no stock figure, so a count a
    human typed into the admin is left exactly as it is.
    """
    return (
        product_model.objects.filter(**LAUNCH_STOCK_SCOPE)
        .filter(NO_STOCK_FIGURE)
        .update(stock=LAUNCH_STOCK)
    )
