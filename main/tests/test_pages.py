"""Tests for the static and mostly-static pages."""

import html as html_module
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


class ServicesPageMixin:
    """Shared readers for the /services page.

    A mixin rather than a base test case: one of the classes below runs with
    no fixtures on purpose and the other needs them, so inheriting tests
    between them would run each class's assertions under the wrong database.
    """

    def page(self):
        response = self.client.get("/services")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def services_section(self):
        """Just the cards, so footer/nav links cannot satisfy an assertion."""
        section = re.search(
            r'<section class="our-team">(.*?)</section>', self.page(),
            re.DOTALL)
        self.assertIsNotNone(section, "services section missing")
        assert section is not None  # for mypy
        return section.group(1)

    def credential_text(self, service_name):
        """The credential line for one card, as plain text.

        The line is rendered as a mix of links and literal copy so book
        titles can point at /book/<isbn>. Reassembling it lets these tests
        assert the owner's exact approved wording rather than fragments of
        it -- these are publishing claims, so the whole sentence matters.
        """
        section = self.services_section()
        chunks = section.split('<div class="team-item">')[1:]
        for chunk in chunks:
            heading = re.search(r"<h4>(.*?)</h4>", chunk, re.DOTALL)
            if heading is None or service_name not in heading.group(1):
                continue
            spans = re.findall(r"<span>(.*?)</span>", chunk, re.DOTALL)
            for span in spans:
                if "co-author" not in span:
                    continue
                text = re.sub(r"<[^>]+>", "", span)
                return html_module.unescape(text).strip()
        self.fail(f"no credential line found for {service_name}")


@override_settings(THUMBNAIL_DEBUG=False)
class ServicesPageTest(ServicesPageMixin, TestCase):
    """The curated /services page that replaced the Product-backed listing.

    The old page listed Product rows with cat=SERVICES. Nothing here is a
    Product, so the point of these tests is that the page says what it offers
    without ever offering to sell it through the cart. Deliberately no
    fixtures: if the page still read Product rows, these would render empty.
    """

    EXPECTED = [
        "Liberated Bread",
        "Apache Spark Consulting",
        "AI Consulting",
        "Fight Health Insurance",
    ]

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

    def test_both_consulting_entries_ask_for_email_with_the_project(self):
        """The owner wants the project described in an email, not a form."""
        html = self.page()

        self.assertEqual(
            html.count(
                "Email holden@pigscanfly.ca with the project you&#x27;d "
                "like help on."),
            2)
        self.assertEqual(html.count("mailto:holden@pigscanfly.ca"), 2)
        # The generic contact form is no longer the consulting CTA.
        self.assertNotIn('href="/contact"', self.services_section())

    def test_the_mailto_subjects_distinguish_the_two_specialisms(self):
        section = self.services_section()

        self.assertIn("mailto:holden@pigscanfly.ca?subject=Apache%20Spark%20"
                      "consulting", section)
        self.assertIn("mailto:holden@pigscanfly.ca?subject=AI%20consulting",
                      section)

    def test_the_consulting_scope_is_stated_once_not_per_card(self):
        """Same engagement for both, so the same sentence twice is filler."""
        html = self.page()

        self.assertEqual(
            html.count(
                "Both consulting engagements cover architecture review, "
                "performance tuning, training, and a retainer for periodic "
                "consulting."),
            1)

    def test_the_spark_credentials_claim_the_first_spark_book(self):
        """The owner has confirmed he wrote the first book about Spark.

        This test is the inverse of the one it replaces. While the question
        was open the page hedged to "one of the first books", and that test
        failed if the stronger claim appeared. The owner has since confirmed
        he wrote both Fast Data Processing with Spark (Packt, 2013) and
        Learning Spark 1e (O'Reilly, 2015), and that the 2013 book is the
        first. So the claim is now asserted, and this fails if it is weakened
        back -- the risk has flipped from overclaiming to underclaiming.
        """
        text = self.credential_text("Apache Spark Consulting")

        self.assertEqual(
            text,
            "From the co-author of Learning Spark (1st edition) and High "
            "Performance Spark (1st and 2nd editions), and author of Fast "
            "Data Processing with Spark — the first book written about "
            "Apache Spark.")
        # The hedge must not creep back in.
        self.assertNotIn("one of the first", text)

    def test_the_ai_credentials_cite_both_ml_books_in_two_sentences(self):
        """Eliding "co-author of" across the clause is ungrammatical.

        The owner rejected "and of the Spark books..." for exactly that. Two
        sentences is the fix, so this pins the sentence boundary.
        """
        text = self.credential_text("AI Consulting")

        self.assertEqual(
            text,
            "From the co-author of Kubeflow for Machine Learning and Scaling "
            "Python with Ray. Much of today's ML tooling still runs on Spark, "
            "and those books are ours too.")
        self.assertNotIn("and of the Spark books", text)

    def test_every_cited_book_links_by_isbn_not_by_primary_key(self):
        """A pk in markup keeps resolving after the row moves; an ISBN does not.

        All five cited titles must be links, and every one must go through
        /book/<isbn>.
        """
        section = self.services_section()
        expected = {
            "Learning Spark (1st edition)": "9781449358624",
            "High Performance Spark (1st and 2nd editions)": "9781491943205",
            "Fast Data Processing with Spark": "9781782167068",
            "Kubeflow for Machine Learning": "9781492050124",
            "Scaling Python with Ray": "9781098118808",
        }

        for title, isbn in expected.items():
            with self.subTest(title=title):
                self.assertIn(f'<a href="/book/{isbn}">{title}</a>', section)

        self.assertNotIn("/product/", section)

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
class ServicesPageBookLinksTest(ServicesPageMixin, TestCase):
    """The cited book links, against a seeded catalogue.

    Separate from ServicesPageTest, which deliberately runs with no fixtures
    to prove the page does not depend on Product rows. That property holds for
    *rendering* the page; resolving its outbound book links is a different
    claim and genuinely does need the catalogue, which production seeds via
    scripts/start-server.sh. Splitting them keeps both honest instead of
    weakening the independence test to accommodate this one.
    """

    fixtures = ["initial_products"]

    def test_every_cited_book_link_actually_resolves(self):
        """A link to a 404 is worse than plain text."""
        isbns = re.findall(r'href="/book/(\d+)"', self.services_section())
        self.assertEqual(len(isbns), 5)

        for isbn in isbns:
            with self.subTest(isbn=isbn):
                response = self.client.get(f"/book/{isbn}")

                self.assertEqual(response.status_code, 302)
                self.assertRegex(response["Location"], r"^/product/\d+$")

    def test_each_cited_book_link_reaches_a_page_naming_that_book(self):
        """Guards a right-shaped link pointing at the wrong book."""
        section = self.services_section()
        for isbn, title in re.findall(
                r'<a href="/book/(\d+)">([^<]+)</a>', section):
            with self.subTest(isbn=isbn):
                response = self.client.get(f"/book/{isbn}", follow=True)
                # The credential titles carry an edition suffix the product
                # name does not, so compare on the part before the bracket.
                stem = title.split(" (")[0]

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, stem)


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
