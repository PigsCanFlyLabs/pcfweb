from django.db import migrations

from main.launch_stock import backfill_launch_stock


def backfill_book_stock(apps, schema_editor):
    """Give the print books a launch stock so they can be bought at all.

    Every print SKU shipped with stock 0, which makes is_out_of_stock() true
    and is_purchasable() false, so the catalogue rendered "Out of Stock" on
    books we were ready to sell.

    Why a migration and not the fixture: `stock` is in seed_products'
    SEED_PROTECTED_FIELDS, and a fixture row carrying a protected field raises
    CommandError and exits 1. scripts/start-server.sh runs seed_products under
    `set -euo pipefail`, so that would stop the primary pod from booting. The
    protection is also load-bearing in its own right -- it is what stops a
    deploy from resetting live inventory -- so it stays, and this runs once
    instead. The distinction that makes both true at once: the FIXTURE may not
    specify stock, while the seed COMMAND may apply a launch default to a row
    it is creating.

    This half covers databases that already have a catalogue -- production
    included. It cannot cover a fresh one: start-server.sh runs migrate before
    seed_products, so on an empty database this finds zero rows. That case is
    handled at the point of creation instead, in seed_products.

    FILL IN ONLY, NEVER CLOBBER. backfill_launch_stock() filters on the row
    carrying no stock figure, so a number a human typed into the admin is left
    exactly as it is. Production may already carry hand-set counts, and
    silently overwriting a real one would oversell a title -- worse than the
    bug being fixed here.

    Scope mirrors is_out_of_stock()'s own clauses, because the rows worth
    touching are precisely the rows that predicate currently blocks:
    cat B + PHYSICAL + not preorder + not backorder + stock 0. Then noorder is
    excluded on top. Row by row, for the next person who asks why their
    product was skipped:

    * pks 100-105 (the print books) are the target -- physical, in the Books
      category, blocked by nothing except the stock count.
    * pk 106 is DIGITAL. is_out_of_stock() short-circuits on
      is_physical_good(), so the count never applies to it; an e-book has no
      unit count to run out of, and writing one here would be a meaningless
      number that a later reader would take for real inventory. See
      test_stock.DigitalStockExemptionTest.
    * pk 107 is noorder=True -- not sold through this site at all.
      is_purchasable() is `not noorder and not is_out_of_stock()`, so stock
      cannot make it purchasable and setting it achieves nothing today. The
      reason to actively skip it rather than let it through harmlessly: if
      noorder were ever cleared, the row would go straight to purchasable
      carrying a stock count nobody ever decided on.
    * preorder_only / backorder rows are excluded for the same reason. They
      are already purchasable (is_out_of_stock() returns False for both), so
      they are not what this fixes, and a title that has not been printed yet
      does not have four copies of itself sitting anywhere.

    The number and the predicate both live in main/launch_stock.py, shared
    with seed_products so the two cannot drift apart. Sharing them couples
    this migration to current app code, which is normally worth avoiding; it
    is accepted here because two copies of the scope rule drifting is the
    worse failure -- that is how the digital row would quietly acquire a
    meaningless inventory count.
    """
    Product = apps.get_model("main", "Product")
    backfill_launch_stock(Product)


def reverse_backfill(apps, schema_editor):
    """Deliberately a no-op.

    Nothing here can tell a 4 this migration wrote from a 4 somebody typed
    into the admin afterwards, so there is no safe rule for putting the old
    value back. The two obvious reverses are both destructive: zeroing every
    book would take the whole catalogue out of stock, and zeroing just the 4s
    would erase a real count that happened to be 4.

    Leaving the data alone is the honest reverse. It keeps the migration
    reversible for graph purposes -- `migrate main 0016` will run rather than
    refuse -- while making clear that unapplying it is a schema-level
    operation, not an inventory-level one. Stock is admin-owned from here on;
    to take a title out of stock, set it to 0 in the admin.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0016_alter_mailinglistdelivery_subscription"),
    ]

    operations = [
        migrations.RunPython(backfill_book_stock, reverse_backfill),
    ]
