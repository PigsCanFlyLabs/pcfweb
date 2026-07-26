"""Tests for the static and mostly-static pages."""

import re
from unittest import mock

from django.test import TestCase, override_settings

from main.models import Product


class StaticPagesTest(TestCase):
    def test_privacy_page_renders_privacy_template(self):
        response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "privacy.html")

    def test_tos_page_renders_tos_template(self):
        response = self.client.get("/tos")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tos.html")


@override_settings(THUMBNAIL_DEBUG=False)
class ServicesPageTest(TestCase):
    """The curated /services page that replaced the Product-backed listing.

    The old page listed Product rows with cat=SERVICES. Nothing here is a
    Product, so the point of these tests is that the page says what it offers
    without ever offering to sell it through the cart.
    """

    EXPECTED = [
        "Liberated Bread",
        "Apache Spark Consulting",
        "AI Consulting",
        "Fight Health Insurance",
    ]

    def page(self):
        response = self.client.get("/services")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_services_page_renders_the_services_template(self):
        response = self.client.get("/services")
        self.assertTemplateUsed(response, "services.html")

    def test_all_four_services_are_listed(self):
        html = self.page()
        for name in self.EXPECTED:
            with self.subTest(name=name):
                self.assertIn(name, html)

    def test_the_page_does_not_depend_on_product_rows(self):
        """The old queryset is gone, so an empty catalogue changes nothing.

        No fixtures on this class: if the page still read Product rows this
        would render an empty list.
        """
        self.assertEqual(Product.objects.count(), 0)

        html = self.page()

        for name in self.EXPECTED:
            self.assertIn(name, html)

    def test_a_service_product_row_no_longer_leaks_onto_the_page(self):
        """A SERVICES Product in the database must not appear here.

        Admin-created service rows still exist in production. The page is
        curated now, so one appearing would mean the old queryset came back.
        """
        with mock.patch("main.models.Payments") as payments:
            payments.create_product.return_value = "prod_svc"
            Product.objects.create(
                name="Some Admin Created Service",
                description="Left over in the database.",
                price=100000,
                cat=Product.Categories.SERVICES,
                external_product_id="prod_svc",
            )

        self.assertNotIn("Some Admin Created Service", self.page())

    def test_nothing_on_the_page_is_buyable(self):
        """Consulting is an enquiry, not a checkout."""
        html = self.page()

        self.assertNotIn("add-to-cart", html)
        self.assertNotIn("Add to Cart", html)
        self.assertNotIn("Buy", html)

    def test_both_consulting_entries_point_at_the_contact_page(self):
        html = self.page()

        self.assertEqual(html.count("Reach out for more info"), 2)
        self.assertIn('href="/contact"', html)

    def test_the_spark_credentials_avoid_the_first_spark_book_claim(self):
        """Learning Spark 1e (2015) is not provably the first Spark book.

        Fast Data Processing with Spark (Packt, 2013) predates it, so "the
        first Spark book" would be a publishing claim this page cannot back.
        "one of the first" is true either way. This test exists so the
        stronger phrasing cannot be reintroduced without someone deciding to.
        """
        html = self.page()

        self.assertIn(
            "From the co-author of Learning Spark (1st edition) and High "
            "Performance Spark (1st and 2nd editions), and one of the first "
            "books written about Apache Spark.",
            html)
        self.assertNotIn("the first Spark book", html)
        self.assertNotIn("the first book about Apache Spark", html)

    def test_fight_health_insurance_is_marked_as_a_separate_company(self):
        html = self.page()

        self.assertIn("A separate company that Holden is involved in", html)
        self.assertIn('href="https://www.fighthealthinsurance.com/"', html)

    def test_services_copy_is_not_copy_pasted_from_the_family_page(self):
        """/family says who they are, /services says what they offer.

        Both pages list Liberated Bread and Fight Health Insurance, which is
        intentional -- but shared sentences would mean one page was filled in
        from the other.
        """
        services_html = self.page()
        family_html = self.client.get("/family").content.decode()

        for family_sentence in (
                "A separate company and project that helps people appeal "
                "health insurance denials.",
                "The same company as Pigs Can Fly Labs, with its own site — "
                "not a separate company. Coming soon."):
            with self.subTest(sentence=family_sentence[:40]):
                self.assertIn(family_sentence, family_html)
                self.assertNotIn(family_sentence, services_html)

    def test_the_page_no_longer_advertises_fmt2(self):
        html = self.page()

        for gone in ("IP Transit", "IP transit", "FMT2", "colocation"):
            with self.subTest(text=gone):
                self.assertNotIn(gone, html)


@override_settings(THUMBNAIL_DEBUG=False)
class HomePageCardsTest(TestCase):
    """The #explore card grid, after the FMT2 services were retired.

    The cards are the homepage's only editorial surface, so what they
    advertise is what the company is saying it sells.
    """

    fixtures = ["initial_products"]

    DC4K_TITLE = "Distributed Computing 4 Kids (and Executives)"

    def homepage(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def explore_section(self):
        html = self.homepage()
        section = re.search(
            r'<section class="" id="explore">(.*?)</section>', html, re.DOTALL)
        self.assertIsNotNone(section, "explore section missing from homepage")
        assert section is not None  # for mypy
        return section.group(1)

    def test_the_homepage_no_longer_advertises_fmt2_network_services(self):
        # The offering is discontinued. History belongs on /about, not on a
        # card that reads as something you can still buy.
        html = self.homepage()

        for gone in ("IP Transit", "IP transit", "FMT2", "servers.jpg"):
            with self.subTest(text=gone):
                self.assertNotIn(gone, html)

    def test_the_testimonial_is_kept_but_no_longer_attributed_to_transit(self):
        html = self.homepage()

        # The quote itself survives, misspelling and all -- it is the owner's
        # voice, not a typo to correct.
        self.assertIn("nothing has exploded in a firey death", html)
        self.assertIn("One of our first customers.", html)
        self.assertNotIn("Our first IP transit customer", html)

    def test_liberated_bread_card_is_present_and_marked_coming_soon(self):
        section = self.explore_section()

        self.assertIn("Liberated Bread", section)
        self.assertIn(">Coming Soon</span>", section)

    def test_liberated_bread_links_to_the_same_place_as_the_family_page(self):
        """One destination, however the visitor arrives at it.

        Two hardcoded copies of this URL is how the homepage and /family end
        up pointing at different hosts after one of them is updated.
        """
        home_links = re.findall(
            r'<a href="(https://[^"]*liberatedbread[^"]*)"',
            self.explore_section())
        family_links = re.findall(
            r'<a href="(https://[^"]*liberatedbread[^"]*)"',
            self.client.get("/family").content.decode())

        self.assertEqual(len(home_links), 1)
        self.assertEqual(len(family_links), 1)
        self.assertEqual(home_links, family_links)

    def test_the_featured_book_card_links_to_the_seeded_product(self):
        section = self.explore_section()
        book = (Product.objects
                .filter(name__startswith=self.DC4K_TITLE)
                .order_by("pk").first())

        assert book is not None  # for mypy
        # Resolved from the object the view looked up, so this follows the
        # fixture rather than restating it.
        self.assertIn(f'href="/product/{book.pk}"', section)

    def test_the_featured_book_card_hardcodes_no_primary_key(self):
        """A pk in the template keeps resolving after the row moves.

        That is the failure mode worth guarding: it does not 404, it silently
        links to whatever product later holds that number. So assert the
        template source contains no literal pk, rather than asserting the
        rendered link -- which the test above already covers.
        """
        with open("main/templates/index.html") as handle:
            template = handle.read()

        self.assertNotIn("'product' 104", template)
        self.assertNotIn("'product' 105", template)
        self.assertNotIn("'product' 106", template)
        self.assertRegex(template, r"\{% url 'product' featured_book\.pk %\}")


@override_settings(THUMBNAIL_DEBUG=False)
class HomePageWithoutSeedDataTest(TestCase):
    """The homepage must still render against an unseeded catalogue.

    No fixtures here on purpose: a fresh database is what a new contributor
    and a first deploy both see, and the featured-book lookup returns None
    there. A card that assumed the row existed would 500 the front page.
    """

    def test_the_homepage_renders_with_no_products_at_all(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Product.objects.count(), 0)

    def test_the_featured_book_card_falls_back_to_the_books_listing(self):
        html = self.client.get("/").content.decode()

        self.assertIn("Distributed Computing 4 Kids", html)
        self.assertIn('href="/products/B"', html)


@override_settings(THUMBNAIL_DEBUG=False)
class PageSmokeTest(TestCase):
    """These pages had no coverage at all; at minimum they must render."""

    fixtures = ["initial_products"]

    def test_public_pages_render(self):
        for path in ["/", "/products", "/products/B", "/services", "/subscribe",
                     "/about", "/contact", "/returns", "/signup", "/login",
                     "/cart", "/family"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    @mock.patch("main.models.Payments")
    @mock.patch("main.views.Payments")
    def test_checkout_redirects_to_the_payment_provider(
            self, payments, model_payments):
        # Checkout now records a PENDING order first, so it needs a non-empty
        # cart -- and Payments.checkout returns (url, session_id).
        model_payments.create_product.return_value = "prod_test"
        model_payments.create_price.return_value = "price_test"
        payments.checkout.return_value = (
            "https://checkout.example/session", "cs_test_smoke")
        Product.objects.filter(pk=100).update(stock=1)
        self.client.post("/add-to-cart/100/1")

        response = self.client.post("/checkout")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], "https://checkout.example/session")

    def test_checkout_with_an_empty_cart_goes_back_to_the_cart(self):
        self.assertRedirects(self.client.post("/checkout"), "/cart")

    def test_checkout_rejects_a_get(self):
        # A GET creates a PENDING order and a Stripe session as a side effect,
        # which an <img> tag or a link prefetch can trigger cross-site with no
        # CSRF token involved.
        self.assertEqual(self.client.get("/checkout").status_code, 405)

    def test_logout_requires_a_login(self):
        response = self.client.get("/logout")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])


@override_settings(THUMBNAIL_DEBUG=False)
class FamilyPageTest(TestCase):
    """The family page lists the Pigs Can Fly Labs family of projects.

    The family is deliberately *not* framed as a flat list of companies: some
    members are their own companies, others are the same company as Pigs Can
    Fly Labs but with their own site. Each card carries a `kind` line saying
    which it is, and these tests pin that distinction -- Liberated Bread is the
    same company with its own site, Fight Health Insurance is a separate
    company as well as a project.

    The "Coming Soon" badge used to be driven by the absence of an outbound
    URL, which badged Pigs Can Fly Labs itself -- it is the parent company and
    this is its own site, so it deliberately links nowhere. Asserting the badge
    appears *somewhere* on the page could not catch that, so these tests pin
    the badge to a specific card.
    """

    COMING_SOON_BADGE = ">Coming Soon</span>"
    LIBERATED_BREAD_URL = "https://www.liberatedbread.com/"
    PROJECT_NAMES = [
        "Pigs Can Fly Labs", "Fight Health Insurance", "Liberated Bread"]

    def get_project_cards(self):
        """Return {project name: card HTML} for each card on the page."""
        html = self.client.get("/family").content.decode()
        section = re.search(
            r'<section class="our-team">(.*?)</section>', html, re.DOTALL)
        self.assertIsNotNone(section, "project section missing from /family")
        assert section is not None  # for mypy
        cards = {}
        for chunk in section.group(1).split('<div class="team-item">')[1:]:
            # Match the name in the card's heading, not anywhere in the card:
            # a description can legitimately mention another member by name
            # (Liberated Bread's says it is the same company as Pigs Can Fly
            # Labs), and matching on the whole chunk would misfile the card.
            heading = re.search(r"<h4>(.*?)</h4>", chunk, re.DOTALL)
            if heading is None:
                continue
            for name in self.PROJECT_NAMES:
                if name in heading.group(1):
                    cards[name] = chunk
        self.assertEqual(
            sorted(cards), sorted(self.PROJECT_NAMES),
            "not every project rendered its own card")
        return cards

    def test_family_page_returns_200(self):
        response = self.client.get("/family")
        self.assertEqual(response.status_code, 200)

    def test_family_page_lists_each_project_by_name(self):
        response = self.client.get("/family")
        self.assertContains(response, "Pigs Can Fly Labs")
        self.assertContains(response, "Fight Health Insurance")
        self.assertContains(response, "Liberated Bread")

    def test_family_page_is_framed_around_projects_not_companies(self):
        # The whole point of this change: the family is a family of projects,
        # not a flat "family of companies".
        response = self.client.get("/family")
        self.assertContains(response, "Our Family of Projects")
        self.assertNotContains(response, "Our Family of Companies")

    def test_liberated_bread_is_the_same_company_with_its_own_site(self):
        # Liberated Bread is not a separate company: it is the same company as
        # Pigs Can Fly Labs, just with its own site.
        card = self.get_project_cards()["Liberated Bread"]
        self.assertIn("Same company", card)
        self.assertIn("not a separate company", card)

    def test_fight_health_insurance_is_a_separate_company_and_project(self):
        # Fight Health Insurance is both its own company and a project.
        card = self.get_project_cards()["Fight Health Insurance"]
        self.assertIn("Separate company and project", card)

    def test_family_page_links_to_fight_health_insurance(self):
        response = self.client.get("/family")
        self.assertContains(
            response,
            'href="https://www.fighthealthinsurance.com/"')

    def test_family_page_marks_liberated_bread_as_coming_soon(self):
        self.assertIn(
            self.COMING_SOON_BADGE, self.get_project_cards()["Liberated Bread"])

    def test_family_page_shows_exactly_one_coming_soon_badge(self):
        response = self.client.get("/family")
        self.assertContains(response, self.COMING_SOON_BADGE, count=1,
                            html=False)

    def test_pigs_can_fly_labs_is_not_marked_coming_soon(self):
        # Regression: the parent company links nowhere by design, which the
        # old "no URL means coming soon" rule read as not being live yet.
        self.assertNotIn(
            self.COMING_SOON_BADGE,
            self.get_project_cards()["Pigs Can Fly Labs"])

    def test_liberated_bread_links_to_owner_supplied_url(self):
        # The owner supplied this exact URL; do not invent alternatives.
        card = self.get_project_cards()["Liberated Bread"]
        self.assertEqual(
            re.findall(r'<a href="([^"]+)"', card),
            [self.LIBERATED_BREAD_URL])

    def test_liberated_bread_can_link_while_still_coming_soon(self):
        # URL and coming_soon are independent fields: both can be true at once.
        card = self.get_project_cards()["Liberated Bread"]
        self.assertIn(f'href="{self.LIBERATED_BREAD_URL}"', card)
        self.assertIn(self.COMING_SOON_BADGE, card)

    def test_family_page_renders_the_family_template(self):
        response = self.client.get("/family")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "family.html")
