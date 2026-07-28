"""The four Product additions in migration 0013, and what they must not do.

One module because the four shipped in one migration, and because three of
the four are only correct in terms of what they leave alone: an out-of-date
edition still sells, an MSRP that is not a saving is not shown, and an e-book
cross-link never puts anything in a cart. Most of what is asserted here is
therefore an absence, and an absence is exactly the kind of assertion that
rots into a tautology -- so each of these has been checked against the state
where the rule does not exist, and fails there.
"""

from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from main.management.commands.seed_products import (
    _product_m2m_field_names, _validate_m2m_links)
from main.models import Product
from main.tests.base import EBOOK_PK


PRINT_PK = 104
EXECUTIVE_PK = 105
NOT_SOLD_HERE_PK = 107
OUT_OF_DATE_PKS = (100, 102)


class ProductFactoryMixin:
    """Products that never reach Stripe.

    Product.save() mints a Stripe product id for any row without one, so
    every test here goes through this rather than Product.objects.create().
    """

    def make_product(self, **kwargs) -> Product:
        kwargs.setdefault("name", "Test product")
        kwargs.setdefault("external_product_id", "prod_test")
        with mock.patch("main.models.Payments") as payments:
            payments.create_product.return_value = "prod_test"
            return Product.objects.create(**kwargs)


# ---------------------------------------------------------------------------
# 1. out_of_date
# ---------------------------------------------------------------------------


class OutOfDateFixtureTest(TestCase):
    """The two editions the owner flagged, and only those two."""

    fixtures = ["initial_products"]

    def test_the_flagged_editions_are_flagged(self):
        for pk in OUT_OF_DATE_PKS:
            with self.subTest(pk=pk):
                self.assertTrue(Product.objects.get(pk=pk).out_of_date)

    def test_no_other_catalogue_row_is_flagged(self):
        # Notably including pk 107, whose own copy calls it "comprehensively
        # out of date" -- the owner asked for 100 and 102, and a flag that
        # spreads to whatever looks old is a flag nobody controls.
        unexpected = sorted(
            Product.objects.filter(out_of_date=True)
            .exclude(pk__in=OUT_OF_DATE_PKS)
            .values_list("pk", flat=True))

        self.assertEqual(unexpected, [])

    def test_the_flag_defaults_off_for_a_new_product(self):
        self.assertFalse(Product(name="Brand new").out_of_date)


class OutOfDateIsAdvisoryOnlyTest(ProductFactoryMixin, TestCase):
    """The load-bearing half: the flag must change nothing but the copy.

    Written as a comparison between two products identical except for the
    flag, rather than as assertions about the flagged one alone. A test that
    only said "pk 100 is purchasable" would keep passing if out_of_date were
    later wired into is_purchasable() and pk 100 happened to be exempt.
    """

    fixtures = ["initial_products"]

    def setUp(self):
        common = dict(
            price=3999, cat=Product.Categories.BOOKS, stock=5,
            delivery_type=Product.DeliveryTypes.PHYSICAL)
        self.current = self.make_product(
            pk=300, name="Current edition", out_of_date=False, **common)
        self.dated = self.make_product(
            pk=301, name="Dated edition", out_of_date=True, **common)

    def test_an_out_of_date_edition_is_still_purchasable(self):
        self.assertTrue(self.dated.is_purchasable())
        self.assertEqual(
            self.dated.is_purchasable(), self.current.is_purchasable())

    def test_the_flag_changes_nothing_the_storefront_reads(self):
        for method in ("is_purchasable", "is_out_of_stock", "buy_text",
                       "get_availability", "stock_description",
                       "get_display_price", "get_feed_price",
                       "is_physical_good", "is_digitally_fulfilled"):
            with self.subTest(method=method):
                self.assertEqual(
                    getattr(self.dated, method)(),
                    getattr(self.current, method)(),
                    f"out_of_date changed {method}(), which is order- or "
                    "feed-visible; it is supposed to be advisory only")

    def test_the_flag_does_not_set_noorder(self):
        """The specific mistake to avoid: quietly delisting the book."""
        self.assertFalse(self.dated.noorder)
        for pk in OUT_OF_DATE_PKS:
            with self.subTest(pk=pk):
                self.assertFalse(Product.objects.get(pk=pk).noorder)


class OutOfDateNoticeRenderingTest(TestCase):
    """The advisory notice on the product page."""

    fixtures = ["initial_products"]

    def test_a_flagged_product_page_carries_the_notice(self):
        for pk in OUT_OF_DATE_PKS:
            with self.subTest(pk=pk):
                response = self.client.get(f"/product/{pk}")

                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response, escape(Product.OUT_OF_DATE_NOTICE))

    def test_an_unflagged_product_page_does_not(self):
        response = self.client.get(f"/product/{PRINT_PK}")

        self.assertNotContains(response, escape(Product.OUT_OF_DATE_NOTICE))

    def test_the_notice_says_it_is_still_available(self):
        """The words matter: this is the difference between an advisory and
        a delisting, and it is the sentence a customer reads before deciding
        the book is not for sale."""
        self.assertIn("remains available", Product.OUT_OF_DATE_NOTICE)
        self.assertIn("historical purposes", Product.OUT_OF_DATE_NOTICE)

    def test_a_flagged_product_page_still_offers_the_buy_button(self):
        """The whole point, asserted through the rendered page.

        Both flagged rows are physical books, so they need stock to be
        purchasable at all; stock is admin-owned and starts at 0, so set it
        here rather than asserting against whatever the fixture left.
        """
        Product.objects.filter(pk__in=OUT_OF_DATE_PKS).update(stock=3)

        for pk in OUT_OF_DATE_PKS:
            with self.subTest(pk=pk):
                response = self.client.get(f"/product/{pk}")

                self.assertContains(response, escape(
                    Product.OUT_OF_DATE_NOTICE))
                self.assertContains(response, "Add to Cart")
                self.assertNotContains(response, "Not sold here")


class OutOfDateListingVisibilityTest(TestCase):
    """A flagged edition stays in the catalogue and in the feed."""

    fixtures = ["initial_products"]

    def test_flagged_products_still_appear_in_the_product_listing(self):
        response = self.client.get("/products")

        self.assertEqual(response.status_code, 200)
        for pk in OUT_OF_DATE_PKS:
            with self.subTest(pk=pk):
                self.assertContains(
                    response, escape(Product.objects.get(pk=pk).name))

    def test_flagged_products_still_appear_in_the_merchant_feed(self):
        response = self.client.get("/google_products.xml")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for pk in OUT_OF_DATE_PKS:
            with self.subTest(pk=pk):
                self.assertIn(f"<g:id>{pk}</g:id>", body)


# ---------------------------------------------------------------------------
# 2. msrp
# ---------------------------------------------------------------------------


class MsrpDisplayRuleTest(TestCase):
    """Strictly greater than the price, or nothing at all."""

    fixtures = ["initial_products"]

    def product(self, price, msrp):
        return Product(name="Priced product", price=price, msrp=msrp)

    def test_an_msrp_above_the_price_is_shown(self):
        product = self.product(price=3999, msrp=4999)

        self.assertTrue(product.show_msrp())
        self.assertEqual(product.get_msrp_display(), "49.99")

    def test_an_msrp_equal_to_the_price_is_not_shown(self):
        """Equal is not a saving, and striking it through implies one."""
        self.assertFalse(self.product(price=3999, msrp=3999).show_msrp())

    def test_an_msrp_below_the_price_is_not_shown(self):
        """The one that would actively mislead: a struck-through number
        lower than what we charge advertises the reverse of a discount."""
        self.assertFalse(self.product(price=3999, msrp=2999).show_msrp())

    def test_an_msrp_one_cent_above_the_price_is_shown(self):
        """Pins the boundary as `>` and not `>=` from the other side."""
        self.assertTrue(self.product(price=3999, msrp=4000).show_msrp())

    def test_an_unset_msrp_is_not_shown_and_does_not_raise(self):
        product = self.product(price=3999, msrp=None)

        self.assertFalse(product.show_msrp())

    def test_msrp_is_read_in_cents_like_price(self):
        """Same units as price. A field that quietly meant dollars would
        make every MSRP a hundredfold too small and so never render."""
        product = self.product(price=1, msrp=100)

        self.assertEqual(product.get_msrp_display(), "1.00")
        self.assertEqual(product.get_display_price(), "0.01")

    def test_no_catalogue_row_ships_an_msrp(self):
        """Deliberate: no MSRP figure has been supplied for any of these
        books, and inventing one would put a false "was" price on the site.
        The mechanism ships; the numbers are the owner's to enter.
        """
        self.assertEqual(
            list(Product.objects.exclude(msrp=None).values_list(
                "pk", flat=True)),
            [])


class MsrpRenderingTest(ProductFactoryMixin, TestCase):
    """The strikethrough on the product page."""

    fixtures = ["initial_products"]

    def page(self, **kwargs):
        kwargs.setdefault("price", 3999)
        kwargs.setdefault("cat", Product.Categories.BOOKS)
        kwargs.setdefault("stock", 5)
        product = self.make_product(pk=310, name="MSRP book", **kwargs)
        return self.client.get(product.get_absolute_url())

    def test_a_higher_msrp_renders_struck_through(self):
        response = self.page(msrp=4999)

        self.assertContains(response, "<s>49.99</s>", html=False)
        self.assertContains(response, "39.99")

    def test_an_equal_msrp_renders_nothing(self):
        self.assertNotContains(self.page(msrp=3999), "<s>")

    def test_a_lower_msrp_renders_nothing(self):
        self.assertNotContains(self.page(msrp=2999), "<s>")

    def test_an_unset_msrp_renders_nothing(self):
        self.assertNotContains(self.page(msrp=None), "<s>")

    def test_a_not_sold_here_product_shows_no_strikethrough(self):
        """pk 107 has price 0, so any MSRP at all would be "greater". Its
        page says "Not sold here" and shows no price, so a struck-through
        number beside that would advertise a discount on a thing nobody can
        buy."""
        Product.objects.filter(pk=NOT_SOLD_HERE_PK).update(msrp=4999)

        response = self.client.get(f"/product/{NOT_SOLD_HERE_PK}")

        self.assertContains(response, "Not sold here")
        self.assertNotContains(response, "<s>")


class MsrpStaysOutOfTheJavascriptTotalTest(ProductFactoryMixin, TestCase):
    """The landmine, pinned so a later edit cannot step on it.

    single-product.html computes its running total with
    parseFloat("{{ product.get_display_price }}"), which already yields NaN
    on a pre-order row because get_display_price() returns "Pre-order: 30.00".
    That bug predates this branch and is untouched by it. What this test
    guarantees is that MSRP did not become a second value on that path.
    """

    def test_the_total_script_reads_the_price_and_not_the_msrp(self):
        product = self.make_product(
            pk=311, name="MSRP book", price=3999, msrp=4999,
            cat=Product.Categories.BOOKS, stock=5)

        body = self.client.get(product.get_absolute_url()).content.decode()

        self.assertIn('parseFloat("39.99")', body)
        self.assertNotIn('parseFloat("49.99")', body)
        # And exactly one parseFloat of a rendered price, not two.
        self.assertEqual(body.count("parseFloat(\""), 1)

    def test_a_preorder_row_with_an_msrp_is_no_worse_than_before(self):
        """Explicitly documents the pre-existing NaN rather than pretending
        it is fixed: the point is that adding an MSRP does not add a second
        unparseable value, and does not make the MSRP itself reachable."""
        product = self.make_product(
            pk=312, name="Preorder book", price=3000, msrp=4000,
            preorder_only=True, cat=Product.Categories.BOOKS)

        body = self.client.get(product.get_absolute_url()).content.decode()

        # Unchanged pre-existing behaviour: the price string is still the
        # human one, and still the only thing parseFloat sees.
        self.assertIn('parseFloat("Pre-order: 30.00")', body)
        self.assertEqual(body.count("parseFloat(\""), 1)
        # The MSRP rendered as markup, nowhere near the script.
        self.assertIn("<s>40.00</s>", body)


# ---------------------------------------------------------------------------
# 3. x_links
# ---------------------------------------------------------------------------


class CrossLinkFixtureTest(TestCase):
    """The three DC4K SKUs all cross-reference each other."""

    fixtures = ["initial_products"]

    def linked(self, pk):
        return sorted(
            Product.objects.get(pk=pk).x_links.values_list("pk", flat=True))

    def test_the_three_dc4k_skus_reference_each_other(self):
        self.assertEqual(self.linked(PRINT_PK), [EXECUTIVE_PK, EBOOK_PK])
        self.assertEqual(self.linked(EXECUTIVE_PK), [PRINT_PK, EBOOK_PK])
        self.assertEqual(self.linked(EBOOK_PK), [PRINT_PK, EXECUTIVE_PK])

    def test_the_relation_is_symmetrical(self):
        """Not incidental to the seed data: it is the field's definition, and
        it is what makes a one-sided fixture edit impossible to ship."""
        field = Product._meta.get_field("x_links")

        self.assertTrue(field.remote_field.symmetrical)

    def test_the_oreilly_titles_are_not_cross_linked_to_anything(self):
        for pk in (100, 101, 102, 103, NOT_SOLD_HERE_PK):
            with self.subTest(pk=pk):
                self.assertEqual(self.linked(pk), [])

    def test_no_product_cross_links_to_itself(self):
        for product in Product.objects.all():
            with self.subTest(pk=product.pk):
                self.assertNotIn(
                    product.pk,
                    product.x_links.values_list("pk", flat=True))


class CrossLinkRenderingTest(TestCase):
    """Cross-links render as navigation, and are labelled by their target."""

    fixtures = ["initial_products"]

    def test_the_print_page_links_to_its_siblings_pages(self):
        response = self.client.get(f"/product/{PRINT_PK}")

        for pk in (EXECUTIVE_PK, EBOOK_PK):
            with self.subTest(pk=pk):
                self.assertContains(
                    response, f'href="{reverse("product", kwargs={"pk": pk})}"')
                self.assertContains(
                    response, escape(Product.objects.get(pk=pk).name))

    def test_a_product_with_no_cross_links_renders_no_section(self):
        response = self.client.get("/product/101")

        self.assertNotContains(response, "Other editions and formats")

    def test_cross_links_are_ordered_deterministically(self):
        links = Product.objects.get(pk=PRINT_PK).get_cross_links()

        self.assertEqual(
            [url for _, url in links],
            [reverse("product", kwargs={"pk": EXECUTIVE_PK}),
             reverse("product", kwargs={"pk": EBOOK_PK})])


class CrossLinksAreNotOffersTest(ProductFactoryMixin, TestCase):
    """x_links is navigation, so it is deliberately NOT purchase-gated.

    The reverse of the rule that governs ebook_x_links below, and separated
    from it for that reason: a "this also exists as a hardback we do not
    stock" pointer stays a true statement, while a *buy* button for the same
    row would not.
    """

    fixtures = ["initial_products"]

    def test_a_cross_link_to_an_unpurchasable_row_is_still_listed(self):
        product = self.make_product(
            pk=320, name="Sibling of a listed-only title", price=1000)
        not_sold = Product.objects.get(pk=NOT_SOLD_HERE_PK)
        self.assertFalse(not_sold.is_purchasable())

        product.x_links.set([not_sold])

        self.assertEqual(
            product.get_cross_links(),
            [(not_sold.name, not_sold.get_absolute_url())])

    def test_a_cross_link_url_is_an_on_site_product_page(self):
        """Never an external retailer: those are get_alt_links()' job, and
        a navigation list that mixed the two would make "other editions"
        mean two different things in one list."""
        for _, url in Product.objects.get(pk=PRINT_PK).get_cross_links():
            with self.subTest(url=url):
                self.assertTrue(url.startswith("/product/"), url)


# ---------------------------------------------------------------------------
# 4. ebook_x_links
# ---------------------------------------------------------------------------


class EbookCrossLinkFixtureTest(TestCase):
    """A separate, directional M2M -- not a view over x_links."""

    fixtures = ["initial_products"]

    def test_it_is_a_distinct_field_from_x_links(self):
        names = {field.name for field in Product._meta.get_fields()
                 if field.many_to_many and not field.auto_created}

        self.assertEqual(names, {"x_links", "ebook_x_links"})

    def test_it_is_not_symmetrical(self):
        """Directional on purpose: symmetrical would make the e-book row
        render "get the e-book" buttons pointing at the two paperbacks."""
        field = Product._meta.get_field("ebook_x_links")

        self.assertFalse(field.remote_field.symmetrical)

    def test_both_print_editions_point_at_the_ebook(self):
        for pk in (PRINT_PK, EXECUTIVE_PK):
            with self.subTest(pk=pk):
                self.assertEqual(
                    list(Product.objects.get(pk=pk)
                         .ebook_x_links.values_list("pk", flat=True)),
                    [EBOOK_PK])

    def test_the_ebook_points_at_no_ebook_of_its_own(self):
        self.assertEqual(
            list(Product.objects.get(pk=EBOOK_PK)
                 .ebook_x_links.values_list("pk", flat=True)),
            [])

    def test_the_ebook_knows_which_print_rows_point_at_it(self):
        self.assertEqual(
            sorted(Product.objects.get(pk=EBOOK_PK)
                   .print_x_links.values_list("pk", flat=True)),
            [PRINT_PK, EXECUTIVE_PK])


class EbookCrossLinkSafetyTest(ProductFactoryMixin, TestCase):
    """The checkout-incident rules, one test each.

    Every one of these is about what the button on a PRINT page must not do.
    """

    fixtures = ["initial_products"]

    def setUp(self):
        self.print_product = Product.objects.get(pk=PRINT_PK)
        self.ebook = Product.objects.get(pk=EBOOK_PK)

    def test_the_print_page_renders_the_ebooks_affordance(self):
        response = self.client.get(f"/product/{PRINT_PK}")

        self.assertContains(
            response,
            escape(f"{Product.EBOOK_CROSS_LINK_PREFIX}: {self.ebook.name}"))

    def test_the_button_points_at_the_ebooks_own_product_page(self):
        self.assertEqual(
            self.print_product.get_ebook_cross_links(),
            [(f"{Product.EBOOK_CROSS_LINK_PREFIX}: {self.ebook.name}",
              f"/product/{EBOOK_PK}")])

    def test_the_button_is_not_an_add_to_cart_for_the_print_pk(self):
        """The incident this exists to prevent: selling a paperback as a
        download. The page's only add-to-cart action is its own, and there
        is exactly one of them."""
        import re

        body = self.client.get(f"/product/{PRINT_PK}").content.decode()

        # Every add-to-cart target on the page, whatever markup carries it.
        targets = re.findall(r"add-to-cart/(\d+)/\d+", body)

        self.assertEqual(targets, [str(PRINT_PK)], body)

    def test_the_button_does_not_post_an_add_to_cart_for_the_ebook_pk(self):
        """The other half: no silent substitution of a different product."""
        body = self.client.get(f"/product/{PRINT_PK}").content.decode()

        self.assertNotIn(f"add-to-cart/{EBOOK_PK}/", body)

    def test_the_button_names_the_product_it_lands_on(self):
        """A visitor pressing it must know which row they are going to."""
        for label, _ in self.print_product.get_ebook_cross_links():
            with self.subTest(label=label):
                self.assertIn(self.ebook.name, label)

    def test_the_label_does_not_promise_a_cart_action(self):
        self.assertNotIn("cart", Product.EBOOK_CROSS_LINK_PREFIX.lower())

    def test_an_unpurchasable_ebook_renders_no_button(self):
        """Where this diverges from get_alt_links(), which is ungated and
        renders a Buy button on pk 107's "not sold here" page. That hole is
        pre-existing; it is not propagated here."""
        # .update() rather than .save(): the fixture leaves this row's
        # external_product_id unset, so saving it would go and mint one at
        # Stripe.
        Product.objects.filter(pk=EBOOK_PK).update(noorder=True)
        self.ebook.refresh_from_db()
        self.assertFalse(self.ebook.is_purchasable())

        self.assertEqual(self.print_product.get_ebook_cross_links(), [])
        self.assertNotContains(
            self.client.get(f"/product/{PRINT_PK}"),
            escape(Product.EBOOK_CROSS_LINK_PREFIX))

    def test_an_out_of_stock_ebook_link_would_also_be_suppressed(self):
        """The gate is is_purchasable(), not just `noorder`, so it tracks
        every reason a product cannot be bought."""
        physical = self.make_product(
            pk=330, name="A print sibling", price=1000,
            cat=Product.Categories.BOOKS, stock=0,
            delivery_type=Product.DeliveryTypes.PHYSICAL)
        buyer = self.make_product(pk=331, name="Buyer page", price=1000)
        buyer.ebook_x_links.set([physical])

        self.assertFalse(physical.is_purchasable())
        self.assertEqual(buyer.get_ebook_cross_links(), [])

    def test_a_self_reference_renders_nothing(self):
        product = self.make_product(pk=332, name="Self-linked", price=1000)
        product.ebook_x_links.add(product)

        self.assertEqual(product.get_ebook_cross_links(), [])

    def test_the_ebook_page_offers_no_ebook_cross_link_to_itself(self):
        response = self.client.get(f"/product/{EBOOK_PK}")

        self.assertNotContains(
            response, escape(Product.EBOOK_CROSS_LINK_PREFIX))

    def test_ebook_cross_links_stay_out_of_get_alt_links(self):
        """get_alt_links() is the retailer list, and test_dc4k asserts it is
        empty for these three rows because no retailer stocks them. An
        on-site cross-link is not a retailer claim and must not leak in."""
        self.assertEqual(self.print_product.get_alt_links(), [])


# ---------------------------------------------------------------------------
# Seeding many-to-many fields
# ---------------------------------------------------------------------------


class SeedProductsCrossLinkTest(TestCase):
    """The blocker: neither seed write path can carry an M2M.

    ``Product(pk=pk, **fields)`` raises on a related-manager keyword and
    ``QuerySet.update()`` cannot write one, so the fixture's cross-link keys
    are split out and applied by a second ``.set()`` pass. These tests cover
    the two properties that pass has to have -- correct after a first seed of
    an empty database, and unchanged and undusplicated after a second.
    """

    def seed(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with mock.patch("main.models.Payments") as payments:
            payments.create_product.return_value = "prod_seeded"
            call_command("seed_products", stdout=out)
        return out.getvalue()

    def links(self):
        return {
            product.pk: (
                sorted(product.x_links.values_list("pk", flat=True)),
                sorted(product.ebook_x_links.values_list("pk", flat=True)))
            for product in Product.objects.order_by("pk")
        }

    def through_row_counts(self):
        """Raw through-table rows, which is where duplication would show.

        Counted through the through models rather than the managers: a
        manager de-duplicates on read, so it would hide exactly the failure
        this is looking for.
        """
        return {
            "x_links": Product.x_links.through.objects.count(),
            "ebook_x_links": Product.ebook_x_links.through.objects.count(),
        }

    def test_the_constructor_really_cannot_take_an_m2m(self):
        """Anti-vacuity for this whole class: prove the blocker is real, so
        the second pass is demonstrably load-bearing rather than ceremony
        around something that would have worked anyway.

        The message is asserted, not just the type. ``Product(x_links=...)``
        raises TypeError either way -- before this field existed it was the
        ordinary "unexpected keyword arguments" one -- and it is specifically
        the many-to-many refusal that seed_products has to work around.
        """
        with self.assertRaisesMessage(
                TypeError, "Direct assignment to the forward side of a "
                           "many-to-many set is prohibited"):
            Product(pk=400, name="Direct", x_links=[EBOOK_PK])

    def test_update_really_cannot_take_an_m2m(self):
        """The other write path, and the same conclusion."""
        Product.objects.bulk_create([Product(pk=401, name="Existing")])

        from django.core.exceptions import FieldError

        with self.assertRaisesMessage(
                FieldError, "Cannot update model field "
                            "<django.db.models.fields.related."
                            "ManyToManyField: x_links>"):
            Product.objects.filter(pk=401).update(x_links=[EBOOK_PK])

    def test_seeding_an_empty_database_creates_the_cross_links(self):
        Product.objects.all().delete()
        self.assertEqual(Product.objects.count(), 0)

        self.seed()

        links = self.links()
        self.assertEqual(links[PRINT_PK], ([EXECUTIVE_PK, EBOOK_PK],
                                           [EBOOK_PK]))
        self.assertEqual(links[EXECUTIVE_PK], ([PRINT_PK, EBOOK_PK],
                                               [EBOOK_PK]))
        self.assertEqual(links[EBOOK_PK], ([PRINT_PK, EXECUTIVE_PK], []))

    def test_seeding_twice_is_idempotent_and_duplicates_nothing(self):
        Product.objects.all().delete()

        self.seed()
        after_first = self.links()
        counts_after_first = self.through_row_counts()

        self.seed()

        self.assertEqual(self.links(), after_first)
        self.assertEqual(self.through_row_counts(), counts_after_first)

    def test_the_symmetrical_field_stores_both_directions_exactly_once(self):
        """Three mutually linked rows are three pairs, and Django's
        symmetrical M2M stores each pair from both ends: six rows, no more."""
        Product.objects.all().delete()
        self.seed()
        self.seed()

        self.assertEqual(self.through_row_counts(),
                         {"x_links": 6, "ebook_x_links": 2})

    def test_reseeding_repairs_a_link_deleted_out_of_band(self):
        """A .set() pass has to converge on the fixture, not merely avoid
        making things worse."""
        Product.objects.all().delete()
        self.seed()
        Product.objects.get(pk=PRINT_PK).x_links.clear()

        self.seed()

        self.assertEqual(
            sorted(Product.objects.get(pk=PRINT_PK)
                   .x_links.values_list("pk", flat=True)),
            [EXECUTIVE_PK, EBOOK_PK])

    def test_seeding_reaches_no_stripe_call(self):
        """The .set() pass must not have reintroduced a Product.save().

        seed_products runs during pod startup under `set -e`, so a Stripe
        outage reaching it stops the primary booting.
        """
        from io import StringIO

        from django.core.management import call_command

        Product.objects.all().delete()

        with mock.patch("main.payments.stripe.Product.create") as create:
            create.side_effect = AssertionError(
                "seed_products reached the Stripe API")
            call_command("seed_products", stdout=StringIO())

        create.assert_not_called()
        self.assertEqual(
            sorted(Product.objects.get(pk=PRINT_PK)
                   .x_links.values_list("pk", flat=True)),
            [EXECUTIVE_PK, EBOOK_PK])

    def test_the_m2m_field_set_is_read_off_the_model(self):
        """So a fifth M2M added to Product later is handled automatically
        rather than taking the deploy down the first time it appears in the
        fixture."""
        self.assertEqual(
            _product_m2m_field_names(), {"x_links", "ebook_x_links"})


class SeedProductsCrossLinkValidationTest(TestCase):
    """Bad cross-link data is rejected with a message, not an IntegrityError.

    Each of these would otherwise fail during the deploy's seed step, where
    `set -euo pipefail` turns it into a pod that does not boot.
    """

    def known(self):
        return {100, 104, 105, 106}

    def test_a_link_to_a_nonexistent_pk_is_rejected(self):
        from django.core.management.base import CommandError

        with self.assertRaisesMessage(CommandError, "pk=999"):
            _validate_m2m_links({104: {"x_links": [999]}}, self.known())

    def test_a_self_link_is_rejected(self):
        from django.core.management.base import CommandError

        with self.assertRaisesMessage(CommandError, "itself"):
            _validate_m2m_links({104: {"x_links": [104]}}, self.known())

    def test_a_one_sided_symmetrical_declaration_is_rejected(self):
        """The subtle one. .set() replaces a row's whole link set and edits
        both ends of a symmetrical field, so 104 saying [105] while 105 says
        [] creates the pair and then deletes it -- and which you get depends
        on fixture order."""
        from django.core.management.base import CommandError

        with self.assertRaisesMessage(CommandError, "symmetrical"):
            _validate_m2m_links(
                {104: {"x_links": [105]}, 105: {"x_links": []}},
                self.known())

    def test_a_symmetrical_pair_that_agrees_is_accepted(self):
        _validate_m2m_links(
            {104: {"x_links": [105]}, 105: {"x_links": [104]}},
            self.known())

    def test_a_row_that_omits_the_key_is_not_required_to_declare_it(self):
        """Omission means "leave this relation alone", so .set() on the
        other side simply adds the link and nothing is contradicted."""
        _validate_m2m_links({104: {"x_links": [105]}}, self.known())

    def test_a_one_sided_directional_declaration_is_fine(self):
        """ebook_x_links is not symmetrical, so it takes no pairing -- which
        is exactly the fixture's shape for 104 -> 106."""
        _validate_m2m_links(
            {104: {"ebook_x_links": [106]}, 106: {"ebook_x_links": []}},
            self.known())

    def test_the_shipped_fixture_passes_validation(self):
        """CI catches a bad fixture edit, so the deploy never has to."""
        import yaml

        with open("main/fixtures/initial_products.yaml", "rb") as handle:
            entries = [entry for entry in yaml.safe_load(handle)
                       if entry.get("model") == "main.product"]

        m2m_names = _product_m2m_field_names()
        declared = {
            entry["pk"]: {
                name: list(entry["fields"][name])
                for name in sorted(set(entry["fields"]) & m2m_names)
            }
            for entry in entries
            if set(entry["fields"]) & m2m_names
        }

        # Anti-vacuity: the fixture really does declare cross-links, so this
        # is validating something.
        self.assertEqual(sorted(declared), [PRINT_PK, EXECUTIVE_PK, EBOOK_PK])

        _validate_m2m_links(declared, {entry["pk"] for entry in entries})
