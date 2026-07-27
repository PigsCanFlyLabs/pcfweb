# Both branches numbered their migration 0012 -- this one adds
# Product.bookshop_ebook_link (main), that one starts the mailing list
# tables -- so the graph came out of the merge with two leaves and Django
# refused to build it at all. Neither side is wrong and neither is
# renumbered here: 0012_product_bookshop_ebook_link is already on main and
# may already be recorded as applied, and renaming an applied migration is
# how you get it run a second time.
#
# So the two are joined instead of reordered. No operations: the branches
# touch different models, nothing needs replaying, and this exists only to
# give the graph the single leaf it needs.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0012_product_bookshop_ebook_link"),
        ("main", "0015_alter_mailinglistdelivery_subscription"),
    ]

    operations = []
