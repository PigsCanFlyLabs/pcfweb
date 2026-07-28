from django.db import migrations


class Migration(migrations.Migration):
    """Rejoins the two branches that both forked from 0012.

    This branch added 0013_product_cross_links_msrp_out_of_date (the MSRP,
    out_of_date and cross-link columns) while the mailing-list work landed
    0013..0016 on main. Both name 0012_product_bookshop_ebook_link as their
    parent, so the graph has two leaves and Django refuses to run.

    Empty on purpose: the two branches touch different tables and neither
    reorders the other's operations, so there is nothing to reconcile beyond
    declaring an order. Renumbering either side would have been the other
    option and is not available -- 0013..0016 are already merged to main and
    may be applied in a database somewhere, so their names are fixed.
    """

    dependencies = [
        ("main", "0013_product_cross_links_msrp_out_of_date"),
        ("main", "0016_alter_mailinglistdelivery_subscription"),
    ]

    operations = []
