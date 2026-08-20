"""Format groups: one work, several SKUs, one card.

The three "Distributed Computing 4 Kids (and Executives)" rows are one book
sold three ways -- paperback, Executive Edition, e-book -- each with its own
ISBN, price, tax code and product page. The catalogue used to show all three
as near-identical cards, and the homepage carousel showed the same book three
times because the [:3] slice had nothing else to choose from.

A ProductGroup collapses that to one card and moves the choice onto the
product page, where each format states its own price. What it must NOT do is
merge the SKUs, and rather more than half of this module is about that: every
row keeps its own identifiers, its own price, its own page and its own line in
the Merchant feed, and no button anywhere adds a sibling to a cart.

Layout, section by section:

  1. the fixture               -- the group as shipped
  2. collapse_format_groups()  -- the listing rule, and its fallbacks
  3. listings                  -- what the catalogue and homepage render
  4. the format chooser        -- what a product page renders
  5. what grouping must not do -- SKUs, carts, feed
  6. seeding                   -- the deploy path
  7. schema                    -- rolling-deploy safety for the new columns
"""

import re
from datetime import date
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils.html import escape

from main.management.commands.seed_products import (
    _product_fk_field_names, _to_attnames, _validate_group_links)
from main.models import Product, ProductGroup
from main.tests.base import EBOOK_PK
# The Stripe-free product factory, borrowed rather than copied: Product.save()
# mints a Stripe product id for any row without one. Importing the mixin (not
# a TestCase) collects no tests from that module.
from main.tests.test_product_editions import ProductFactoryMixin
from main.tests.test_stock import OLD_CODE_PRODUCT_COLUMNS


PRINT_PK = 104
EXECUTIVE_PK = 105
NOT_SOLD_HERE_PK = 107
DC4K_GROUP_PK = 200
DC4K_PKS = (PRINT_PK, EXECUTIVE_PK, EBOOK_PK)
WORK_NAME = "Distributed Computing 4 Kids (and Executives)"


# ---------------------------------------------------------------------------
# 1. The fixture
# ---------------------------------------------------------------------------


class Dc4kGroupFixtureTest(TestCase):
    """The group as shipped, and nothing else grouped by accident."""

    fixtures = ["initial_products"]

    def test_the_three_dc4k_skus_share_one_group(self):
        groups = {
            Product.objects.get(pk=pk).group_id for pk in DC4K_PKS}

        self.assertEqual(groups, {DC4K_GROUP_PK})

    def test_the_group_is_named_for_the_work(self):
        self.assertEqual(
            ProductGroup.objects.get(pk=DC4K_GROUP_PK).name, WORK_NAME)

    def test_no_other_catalogue_row_is_grouped(self):
        """A group is an explicit statement about three rows. Anything else
        picking one up would collapse cards nobody asked to collapse."""
        unexpected = sorted(
            Product.objects.exclude(group=None)
            .exclude(pk__in=DC4K_PKS)
            .values_list("pk", flat=True))

        self.assertEqual(unexpected, [])

    def test_each_format_is_labelled_and_ordered(self):
        self.assertEqual(
            [(product.format_label, product.format_order)
             for product in ProductGroup.objects
             .get(pk=DC4K_GROUP_PK).members()],
            [("Paperback", 0), ("Executive Edition", 1), ("E-book", 2)])

    def test_the_paperback_is_the_member_a_listing_shows(self):
        """Not a tiebreak: format_order 0 is the owner's choice of what to
        show somebody who has not picked a format yet. The paperback is the
        middle price of the three, so no ordering inferred from price would
        have produced it."""
        group = ProductGroup.objects.get(pk=DC4K_GROUP_PK)

        self.assertEqual(group.members()[0].pk, PRINT_PK)
        prices = [member.price for member in group.members()]
        self.assertEqual(sorted(prices)[1], Product.objects.get(
            pk=PRINT_PK).price)

    def test_a_new_product_is_ungrouped_by_default(self):
        product = Product(name="Brand new")

        self.assertIsNone(product.group_id)
        self.assertEqual(product.group_members(), [])
        self.assertEqual(product.get_format_options(), [])
        self.assertEqual(product.listing_format_labels(), [])
        self.assertEqual(product.listing_name(), "Brand new")

    def test_an_unsaved_products_format_label_is_a_string(self):
        """db_default alone leaves the attribute holding Django's
        DatabaseDefault sentinel until the row is saved and reloaded. The
        sentinel is truthy, so `format_label or name` returned the sentinel
        object itself -- and a template renders that."""
        product = Product(name="Brand new")

        self.assertEqual(product.format_label, "")
        self.assertEqual(product.get_format_label(), "Brand new")
        self.assertEqual(product.format_order, 0)


# ---------------------------------------------------------------------------
# 2. collapse_format_groups()
# ---------------------------------------------------------------------------


class CollapseFormatGroupsTest(ProductFactoryMixin, TestCase):
    """The listing rule, tested on the queryset rather than through a page."""

    fixtures = ["initial_products"]

    def collapsed_pks(self, queryset=None):
        queryset = (Product.objects.exclude(noorder=True)
                    if queryset is None else queryset)
        return [product.pk
                for product in queryset.order_by_release_date()
                .collapse_format_groups()]

    def test_a_group_contributes_exactly_one_row(self):
        collapsed = self.collapsed_pks()

        self.assertEqual(
            [pk for pk in collapsed if pk in DC4K_PKS], [PRINT_PK])

    def test_ungrouped_products_pass_through_untouched(self):
        collapsed = self.collapsed_pks()

        self.assertEqual(
            [pk for pk in collapsed if pk not in DC4K_PKS],
            [108, 103, 102, 101, 100])

    def test_collapsing_never_reorders_what_is_left(self):
        """The group takes the position of its first surviving member, so
        removing duplicates is the ONLY difference between the two lists."""
        full = self.collapsed_pks()
        uncollapsed = list(
            Product.objects.exclude(noorder=True)
            .order_by_release_date().values_list("pk", flat=True))

        self.assertEqual(full, [pk for pk in uncollapsed
                                if pk not in (EXECUTIVE_PK, EBOOK_PK)])

    def test_a_filtered_out_representative_falls_back_to_a_sibling(self):
        """The reason this is a Python pass and not a DISTINCT ON.

        Filtering can remove the member a group would otherwise show -- here
        by delisting the paperback. The work must still appear, represented by
        a format that survived, rather than dropping off the catalogue.
        """
        Product.objects.filter(pk=PRINT_PK).update(noorder=True)

        collapsed = self.collapsed_pks()

        self.assertEqual(
            [pk for pk in collapsed if pk in DC4K_PKS], [EXECUTIVE_PK])

    def test_the_lowest_format_order_wins_regardless_of_queryset_order(self):
        """Position comes from the caller's ordering; WHICH member is shown
        comes from format_order. Reversing the queryset must not swap the
        e-book in as the face of the group."""
        collapsed = (Product.objects.exclude(noorder=True)
                     .order_by("-pk").collapse_format_groups())

        self.assertEqual(
            [product.pk for product in collapsed if product.pk in DC4K_PKS],
            [PRINT_PK])

    def test_a_second_group_collapses_independently(self):
        group = ProductGroup.objects.create(name="Another work")
        first = self.make_product(
            pk=340, name="Another work, paperback", price=1000,
            group=group, format_order=0, release_date=date(2024, 1, 1))
        self.make_product(
            pk=341, name="Another work, e-book", price=500,
            group=group, format_order=1, release_date=date(2024, 1, 1))

        collapsed = self.collapsed_pks()

        self.assertIn(first.pk, collapsed)
        self.assertNotIn(341, collapsed)
        # ...and the DC4K group is unaffected by the new one.
        self.assertIn(PRINT_PK, collapsed)

    def test_an_empty_queryset_collapses_to_nothing(self):
        self.assertEqual(
            Product.objects.none().collapse_format_groups(), [])

    def test_it_returns_products_not_pks(self):
        """The listing templates call model methods on what comes back."""
        collapsed = (Product.objects.exclude(noorder=True)
                     .order_by_release_date().collapse_format_groups())

        self.assertTrue(all(isinstance(p, Product) for p in collapsed))

    def test_the_group_members_are_prefetched_not_queried_per_card(self):
        """The prefetch has to be USED, not merely issued.

        Every grouped card asks its group for the members twice over (the
        format summary and the price range), and a page is a loop over cards.
        ``ProductGroup.members()`` sorts in Python off ``.all()`` for exactly
        this reason: an ``.order_by()`` there builds a new queryset, cannot
        read the prefetched cache, and turns the prefetch into a wasted query
        plus one per card.
        """
        collapsed = (Product.objects.exclude(noorder=True)
                     .order_by_release_date().collapse_format_groups())

        with self.assertNumQueries(0):
            for product in collapsed:
                product.listing_name()
                product.listing_format_labels()
                product.listing_price_display()
                product.listing_release_year()

    def test_the_whole_listing_costs_a_fixed_number_of_queries(self):
        """...and the count does not grow with the catalogue: one for the
        rows with their groups joined in, one for the prefetched members."""
        with self.assertNumQueries(2):
            collapsed = (Product.objects.exclude(noorder=True)
                         .order_by_release_date().collapse_format_groups())
            for product in collapsed:
                product.listing_format_labels()
                product.listing_price_display()


# ---------------------------------------------------------------------------
# 3. Listings
# ---------------------------------------------------------------------------


@override_settings(THUMBNAIL_DEBUG=False)
class GroupedListingRenderingTest(TestCase):
    """What /products and the homepage actually render."""

    fixtures = ["initial_products"]

    def dc4k_card(self) -> str:
        """The DC4K group's card on /products, as rendered markup.

        Split on the card body rather than parsed, which is enough to keep an
        assertion about one card from being satisfied by another card's
        contents -- the point of asserting on a card rather than on the page.
        """
        html = self.client.get("/products").content.decode()
        cards = [f'<div class="down-content">{part}'
                 for part in html.split('<div class="down-content">')[1:]]
        matching = [card for card in cards
                    if f'href="/product/{PRINT_PK}"' in card]

        self.assertEqual(len(matching), 1, "expected exactly one DC4K card")
        return matching[0]

    def test_the_catalogue_links_the_group_once(self):
        html = self.client.get("/products").content.decode()

        links = re.findall(r'href="/product/(\d+)"', html)

        self.assertEqual(set(links) & {"105", "106"}, set())
        self.assertIn("104", links)

    def test_the_card_is_titled_with_the_work(self):
        html = self.client.get("/products").content.decode()

        self.assertIn(escape(WORK_NAME), html)
        # And not with the member SKU's format-suffixed name, which would be
        # the wrong title for a card standing for all three.
        self.assertNotIn(
            escape(Product.objects.get(pk=EXECUTIVE_PK).name), html)

    def test_the_card_names_the_formats_it_stands_for(self):
        """Collapsing the cards must not hide that the other formats exist."""
        html = self.client.get("/products").content.decode()

        for label in ("Paperback", "Executive Edition", "E-book"):
            with self.subTest(label=label):
                self.assertIn(label, html)

    def test_the_card_flags_the_pay_what_you_want_format(self):
        html = self.client.get("/products").content.decode()

        self.assertIn(f"E-book ({Product.PWYW_FORMAT_SUFFIX})", html)

    def test_the_card_prices_the_group_as_a_range(self):
        html = self.client.get("/products").content.decode()

        self.assertIn(
            f"12.99{Product.PRICE_RANGE_SEPARATOR}34.42", html)

    def test_the_card_does_not_quote_one_members_price_as_the_works(self):
        """The card is titled with the work, so a single figure on it reads
        as what the book costs -- and the member it would come from was
        picked by format_order, not by the visitor."""
        card = self.dc4k_card()

        self.assertNotIn("20.00", card)
        self.assertIn(
            f"12.99{Product.PRICE_RANGE_SEPARATOR}34.42", card)

    def test_an_ungrouped_card_keeps_its_own_price(self):
        html = self.client.get("/products").content.decode()

        # pk 108, ungrouped, 65.99 -- rendered exactly as before groups.
        self.assertIn("65.99", html)
        self.assertNotIn(f"65.99{Product.PRICE_RANGE_SEPARATOR}", html)

    def test_an_ungrouped_card_carries_no_format_summary(self):
        """The summary is conditional, not decoration on every card: an
        ungrouped product lists exactly as it did before groups existed."""
        html = self.client.get("/products").content.decode()

        self.assertEqual(html.count("Available in"), 1)

    def test_the_cover_link_announces_the_same_title_as_the_heading(self):
        """The cover's aria-label is what a screen reader reads out for that
        link; announcing a format-suffixed SKU name for a card headed with
        the work would name two different things on one card."""
        card = self.client.get("/products").content.decode()

        self.assertIn(f'aria-label="{escape(WORK_NAME)}"', card)
        self.assertNotIn(
            f'aria-label="{escape(Product.objects.get(pk=EBOOK_PK).name)}"',
            card)

    def test_the_homepage_carousel_shows_the_group_once(self):
        html = self.client.get("/").content.decode()

        self.assertNotIn('href="/product/105"', html)
        self.assertNotIn('href="/product/106"', html)
        self.assertIn('href="/product/104"', html)


class ListingPriceRangeTest(ProductFactoryMixin, TestCase):
    """listing_price_display(): a span for a group, a price for a row."""

    fixtures = ["initial_products"]

    def make_group(self, *prices, **kwargs):
        """A group of len(prices) products, cheapest first by format_order."""
        group = ProductGroup.objects.create(name="A priced work")
        for index, price in enumerate(prices):
            self.make_product(
                pk=400 + index, name=f"Format {index}", price=price,
                group=group, format_order=index, **kwargs)
        return Product.objects.get(pk=400)

    def test_the_range_spans_the_cheapest_and_dearest_format(self):
        self.assertEqual(
            Product.objects.get(pk=PRINT_PK).listing_price_bounds(),
            (1299, 3442))

    def test_the_range_renders_low_to_high(self):
        self.assertEqual(
            Product.objects.get(pk=PRINT_PK).listing_price_display(),
            f"12.99{Product.PRICE_RANGE_SEPARATOR}34.42")

    def test_every_member_shows_the_same_range(self):
        """Which member a listing happens to show is a detail of the
        collapse; the price it quotes for the work must not depend on it."""
        self.assertEqual(
            {Product.objects.get(pk=pk).listing_price_display()
             for pk in DC4K_PKS},
            {f"12.99{Product.PRICE_RANGE_SEPARATOR}34.42"})

    def test_a_pay_what_you_want_format_contributes_its_suggestion(self):
        """The owner's call: the e-book's floor is zero, but a range opening
        at 0.00 is a claim about the book rather than about that one format.
        The suggestion is the number it is priced at everywhere else."""
        ebook = Product.objects.get(pk=EBOOK_PK)

        self.assertTrue(ebook.is_pwyw)
        self.assertEqual(
            Product.objects.get(pk=PRINT_PK).listing_price_bounds()[0],
            ebook.price)

    def test_a_format_that_is_not_sold_here_stays_out_of_the_range(self):
        """pk 107's price is 0 because it is not sold, not because it is
        free. Counting it would advertise a free book."""
        group = ProductGroup.objects.create(name="A work with a dead format")
        Product.objects.filter(pk__in=(PRINT_PK, NOT_SOLD_HERE_PK)).update(
            group=group)

        product = Product.objects.get(pk=PRINT_PK)

        self.assertEqual(product.listing_price_bounds(), (2000, 2000))
        self.assertEqual(product.listing_price_display(), "20.00")

    def test_formats_that_agree_on_a_price_show_that_price(self):
        product = self.make_group(1500, 1500)

        self.assertFalse(product.listing_price_is_a_range())
        self.assertEqual(product.listing_price_display(), "15.00")

    def test_an_ungrouped_product_keeps_its_own_price_display(self):
        for pk in (101, 108):
            with self.subTest(pk=pk):
                product = Product.objects.get(pk=pk)

                self.assertIsNone(product.listing_price_bounds())
                self.assertEqual(product.listing_price_display(),
                                 product.get_display_price())

    def test_a_group_of_one_keeps_its_own_price_display(self):
        product = self.make_group(1500)

        self.assertIsNone(product.listing_price_bounds())
        self.assertEqual(product.listing_price_display(), "15.00")

    def test_a_group_with_nothing_sellable_falls_back_to_its_own_price(self):
        product = self.make_group(1500, 2500, noorder=True)

        self.assertIsNone(product.listing_price_bounds())
        self.assertEqual(product.listing_price_display(), "15.00")

    def test_a_single_price_keeps_the_preorder_prefix(self):
        """The prefix survives where it can be attached to one figure."""
        product = self.make_group(1500, 1500, preorder_only=True)

        self.assertEqual(product.listing_price_display(), "Pre-order: 15.00")

    def test_a_range_carries_no_preorder_prefix(self):
        """And not where it cannot: prefixing a span claims both ends ship
        later, which may be true of only one of them."""
        group = ProductGroup.objects.create(name="A mixed work")
        self.make_product(pk=410, name="Out now", price=1500, group=group,
                          format_order=0)
        self.make_product(pk=411, name="Coming soon", price=2500,
                          group=group, format_order=1, preorder_only=True)

        self.assertEqual(
            Product.objects.get(pk=410).listing_price_display(),
            f"15.00{Product.PRICE_RANGE_SEPARATOR}25.00")

    def test_the_separator_is_not_a_hyphen(self):
        """A hyphen between two numbers reads as a minus sign."""
        self.assertNotIn("-", Product.PRICE_RANGE_SEPARATOR)


class FormatSummaryExcludesDelistedFormatsTest(TestCase):
    """"Available in N formats" has to mean available.

    The same rows listing_price_bounds() leaves out of the range: a format
    that is not sold here is not one a card may offer. The chooser on the
    product page still lists it, with "Not sold here" beside it -- the card
    advertises what can be bought, the chooser says what exists.
    """

    fixtures = ["initial_products"]

    def group_with_a_delisted_format(self):
        group = ProductGroup.objects.create(name="A work with a dead format")
        Product.objects.filter(
            pk__in=(PRINT_PK, EBOOK_PK, NOT_SOLD_HERE_PK)).update(group=group)
        return Product.objects.get(pk=PRINT_PK)

    def test_a_delisted_format_is_not_advertised_on_the_card(self):
        product = self.group_with_a_delisted_format()
        not_sold = Product.objects.get(pk=NOT_SOLD_HERE_PK)

        labels = product.listing_format_labels()

        self.assertEqual(len(labels), 2)
        self.assertNotIn(not_sold.get_format_label(), labels)

    def test_a_group_whose_only_sibling_is_delisted_shows_no_summary(self):
        """One format for sale is not a choice of formats."""
        group = ProductGroup.objects.create(name="A work with a dead format")
        Product.objects.filter(pk__in=(PRINT_PK, NOT_SOLD_HERE_PK)).update(
            group=group)

        self.assertEqual(
            Product.objects.get(pk=PRINT_PK).listing_format_labels(), [])

    def test_the_summary_and_the_range_agree_on_which_rows_count(self):
        """Anti-drift: the card's two group-level statements are about the
        same set of formats, so a reader can match one to the other."""
        product = self.group_with_a_delisted_format()

        self.assertEqual(len(product.listing_format_labels()), 2)
        self.assertEqual(product.listing_price_bounds(), (1299, 2000))


class ListingReleaseYearTest(ProductFactoryMixin, TestCase):
    """A card for a work is dated by the work, not by one of its formats."""

    fixtures = ["initial_products"]

    def test_the_dc4k_card_shows_the_shared_year(self):
        self.assertEqual(
            Product.objects.get(pk=PRINT_PK).listing_release_year(), 2026)

    def test_a_later_format_does_not_redate_the_book(self):
        """The paperback came out in 2019; an e-book in 2024 does not make it
        a 2024 book. (Where the card SITS is the other question, and the
        collapse deliberately leaves that to the newest member.)"""
        group = ProductGroup.objects.create(name="A long-lived work")
        paperback = self.make_product(
            pk=430, name="Paperback", price=2000, group=group,
            format_order=0, release_date=date(2019, 5, 1))
        self.make_product(
            pk=431, name="E-book", price=1000, group=group,
            format_order=1, release_date=date(2024, 5, 1))

        self.assertEqual(paperback.listing_release_year(), 2019)
        self.assertEqual(
            Product.objects.get(pk=431).listing_release_year(), 2019)

    def test_an_ungrouped_product_shows_its_own_year(self):
        self.assertEqual(
            Product.objects.get(pk=101).listing_release_year(), 2017)

    def test_a_product_with_no_date_anywhere_shows_nothing(self):
        product = self.make_product(pk=432, name="Undated", price=100)

        self.assertIsNone(product.listing_release_year())

    def test_a_dateless_member_does_not_win_the_earliest(self):
        """NULL is "unknown", not "the beginning of time"."""
        group = ProductGroup.objects.create(name="A partly dated work")
        dated = self.make_product(
            pk=433, name="Dated", price=2000, group=group, format_order=0,
            release_date=date(2019, 5, 1))
        self.make_product(
            pk=434, name="Undated", price=1000, group=group, format_order=1)

        self.assertEqual(dated.listing_release_year(), 2019)

    @override_settings(THUMBNAIL_DEBUG=False)
    def test_the_card_renders_the_works_year(self):
        html = self.client.get("/products").content.decode()

        self.assertIn('<span class="pub-year">2026</span>', html)


class ListingPwywNoticeTest(ProductFactoryMixin, TestCase):
    """The notice follows the number it describes."""

    fixtures = ["initial_products"]

    def test_an_ungrouped_pwyw_row_still_carries_the_notice(self):
        product = self.make_product(
            pk=420, name="An ungrouped e-book", price=1299, is_pwyw=True)

        self.assertTrue(product.listing_shows_pwyw_notice())

    def test_a_pwyw_member_of_a_ranged_group_does_not(self):
        """Its card would show 12.99 – 34.42, and "that is a suggestion, pay
        nothing if you like" is false of most of that span."""
        self.assertTrue(Product.objects.get(pk=EBOOK_PK).is_pwyw)

        self.assertFalse(
            Product.objects.get(pk=EBOOK_PK).listing_shows_pwyw_notice())

    def test_an_all_pwyw_single_priced_group_does(self):
        """No range and every format is a suggestion, so the notice is true
        of everything the card stands for."""
        group = ProductGroup.objects.create(name="Two suggestions")
        product = self.make_product(
            pk=421, name="One", price=1299, is_pwyw=True, group=group)
        self.make_product(
            pk=422, name="Two", price=1299, is_pwyw=True, group=group)

        self.assertFalse(product.listing_price_is_a_range())
        self.assertTrue(product.listing_shows_pwyw_notice())

    def test_a_fixed_price_sibling_at_the_same_figure_suppresses_it(self):
        """The degenerate-range trap: a pwyw e-book at a suggested 12.99
        grouped with a paperback FIXED at 12.99 shows one figure, but "or
        nothing at all" under it invites the reader to conclude the paperback
        can be had for nothing. The notice must be true of every format the
        card stands for, not just the row that happens to represent it."""
        group = ProductGroup.objects.create(name="A mixed twin")
        product = self.make_product(
            pk=423, name="E-book", price=1299, is_pwyw=True, group=group,
            format_order=0)
        self.make_product(
            pk=424, name="Paperback", price=1299, group=group,
            format_order=1)

        self.assertFalse(product.listing_price_is_a_range())
        self.assertFalse(product.listing_shows_pwyw_notice())

    def test_a_noorder_sibling_does_not_suppress_it(self):
        """A format the card does not stand for has no vote: the same rows
        the bounds and the format summary exclude are excluded here."""
        group = ProductGroup.objects.create(name="A pwyw with a dead twin")
        product = self.make_product(
            pk=425, name="E-book", price=1299, is_pwyw=True, group=group,
            format_order=0)
        self.make_product(
            pk=426, name="Delisted", price=0, noorder=True, group=group,
            format_order=1)

        self.assertTrue(product.listing_shows_pwyw_notice())

    def test_a_fixed_price_row_never_carries_it(self):
        self.assertFalse(
            Product.objects.get(pk=PRINT_PK).listing_shows_pwyw_notice())


# ---------------------------------------------------------------------------
# 4. The format chooser
# ---------------------------------------------------------------------------


class FormatChooserTest(ProductFactoryMixin, TestCase):
    """get_format_options(), and the page it renders on."""

    fixtures = ["initial_products"]

    def options(self, pk):
        return Product.objects.get(pk=pk).get_format_options()

    def test_every_format_is_offered_from_every_format(self):
        for pk in DC4K_PKS:
            with self.subTest(pk=pk):
                self.assertEqual(
                    [option["url"] for option in self.options(pk)],
                    [f"/product/{member}" for member in DC4K_PKS])

    def test_the_current_format_is_listed_and_marked(self):
        """Listed, not omitted: a chooser showing only the alternatives makes
        the reader deduce what they are looking at from what is missing."""
        for pk in DC4K_PKS:
            with self.subTest(pk=pk):
                current = [option for option in self.options(pk)
                           if option["is_current"]]

                self.assertEqual(len(current), 1)
                self.assertEqual(current[0]["url"], f"/product/{pk}")

    def test_each_option_carries_its_format_label_and_its_row_name(self):
        """The label is what the reader chooses between; the name is which
        row they land on -- the rule get_ebook_cross_links() is written
        around."""
        options = self.options(PRINT_PK)

        self.assertEqual(
            [option["label"] for option in options],
            ["Paperback", "Executive Edition", "E-book"])
        self.assertEqual(
            [option["name"] for option in options],
            [Product.objects.get(pk=pk).name for pk in DC4K_PKS])

    def test_each_option_states_that_formats_own_price(self):
        prices = {option["label"]: option["price"]
                  for option in self.options(PRINT_PK)}

        self.assertEqual(prices["Paperback"], "20.00")
        self.assertEqual(prices["Executive Edition"], "34.42")

    def test_a_pay_what_you_want_format_is_not_quoted_a_fixed_price(self):
        """12.99 printed beside two fixed prices reads as a third fixed
        price. The suggestion goes in the note, where it is labelled."""
        ebook = [option for option in self.options(PRINT_PK)
                 if option["url"] == f"/product/{EBOOK_PK}"][0]

        self.assertEqual(ebook["price"], Product.FORMAT_PWYW_PRICE)
        self.assertEqual(ebook["note"], "suggested 12.99")

    def test_a_format_that_is_not_sold_here_says_so(self):
        """Ungated like get_cross_links(), because this is navigation -- but
        a delisted row's price is 0, and "0.00" is not what it costs."""
        not_sold = Product.objects.get(pk=NOT_SOLD_HERE_PK)
        group = ProductGroup.objects.create(name="A work with a dead format")
        Product.objects.filter(pk__in=(PRINT_PK, NOT_SOLD_HERE_PK)).update(
            group=group)

        options = {option["url"]: option
                   for option in self.options(PRINT_PK)}

        self.assertEqual(
            options[not_sold.get_absolute_url()]["price"],
            Product.FORMAT_NOT_SOLD_HERE)

    def test_an_out_of_stock_format_says_so_in_the_note(self):
        Product.objects.filter(pk=EXECUTIVE_PK).update(stock=0)

        executive = [option for option in self.options(PRINT_PK)
                     if option["url"] == f"/product/{EXECUTIVE_PK}"][0]

        self.assertEqual(executive["note"], "Out of stock")

    def test_an_ungrouped_product_has_no_chooser(self):
        self.assertEqual(self.options(101), [])

    def test_a_delisted_format_is_offered_by_the_chooser(self):
        """The chooser answers "what formats exist", so it lists one that is
        not sold here -- and says so. The listing card is the other way
        round; see FormatSummaryExcludesDelistedFormatsTest."""
        group = ProductGroup.objects.create(name="A work with a dead format")
        Product.objects.filter(pk__in=(PRINT_PK, NOT_SOLD_HERE_PK)).update(
            group=group)

        self.assertEqual(
            [option["url"] for option in self.options(PRINT_PK)],
            [f"/product/{PRINT_PK}", f"/product/{NOT_SOLD_HERE_PK}"])

    def test_a_group_of_one_has_no_chooser(self):
        """One format is not a choice; a chooser offering only the page you
        are on is a dead end."""
        group = ProductGroup.objects.create(name="A lonely work")
        Product.objects.filter(pk=101).update(group=group)

        self.assertEqual(self.options(101), [])
        self.assertEqual(Product.objects.get(pk=101).listing_format_labels(),
                         [])

    def test_a_member_with_no_label_falls_back_to_its_name(self):
        """A blank button says nothing about where it goes."""
        Product.objects.filter(pk=EXECUTIVE_PK).update(format_label="")
        executive = Product.objects.get(pk=EXECUTIVE_PK)

        self.assertEqual(executive.get_format_label(), executive.name)

    # -- rendering ---------------------------------------------------------

    @override_settings(THUMBNAIL_DEBUG=False)
    def test_the_page_renders_the_chooser_with_every_format(self):
        response = self.client.get(f"/product/{PRINT_PK}")

        self.assertContains(response, escape(Product.FORMAT_CHOOSER_HEADING))
        for pk in (EXECUTIVE_PK, EBOOK_PK):
            with self.subTest(pk=pk):
                self.assertContains(response, f'href="/product/{pk}"')
        self.assertContains(response, escape(Product.FORMAT_CURRENT_MARKER))

    @override_settings(THUMBNAIL_DEBUG=False)
    def test_the_current_format_is_not_a_link_to_itself(self):
        html = self.client.get(f"/product/{PRINT_PK}").content.decode()
        chooser = re.search(
            r'<div class="format-chooser">(.*?)</div>', html, re.DOTALL)
        self.assertIsNotNone(chooser)
        assert chooser is not None  # for mypy

        self.assertNotIn(f'href="/product/{PRINT_PK}"', chooser.group(1))

    @override_settings(THUMBNAIL_DEBUG=False)
    def test_an_ungrouped_page_renders_no_chooser(self):
        response = self.client.get("/product/101")

        self.assertNotContains(response, escape(
            Product.FORMAT_CHOOSER_HEADING))

    @override_settings(THUMBNAIL_DEBUG=False)
    def test_the_chooser_is_never_an_add_to_cart_for_a_sibling(self):
        """The checkout incident the whole edition feature is written around:
        a button on the paperback's page that adds the e-book would either
        sell a paperback as a download or put a product the visitor never
        named into their cart. Every chooser entry is a link to a page."""
        for pk in DC4K_PKS:
            with self.subTest(pk=pk):
                body = self.client.get(f"/product/{pk}").content.decode()

                self.assertEqual(
                    re.findall(r"add-to-cart/(\d+)/\d+", body), [str(pk)])


class OtherEditionLinksTest(ProductFactoryMixin, TestCase):
    """get_other_edition_links(): x_links minus what the chooser shows."""

    fixtures = ["initial_products"]

    def test_a_grouped_row_does_not_list_its_formats_twice(self):
        product = Product.objects.get(pk=PRINT_PK)

        # Anti-vacuity: the x_links really are there, and really are the
        # siblings -- this is a filter doing work, not an empty relation.
        self.assertEqual(len(product.get_cross_links()), 2)
        self.assertEqual(product.get_other_edition_links(), [])

    def test_a_cross_link_outside_the_group_still_renders(self):
        """The case the two fields do not share: a related title that is NOT
        another format of this work has nowhere else to be listed."""
        other = self.make_product(
            pk=350, name="A different book entirely", price=1000)
        Product.objects.get(pk=PRINT_PK).x_links.add(other)

        self.assertEqual(
            Product.objects.get(pk=PRINT_PK).get_other_edition_links(),
            [(other.name, other.get_absolute_url())])

    def test_an_ungrouped_row_keeps_every_cross_link(self):
        product = self.make_product(pk=351, name="Ungrouped", price=1000)
        other = self.make_product(pk=352, name="Its sibling", price=1000)
        product.x_links.add(other)

        self.assertEqual(
            product.get_other_edition_links(), product.get_cross_links())

    @override_settings(THUMBNAIL_DEBUG=False)
    def test_the_grouped_page_drops_the_other_editions_section(self):
        response = self.client.get(f"/product/{PRINT_PK}")

        self.assertNotContains(response, "Other editions and formats")
        # The formats are still reachable -- via the chooser, which says
        # more about each of them than that list did.
        self.assertContains(response, f'href="/product/{EBOOK_PK}"')


# ---------------------------------------------------------------------------
# 5. What grouping must NOT do
# ---------------------------------------------------------------------------


class GroupingIsPresentationOnlyTest(TestCase):
    """A group merges cards, never SKUs."""

    fixtures = ["initial_products"]

    def test_each_format_keeps_its_own_identifiers(self):
        gtins = [Product.objects.get(pk=pk).get_gtin() for pk in DC4K_PKS]

        self.assertEqual(len(set(gtins)), len(DC4K_PKS))
        self.assertTrue(all(gtins))

    def test_each_format_keeps_its_own_price(self):
        prices = [Product.objects.get(pk=pk).price for pk in DC4K_PKS]

        self.assertEqual(prices, [2000, 3442, 1299])

    @override_settings(THUMBNAIL_DEBUG=False)
    def test_each_format_keeps_its_own_product_page(self):
        for pk in DC4K_PKS:
            with self.subTest(pk=pk):
                response = self.client.get(f"/product/{pk}")

                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response, escape(Product.objects.get(pk=pk).name))

    def test_each_format_is_still_its_own_offer_in_the_merchant_feed(self):
        """Google is being told about three products for sale, and it is:
        three ISBNs, three prices, three fulfilment stories. The listing
        collapse is a fact about this site's HTML and nothing else."""
        body = self.client.get("/google_products.xml").content.decode()

        for pk in DC4K_PKS:
            with self.subTest(pk=pk):
                self.assertIn(f"<g:id>{pk}</g:id>", body)
                self.assertIn(
                    f"<g:gtin>{Product.objects.get(pk=pk).get_gtin()}</g:gtin>",
                    body)

    def test_deleting_a_group_does_not_delete_its_products(self):
        """SET_NULL, not CASCADE: "stop showing these as one work" must not
        mean "delete the three books"."""
        ProductGroup.objects.get(pk=DC4K_GROUP_PK).delete()

        self.assertEqual(
            Product.objects.filter(pk__in=DC4K_PKS).count(), len(DC4K_PKS))
        for pk in DC4K_PKS:
            with self.subTest(pk=pk):
                product = Product.objects.get(pk=pk)
                self.assertIsNone(product.group_id)
                self.assertEqual(product.listing_name(), product.name)


# ---------------------------------------------------------------------------
# 6. Seeding
# ---------------------------------------------------------------------------


class SeedProductsGroupTest(TestCase):
    """The deploy path: start-server.sh runs this under `set -euo pipefail`,
    so anything it rejects stops the primary pod from booting."""

    def seed(self):
        out = StringIO()
        with mock.patch("main.models.Payments"):
            call_command("seed_products", stdout=out)
        return out.getvalue()

    def test_a_clean_deploy_creates_the_group_and_links_the_members(self):
        output = self.seed()

        self.assertIn(f"Created product group pk={DC4K_GROUP_PK}", output)
        for pk in DC4K_PKS:
            with self.subTest(pk=pk):
                self.assertEqual(
                    Product.objects.get(pk=pk).group_id, DC4K_GROUP_PK)

    def test_the_labels_and_order_arrive_from_the_fixture(self):
        self.seed()

        self.assertEqual(
            [(p.format_label, p.format_order)
             for p in ProductGroup.objects.get(pk=DC4K_GROUP_PK).members()],
            [("Paperback", 0), ("Executive Edition", 1), ("E-book", 2)])

    def test_seeding_twice_changes_nothing_and_duplicates_nothing(self):
        self.seed()
        before = list(
            Product.objects.order_by("pk").values_list(
                "pk", "group_id", "format_label", "format_order"))

        self.seed()

        self.assertEqual(ProductGroup.objects.count(), 1)
        self.assertEqual(
            list(Product.objects.order_by("pk").values_list(
                "pk", "group_id", "format_label", "format_order")),
            before)

    def test_a_no_op_reseed_does_not_claim_it_updated_the_group(self):
        """.update() returns rows MATCHED, so a log line keyed off it would
        say "Updated" on every deploy for an identical fixture row -- and
        the deploy log is how an operator learns what a seed changed."""
        self.seed()

        output = self.seed()

        self.assertNotIn(f"Updated product group pk={DC4K_GROUP_PK}", output)
        self.assertNotIn(f"Created product group pk={DC4K_GROUP_PK}", output)

    def test_a_renamed_group_is_reported_and_reseeded(self):
        """The other half, so the quiet path above is not quiet by being
        inert: a real difference is both written and said."""
        self.seed()
        ProductGroup.objects.filter(pk=DC4K_GROUP_PK).update(
            name="Renamed in the admin")

        output = self.seed()

        self.assertIn(f"Updated product group pk={DC4K_GROUP_PK}", output)
        self.assertEqual(
            ProductGroup.objects.get(pk=DC4K_GROUP_PK).name, WORK_NAME)

    def test_the_collapsed_listing_works_off_seeded_data(self):
        """End to end on the real deploy path rather than off loaddata: the
        fixture spells the FK `group: 200`, and neither seed write path
        accepts that spelling without the rewrite in _to_attnames."""
        self.seed()

        collapsed = [
            product.pk for product in
            Product.objects.exclude(noorder=True)
            .order_by_release_date().collapse_format_groups()]

        self.assertEqual([pk for pk in collapsed if pk in DC4K_PKS],
                         [PRINT_PK])


class SeedProductsGroupValidationTest(TestCase):
    """The up-front checks, so a bad fixture fails with a sentence."""

    def test_a_product_naming_an_unknown_group_is_rejected(self):
        with self.assertRaisesRegex(CommandError, r"names group pk=999"):
            _validate_group_links({104: 999}, {200})

    def test_a_known_group_passes(self):
        _validate_group_links({104: 200, 105: 200}, {200})

    def test_group_is_recognised_as_a_foreign_key(self):
        """Read off the model, so a second FK added to Product later is
        rewritten automatically rather than taking the deploy down."""
        self.assertIn("group", _product_fk_field_names())

    def test_a_foreign_key_value_is_rewritten_to_its_attname(self):
        """``Product(group=200)`` raises and ``.update(group=200)`` is no
        better; both take group_id."""
        self.assertEqual(
            _to_attnames({"group": 200, "name": "A book"}, {"group"}),
            {"group_id": 200, "name": "A book"})

    def test_the_rewrite_would_be_needed(self):
        """Guards the guard: without it the create path really does fail."""
        with self.assertRaises(ValueError):
            Product(pk=999, name="Bad", group=200)


# ---------------------------------------------------------------------------
# 7. Schema
# ---------------------------------------------------------------------------


class ProductFormatGroupSchemaTest(TestCase):
    """The columns 0025 adds to main_product, and a rolling deploy.

    `migrate` runs on web-primary while pods on the previous image keep
    writing Products, so for the length of every rollout the new schema is
    written by code that has never heard of these columns. The two routes to
    surviving that are NULLable (group_id) and NOT NULL with a database
    default that STAYS in the schema (format_label, format_order); the
    postgres-DDL half of this argument is in test_schema.py, and this is the
    runtime half plus the model declarations.
    """

    def test_the_new_columns_are_absent_from_the_old_code_column_list(self):
        """Anti-vacuity for the insert below: if a future edit adds them to
        that list, the raw INSERT stops exercising anything."""
        for column in ("group_id", "format_label", "format_order"):
            with self.subTest(column=column):
                self.assertNotIn(column, OLD_CODE_PRODUCT_COLUMNS)

    def test_an_old_pod_can_still_write_a_product(self):
        columns = OLD_CODE_PRODUCT_COLUMNS
        values: list = [None] * len(columns)
        for name, value in (
                ("preorder_only", False), ("noorder", False),
                ("backorder", False), ("name", "Old-code product"),
                ("page", ""), ("price", 1000), ("image", ""),
                ("image_name", ""),
                ("tax_code", Product.TaxTypes.GOODS),
                ("cat", Product.Categories.MERCH),
                ("mode", Product.Modes.PAYMENT),
                ("description", "Written by an old pod.")):
            values[columns.index(name)] = value

        with connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO main_product ({", ".join(columns)}) VALUES '
                f'({", ".join(["%s"] * len(columns))})',
                values)

        product = Product.objects.get(name="Old-code product")
        self.assertIsNone(product.group_id)
        self.assertEqual(product.format_label, "")
        self.assertEqual(product.format_order, 0)

    def test_group_is_nullable(self):
        self.assertTrue(Product._meta.get_field("group").null)

    def test_the_not_null_columns_carry_a_database_default(self):
        for name, expected in (("format_label", ""), ("format_order", 0)):
            with self.subTest(name=name):
                field = Product._meta.get_field(name)

                self.assertFalse(field.null)
                self.assertEqual(field.db_default, expected)
