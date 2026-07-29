"""Tests for the static and mostly-static pages."""

import html as html_module
import re
from datetime import datetime
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from main.models import Product
from main.tests.base import REPO_ROOT

# An edition ordinal as it appears either in a credential label ("1st and 2nd
# editions") or in a product name ("(1st edition)", ", 2nd Edition"). The two
# are worded differently on purpose -- one reads as a sentence, the other is
# the catalogue title -- so the comparison has to be on the numbers, not on
# the strings. Requiring the ordinal suffix keeps plain digits elsewhere in a
# title out of it: "Distributed Computing 4 Kids" names no edition.
_EDITION_ORDINAL = re.compile(r"(\d+)(?:st|nd|rd|th)\b", re.IGNORECASE)


def editions_named(text: str) -> set[int]:
    """The edition numbers *text* claims, as ints. Empty if it claims none."""
    return {int(n) for n in _EDITION_ORDINAL.findall(text)}


class StaticPagesTest(TestCase):
    @staticmethod
    def main_css_declarations_for(selector):
        css = (REPO_ROOT / "main" / "static" / "assets"
               / "css" / "main.css").read_text()
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
        css = re.sub(r"@import[^;]+;", "", css)

        declared = {}
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            names = {" ".join(part.split())
                     for part in selectors.split(",")}
            if selector not in names:
                continue
            for part in body.split(";"):
                if ":" in part:
                    prop, _, value = part.partition(":")
                    declared[prop.strip().lower()] = value.strip().lower()
        return declared

    def test_privacy_page_renders_privacy_template(self):
        response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "privacy.html")

    def test_tos_page_renders_tos_template(self):
        response = self.client.get("/tos")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tos.html")

    def test_queued_messages_clear_the_header_by_token(self):
        root = self.main_css_declarations_for(":root")
        messages = self.main_css_declarations_for(".site-messages")

        self.assertNotEqual(
            messages, {}, "queued messages have no .site-messages CSS rule")
        self.assertEqual(
            messages.get("padding-top"),
            "var(--site-header-clearance, 100px)",
            "queued messages must clear the nav with the shared header token")

        for token in ("--site-header-default-height",
                      "--site-header-fixed-height"):
            self.assertRegex(
                root.get(token, ""), r"^[1-9][0-9]*px$",
                f"{token} must stay a non-zero pixel height")

        clearance = root.get("--site-header-clearance", "")
        self.assertIn("max(", clearance)
        self.assertIn("var(--site-header-default-height)", clearance)
        self.assertIn("var(--site-header-fixed-height)", clearance)


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
            # The 2nd edition's ISBN: the label names both editions, so it
            # links the newer one. See the note on
            # ServicesPageView.HIGH_PERFORMANCE_SPARK.
            "High Performance Spark (1st and 2nd editions)": "9781098145859",
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
                # Compare on the part before the bracket: a credential's
                # edition suffix is worded for the sentence ("1st and 2nd
                # editions") and does not match the product's own suffix
                # ("(1st edition)", ", 2nd Edition") even when both mean the
                # same book. Stripping it is what makes this check general.
                #
                # The cost is that this test cannot see a label and a
                # destination disagreeing about WHICH edition they mean --
                # both reduce to the same stem. That is deliberate here and
                # covered by test_cited_editions_land_on_the_edition_they_name
                # below; do not try to make this one do both jobs.
                stem = title.split(" (")[0]

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, stem)

    def test_cited_editions_land_on_the_edition_they_name(self):
        """A citation naming an edition must reach that edition's page.

        The stem comparison above is blind to this by construction, so
        nothing detected it when pk 101 was renamed to "High Performance
        Spark (1st edition)" and the credential citing "1st and 2nd editions"
        went on pointing at it: the label promised both editions and the page
        it reached said it was only the first. The reader is the one who finds
        out.

        The rule, which matches what the credentials mean: a label naming a
        single edition must reach that edition, a label naming several must
        reach the newest one it names (the one in print), and a label naming
        no edition must not land on an edition-specific page at all.
        """
        section = self.services_section()
        cited = re.findall(r'<a href="/book/(\d+)">([^<]+)</a>', section)
        # Anti-vacuity: the citations are really there to be checked.
        self.assertEqual(len(cited), 5)

        saw_an_edition_claim = False
        for isbn, label in cited:
            with self.subTest(label=label):
                response = self.client.get(f"/book/{isbn}")
                self.assertEqual(response.status_code, 302)
                pk = int(response["Location"].rsplit("/", 1)[-1])
                reached_name = Product.objects.get(pk=pk).name

                claimed = editions_named(html_module.unescape(label))
                reached = editions_named(reached_name)

                if claimed:
                    saw_an_edition_claim = True
                    self.assertEqual(
                        reached, {max(claimed)},
                        f"{label!r} names edition(s) "
                        f"{sorted(claimed)} so it must link the newest of "
                        f"them, edition {max(claimed)}; /book/{isbn} reaches "
                        f"pk {pk}, {reached_name!r}.")
                else:
                    self.assertEqual(
                        reached, set(),
                        f"{label!r} names no edition, but /book/{isbn} "
                        f"reaches pk {pk}, {reached_name!r}, which is "
                        "specific to one.")

        # Anti-vacuity: at least one citation really does make an edition
        # claim, so the branch that matters was actually exercised.
        self.assertTrue(saw_an_edition_claim)


@override_settings(THUMBNAIL_DEBUG=False)
class HomePageCardsTest(TestCase):
    """The #explore card grid, after the FMT2 services were retired.

    The cards are the homepage's only editorial surface, so what they
    advertise is what the company is saying it sells.
    """

    fixtures = ["initial_products"]

    DC4K_TITLE = "Distributed Computing 4 Kids (and Executives)"

    # The featured-book card's copy, exactly as the owner wrote it -- one
    # sentence, one colon, no parenthesis around "and Executives". Spelled
    # out here as an independent statement of the words, the same way
    # test_dc4k.EXECUTIVE_EDITION_COPY is: a reworded template has to fail.
    # It is a single string on purpose. Splitting it across an <h4> and a
    # <span> would put markup in the middle of the sentence, and this
    # assertion -- which is a substring test against rendered HTML -- would
    # stop being able to see it.
    FEATURED_CARD_COPY = (
        "Distributed Computing 4 Kids and Executives: "
        "Executives may require more help")
    LIBERATED_BREAD_TAGLINE = "Liberating your toaster since '26"

    # The rendered form of a Liberated Bread link. One capture group, the
    # destination, so findall returns the URLs themselves.
    LIBERATED_BREAD_HREF = r'<a href="(https://[^"]*liberatedbread[^"]*)"'

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

    def featured_book_card(self):
        """The .second-image block, which is the featured-book card.

        Matched non-greedily to the first </div>, which is correct only
        because the card holds no nested div -- if one is ever added this
        helper has to grow a real parser rather than silently returning a
        fragment.
        """
        card = re.search(r'<div class="second-image">(.*?)</div>',
                         self.explore_section(), re.DOTALL)
        self.assertIsNotNone(card, "featured-book card missing from #explore")
        assert card is not None  # for mypy
        return card.group(1)

    def liberated_bread_cards(self):
        """The two #explore cards that link to Liberated Bread, as
        (logo_card, text_card).

        Each is located with findall and required to appear exactly once,
        because "how many cards" is the thing this scoping exists to check.
        A copy-pasted card is the mistake this grid is most likely to
        acquire, and it does not change the set of destinations on the page
        -- only the number of cards carrying them.

        Matched non-greedily to the first </div>, which is correct only
        because neither card holds a nested div -- if one ever does, this
        helper has to grow a real parser rather than silently returning a
        fragment. Same caveat as featured_book_card.
        """
        section = self.explore_section()
        cards = []
        for name, pattern in (
                ("logo", r'<div class="liberated-bread">(.*?)</div>'),
                ("text", r'<div class="types">(.*?)</div>')):
            found = re.findall(pattern, section, re.DOTALL)
            self.assertEqual(
                len(found), 1,
                f"expected exactly one Liberated Bread {name} card in "
                f"#explore, found {len(found)}")
            cards.append(found[0])
        return cards

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
        family_links = re.findall(
            self.LIBERATED_BREAD_HREF,
            self.client.get("/family").content.decode())
        self.assertEqual(len(family_links), 1)

        # Per card, not across the section. The homepage legitimately carries
        # two of these -- the logo card and the wording beside it -- and a
        # count taken over the whole section cannot tell two cards from one
        # card pasted twice: a duplicate adds a link that is equal to the
        # others, so both a set comparison and a "one or more" count still
        # pass. Requiring exactly one card of each kind, each holding exactly
        # one link, is what makes the duplicate visible; the equality below
        # is what keeps the two copies of the URL from drifting apart.
        logo_card, text_card = self.liberated_bread_cards()
        for name, card in (("logo", logo_card), ("text", text_card)):
            with self.subTest(card=name):
                links = re.findall(self.LIBERATED_BREAD_HREF, card)
                self.assertEqual(
                    len(links), 1,
                    f"expected exactly one Liberated Bread link in the "
                    f"{name} card, found {len(links)}")
                self.assertEqual(links, family_links)

    def test_the_liberated_bread_logo_card_renders_the_512_asset(self):
        """The 512 master, not the 128: it is displayed at 200px, so the
        smaller file would be upscaled and soft on a HiDPI screen.

        The file itself is not in this repo -- main/static/assets/images is
        gitignored and filled by scripts/sync-local-assets.sh out of
        ../pcfweb-assets -- so this pins the reference, which is the part
        this template owns.
        """
        section = self.explore_section()

        self.assertIn(
            "assets/images/liberated-bread-logo-512.png", section)
        self.assertNotIn("liberated-bread-logo.png", section)

    def test_the_liberated_bread_logo_is_served_through_the_static_tag(self):
        """Not a hand-written /static/... path.

        STATIC_URL is what decides that prefix, and a literal that agrees
        with it today goes wrong silently the day it moves.
        """
        with open("main/templates/index.html") as handle:
            template = handle.read()

        self.assertRegex(
            template,
            r"\{% static 'assets/images/liberated-bread-logo-512\.png' %\}")
        self.assertNotIn('"/static/assets/images/liberated-bread', template)

    def test_the_liberated_bread_logo_is_not_stretched(self):
        """The squish bug, guarded at its source.

        Both axes are pinned, and an <img> with a fixed width and height and
        no object-fit uses `fill` -- it distorts the image to the box instead
        of preserving its aspect ratio. That is a real defect this codebase
        has already had to fix once, and it is invisible in the HTML, so the
        assertion has to be against the stylesheet.
        """
        css = (REPO_ROOT / "main" / "static" / "assets"
               / "css" / "main.css").read_text()

        rule = re.search(
            r"#explore \.liberated-bread img \{(.*?)\}", css, re.DOTALL)
        self.assertIsNotNone(
            rule, "no #explore .liberated-bread img rule in main.css")
        assert rule is not None  # for mypy
        body = rule.group(1)

        fit = re.search(r"object-fit:\s*([a-z-]+)", body)
        self.assertIsNotNone(fit, "the logo pins both axes but sets no "
                                  "object-fit, so it renders as `fill`")
        assert fit is not None  # for mypy
        self.assertNotEqual(fit.group(1), "fill")
        self.assertEqual(fit.group(1), "cover")

    def test_the_liberated_bread_logo_card_and_its_wording_are_one_unit(self):
        """Two cards, one voice. If they drift apart in the grid the logo
        reads as an unexplained tile, so pin that they are adjacent."""
        section = self.explore_section()

        logo = section.index("liberated-bread-logo-512.png")
        wording = section.index(
            self.LIBERATED_BREAD_TAGLINE)
        self.assertLess(
            logo, wording,
            "the Liberated Bread logo card should precede its wording")
        between = section[logo:wording]

        # Nothing else's card sits between them.
        self.assertNotIn("second-image", between)
        self.assertNotIn("featured_book", between)

    def test_the_companion_text_card_keeps_its_wording_and_badge(self):
        """The copy is the owner's, verbatim -- not a paraphrase."""
        section = self.explore_section()

        self.assertIn(
            f"<span>{self.LIBERATED_BREAD_TAGLINE}</span>",
            section)
        self.assertIn("Liberated Bread <span", section)
        self.assertIn(">Coming Soon</span>", section)
        # Still the .types card, so it keeps the grid's shared styling.
        self.assertIn('<div class="types">', section)

    def test_the_liberated_bread_tagline_is_owner_copy(self):
        section = self.explore_section()

        self.assertIn(
            "Liberating your toaster since '26",
            section)

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

    def test_the_featured_cover_is_cropped_rather_than_squished(self):
        """A 2:3 book cover in a square box must not be stretched.

        The card pins both axes at 200px. CSS `object-fit` defaults to
        `fill`, which scales width and height by *different* factors to
        make the image meet both -- a 2:3 cover comes out visibly squished.
        `cover` scales uniformly and crops the overflow instead, which is
        the behaviour the owner asked for.

        Asserted as "both axes pinned implies an object-fit that preserves
        the aspect ratio", not as a literal style string, so that unpinning
        an axis is also an acceptable way to pass -- the bug is the
        combination, not the declaration.
        """
        tag = re.search(r'<img\b[^>]*>', self.featured_book_card())
        self.assertIsNotNone(tag, "featured-book card renders no <img>")
        assert tag is not None  # for mypy

        style = re.search(r'style="([^"]*)"', tag.group(0))
        declared = {}
        if style is not None:
            for part in style.group(1).split(";"):
                if ":" in part:
                    prop, _, value = part.partition(":")
                    declared[prop.strip().lower()] = value.strip().lower()

        if "width" in declared and "height" in declared:
            self.assertEqual(
                declared.get("object-fit"), "cover",
                "both axes are pinned, so without object-fit:cover the "
                f"browser falls back to `fill` and squishes the cover: {tag.group(0)}")

    def test_the_featured_card_carries_the_owners_copy(self):
        # Substring of the rendered page, so it fails both on a reworded
        # string and on markup inserted into the middle of the sentence.
        self.assertIn(self.FEATURED_CARD_COPY, self.featured_book_card())

    # -- card chrome and the alternating grid ---------------------------

    @staticmethod
    def declarations_for(selector):
        """Every declaration the stylesheet applies to `selector`.

        Merged across all rules that name it, in source order, because the
        grid's shared chrome is deliberately written as one selector list
        with per-card overrides after it -- a test that read only the first
        matching block would report the pre-override value.

        Selectors are compared after collapsing whitespace, so a rule that
        lists the card on its own line still counts. This is a small reader,
        not a CSS engine: it ignores @media blocks, which is what we want
        here -- these assertions are about the desktop cascade.
        """
        css = (REPO_ROOT / "main" / "static" / "assets"
               / "css" / "main.css").read_text()
        # Drop @media bodies so their overrides don't leak into the result.
        css = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)

        declared = {}
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            names = {" ".join(part.split())
                     for part in selectors.split(",")}
            if selector not in names:
                continue
            for part in body.split(";"):
                if ":" in part:
                    prop, _, value = part.partition(":")
                    declared[prop.strip().lower()] = value.strip().lower()
        return declared

    def test_the_featured_book_card_has_the_same_chrome_as_the_other_cards(
            self):
        """.second-image is a card in the grid, so it has to look like one.

        It used to carry no CSS at all -- no background, no centring, no
        box -- so it rendered as bare text next to three tiles and its <h4>
        fell back to the browser default. Assert it against .types rather
        than against literal values, so the two can only be changed
        together.
        """
        card = self.declarations_for("#explore .second-image")
        self.assertNotEqual(
            card, {},
            "the featured-book card has no styling of its own; it renders "
            "with no chrome next to the other cards")

        reference = self.declarations_for("#explore .types")
        for prop in ("min-height", "background-color", "width"):
            with self.subTest(property=prop):
                self.assertEqual(
                    card.get(prop), reference[prop],
                    f"the featured-book card's {prop} does not match the "
                    "other cards in the grid")

    def test_the_featured_card_does_not_inherit_the_heading_only_padding(
            self):
        """padding-top:90px is tuned for .types, which holds one short line.

        The featured card's copy runs to several lines and sits beside a
        200px cover, so 90px of lead-in pushes it straight out of the 255px
        box. The card centres its contents instead -- so what this guards is
        that it never joins that rule.
        """
        heading = self.declarations_for("#explore .second-image h4")

        # Styled at all, first -- otherwise the assertion below passes on a
        # card whose heading is browser-default, which is the bug this
        # branch set out to fix.
        self.assertIn(
            "font-size", heading,
            "the featured card's heading has no styling, so it falls back "
            "to the browser default next to three styled cards")
        self.assertNotEqual(
            heading.get("padding-top"), "90px",
            "the featured card's long copy cannot survive the padding that "
            "centres a one-line heading")

    def test_the_featured_card_sets_its_cover_beside_the_copy(self):
        """The zig-zag's second row: text on the left, cover on the right.

        The alternation is only half in the markup -- the DOM order puts the
        copy first, and the placement that makes it a row rather than a
        stack lives in the stylesheet, so assert that half here.
        """
        cover = self.declarations_for("#explore .second-image img")

        self.assertEqual(
            cover.get("grid-column"), "2",
            "the cover is not placed in the card's second column, so the "
            "row does not read text-then-image")

    def test_the_explore_cards_alternate_image_and_text(self):
        """Image, text / text, image -- the zig-zag the grid used to have.

        The order is the markup's, exactly as it was when the section last
        had four cards (d7966bd), so it is readable straight off the
        rendered page: the Liberated Bread unit leads with its mark, and the
        featured book leads with its words.
        """
        section = self.explore_section()

        logo = section.index("liberated-bread-logo-512.png")
        bread_copy = section.index(
            self.LIBERATED_BREAD_TAGLINE)
        self.assertLess(
            logo, bread_copy,
            "the first row should read image-then-text")
        first_row = section[logo:bread_copy]
        self.assertNotIn(
            "second-image", first_row,
            "the first row should not have another card between the "
            "Liberated Bread logo and copy")

        card = self.featured_book_card()
        book_copy = card.index(self.FEATURED_CARD_COPY)
        cover = card.index("<img")
        self.assertLess(
            book_copy, cover,
            "the second row should read text-then-image, so that it "
            "alternates against the row above it")


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
        # The same words as the seeded card, so the two branches of the
        # {% if featured_book %} cannot drift apart.
        self.assertIn(HomePageCardsTest.FEATURED_CARD_COPY, html)


@override_settings(THUMBNAIL_DEBUG=False)
class ProductListingTitleLinksTest(TestCase):
    fixtures = ["initial_products"]

    CARD_LINK_PATTERN = re.compile(
        r'<a\b[^>]*href="(?P<image_href>/product/\d+)"[^>]*>\s*'
        r'<img[^>]*>\s*</a>\s*</div>\s*<div class="down-content">\s*<h4>\s*'
        r'<a\b[^>]*href="(?P<title_href>/product/\d+)"[^>]*>'
        r'(?P<title>.*?)</a>\s*</h4>',
        re.DOTALL,
    )

    def assert_title_links_match_cover_links(self, response, expected_products):
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        matches = list(self.CARD_LINK_PATTERN.finditer(html))

        self.assertEqual(
            len(matches), len(expected_products),
            "each rendered product card should have a title link and a "
            "cover link")

        actual = set()
        for match in matches:
            image_href = match.group("image_href")
            title_href = match.group("title_href")
            self.assertEqual(title_href, image_href)
            actual.add((
                title_href,
                html_module.unescape(match.group("title")).strip(),
            ))

        expected = {
            (f"/product/{product.pk}", product.name)
            for product in expected_products
        }
        self.assertEqual(actual, expected)

    def test_all_products_listing_links_each_title_to_its_cover_destination(
            self):
        response = self.client.get("/products")
        products = Product.objects.exclude(noorder=True)

        self.assert_title_links_match_cover_links(response, products)

    def test_books_category_listing_links_each_title_to_its_cover_destination(
            self):
        response = self.client.get("/products/B")
        products = Product.objects.filter(
            cat=Product.Categories.BOOKS).exclude(noorder=True)

        self.assert_title_links_match_cover_links(response, products)

    def test_books_category_query_links_each_title_to_its_cover_destination(
            self):
        response = self.client.get("/products", {"category": "B"})
        products = Product.objects.filter(
            cat=Product.Categories.BOOKS).exclude(noorder=True)

        self.assert_title_links_match_cover_links(response, products)


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
        payments.pwyw_add_to_cart_blocker.return_value = None
        payments.pwyw_checkout_blocker.return_value = None
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
    URL, which badged Pigs Can Fly Labs itself -- it is the company behind
    this site and all of Holden's books, so it deliberately links nowhere.
    Asserting the badge appears *somewhere* on the page could not catch that,
    so these tests pin the badge to a specific card.
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
        # Regression: Pigs Can Fly Labs links nowhere by design, which the old
        # "no URL means coming soon" rule read as not being live yet.
        self.assertNotIn(
            self.COMING_SOON_BADGE,
            self.get_project_cards()["Pigs Can Fly Labs"])

    def test_pigs_can_fly_labs_uses_owner_supplied_kind_copy(self):
        card = self.get_project_cards()["Pigs Can Fly Labs"]
        owner_copy = (
            "The company behind this site and all of Holden's books!")
        self.assertIn(
            html_module.escape(owner_copy), card)

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


class FrozenDatetime(datetime):
    """A datetime whose ``now()`` is pinned to :data:`frozen_year`.

    A subclass rather than a Mock so that every other use of the patched
    symbol keeps behaving like a real datetime; only ``now()`` is stolen.

    ``NowNode`` calls ``datetime.now(tz=tzinfo)``, so the tz it asks for is
    honoured: the instant is built *in* that zone rather than in UTC and
    reinterpreted. Midday on 15 June is chosen so the result is roughly six
    months and twelve hours from the nearest year boundary -- no real zone
    offset (at most +14/-12 hours) can drag the frozen year to an adjacent
    one, whatever TIME_ZONE is in force.
    """

    frozen_year = 2000

    @classmethod
    def now(cls, tz=None):
        return datetime(cls.frozen_year, 6, 15, 12, 0, 0, tzinfo=tz)


def frozen_at(year):
    """A ``FrozenDatetime`` subclass reporting ``year`` from ``now()``."""
    return type("FrozenAt%d" % year, (FrozenDatetime,),
                {"frozen_year": year})


class FooterCopyrightTest(TestCase):
    """The footer copyright range, which every page inherits from base.html.

    The point of these tests is that the end of the range *follows a clock*
    rather than being pinned to a literal. That property is impossible to
    check against the real clock: today the live year and any freshly
    hardcoded year are the same number, so an oracle built on the current
    year passes just as happily against a literal template. Instead the
    clock the tag actually reads is frozen to two years that are not this
    year and cannot both be satisfied by any single literal.

    Nothing here compares against a hardcoded *current* year, so no
    assertion in this class can start failing because a new year began.
    """

    # Deliberately far-future and mutually exclusive: a template frozen at
    # either one would fail the other.
    FROZEN_YEARS = (2031, 2043)

    # Extremes of the real offset range, either side of the project's UTC.
    # The frozen year must survive all of them.
    FROZEN_ZONES = ("UTC", "Pacific/Kiritimati", "Etc/GMT+12",
                    "America/Los_Angeles")

    # `{% now %}` is rendered by django.template.defaulttags.NowNode, which
    # calls `datetime.now(tz=...)` against the `datetime` symbol imported
    # into that module -- NOT django.utils.timezone.now(), which it only
    # consults for the tzinfo argument. Patching timezone.now would leave
    # the tag reading the real clock and quietly prove nothing.
    NOW_SYMBOL = "django.template.defaulttags.datetime"

    def django_year(self):
        """The year *Django's* clock is in, which is what the tag renders.

        Not ``datetime.now().year``: that is the host's local year, and the
        host need not be on TIME_ZONE. Between 00:00 UTC on 1 January and
        midnight locally, a machine behind UTC is still in the old year
        while the footer correctly shows the new one -- a red CI run with
        no product regression, i.e. the exact bomb this class exists to
        design out.

        This mirrors NowNode's own tz choice through
        ``django.utils.timezone``, which is a separate code path from the
        one under test and is untouched by the patch above -- so it still
        reports the real year when the template has stopped reading a clock.
        """
        if not settings.USE_TZ:
            return datetime.now().year
        return timezone.localtime(timezone.now(),
                                  timezone.get_current_timezone()).year

    def footer_years(self):
        response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        match = re.search(
            r"Copyright © (\d{4})-(\d{4}) Pigs Can Fly Labs",
            response.content.decode())
        self.assertIsNotNone(match, "footer copyright line missing")
        assert match is not None  # for mypy
        return int(match.group(1)), int(match.group(2))

    def test_footer_copyright_starts_at_the_founding_year(self):
        start, _ = self.footer_years()
        self.assertEqual(start, 2022)

    def test_footer_copyright_end_year_follows_the_clock(self):
        """A literal end year cannot track two different frozen clocks.

        If the template is ever changed back to a hardcoded year this
        fails, whatever year is hardcoded -- including the current one.
        """
        for year in self.FROZEN_YEARS:
            for zone in self.FROZEN_ZONES:
                with self.subTest(frozen_year=year, time_zone=zone):
                    with override_settings(TIME_ZONE=zone):
                        with mock.patch(self.NOW_SYMBOL, frozen_at(year)):
                            _, end = self.footer_years()
                    self.assertEqual(
                        end, year,
                        "footer end year did not follow a clock frozen at "
                        f"{year} under TIME_ZONE={zone}; it is not reading "
                        "the clock")

    def test_footer_copyright_shows_the_real_year_when_not_frozen(self):
        """The frozen test above would also pass on a template hardcoded to
        2031, so pin the unfrozen render to the real clock as well.

        The expected year comes from Django's clock in Django's timezone --
        the same instant and zone the tag reads -- not from the host's local
        wall clock, so a host behind TIME_ZONE cannot turn New Year's Day
        into a false failure.
        """
        _, end = self.footer_years()
        self.assertEqual(end, self.django_year())
