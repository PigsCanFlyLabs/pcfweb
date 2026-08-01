"""The post-checkout asks: join the list, follow us, say why you bought.

Three things happen on the page Stripe sends a buyer back to, and none of
them may happen *to* the buyer: the mailing list signup is the same double
opt-in form as everywhere else, the follow block offers the accounts settings
say we have and no others, and the "what made you buy this?" form writes a
row only for somebody holding the Stripe session id for a real order.

Most of what is asserted here is therefore an absence -- nothing subscribed,
no invented link, no note written onto an order the submitter cannot name.

The follow block is offered per account holder, so a second kind of absence
matters as much: a link must be under the name whose account it is and under
no other. That is asserted against a slice of the row rather than against
the whole page, because two of the three names appear elsewhere in the
markup anyway -- in the footer copyright and in the signup dropdown.
"""

import re
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import Client, SimpleTestCase, override_settings

from newsletter.models import Newsletter, Subscription

from main import mailing
from main.models import Order, Product, PurchaseFeedback
from main.socials import (
    LIBERATED_BREAD_URL, NETWORKS, TARGETS, follow_targets, setting_names,
    usable_url)
from main.tests.base import (
    ORDER_TEST_SETTINGS, OrderTestBase, OWNER_EMAIL, REPO_ROOT)


FEEDBACK_URL = "/checkout/feedback"

YOUTUBE = "https://www.youtube.com/user/holdenkarau"
PCFL_MASTODON = "https://example.social/@pigscanfly"

# Every account setting the rows read, all empty. Tests switch on the one or
# two they are about, so what they assert is the code's behaviour and not
# today's contents of settings.py. Derived from the specs rather than typed
# out: a hand-written list stops blanking a network the day one is added to
# NETWORKS, and every test here would quietly start reading the real
# settings.py value for it again -- which is the coupling this exists to cut.
NO_SOCIALS = {name: "" for name in setting_names()}

# The state the site actually ships in: Holden's YouTube channel, the Discord
# under the company, Liberated Bread's site, and nothing else configured.
ONE_SOCIAL = {**NO_SOCIALS, "SOCIAL_HOLDEN_YOUTUBE_URL": YOUTUBE}


def targets_by_key(**overrides):
    """The rows that render under `overrides`, keyed by target key.

    Only the named settings are set; everything else is blanked. A row that
    was dropped is a missing key here, so a test that expected one fails as a
    KeyError naming it.
    """
    with override_settings(**{**NO_SOCIALS, **overrides}):
        return {target.key: target for target in follow_targets()}


class SocialLinkSettingsTest(SimpleTestCase):
    """Only https URLs to a real host become links."""

    def test_only_the_configured_networks_render(self):
        links = targets_by_key(SOCIAL_HOLDEN_YOUTUBE_URL=YOUTUBE)[
            "holden"].links

        self.assertEqual([link.label for link in links], ["YouTube"])
        self.assertEqual(links[0].url, YOUTUBE)

    def test_a_blank_setting_is_not_a_link(self):
        # The normal state of most of them: an account we do not have must not
        # appear as a link to nowhere.
        targets = targets_by_key(SOCIAL_HOLDEN_YOUTUBE_URL=YOUTUBE,
                                 SOCIAL_HOLDEN_MASTODON_URL=" ",
                                 SOCIAL_HOLDEN_BLUESKY_URL="")

        self.assertEqual(
            [link.label for link in targets["holden"].links], ["YouTube"])

    def test_a_url_that_is_not_https_to_a_host_is_refused(self):
        # A half-finished ConfigMap edit costs a link; it does not hand a
        # visitor's browser a scheme we did not intend. Same rule /discord
        # applies to its invite halves.
        for refused in ["javascript:alert(1)",
                        "http://www.youtube.com/user/holdenkarau",
                        "//www.youtube.com/user/holdenkarau",
                        "holdenkarau",
                        "https://",
                        "https://exa mple.com/x",
                        "https://example.com/\nx",
                        ""]:
            with self.subTest(refused=refused):
                self.assertIsNone(usable_url(refused))

    def test_an_https_url_is_kept_exactly_as_configured(self):
        self.assertEqual(
            usable_url("  https://example.social/@pigscanfly  "),
            "https://example.social/@pigscanfly")

    def test_a_bad_url_drops_only_its_own_link(self):
        targets = targets_by_key(
            SOCIAL_HOLDEN_YOUTUBE_URL=YOUTUBE,
            SOCIAL_HOLDEN_MASTODON_URL="javascript:alert(1)")

        self.assertEqual(
            [link.label for link in targets["holden"].links], ["YouTube"])


class FollowTargetsTest(SimpleTestCase):
    """Three of us, kept apart.

    "Follow us" is three different asks -- Holden writes the books, Pigs Can
    Fly Labs publishes them and runs the Discord, Liberated Bread is the same
    company under its own name -- so an account set for one of them must not
    turn up under another, and a name with nothing under it must not render
    as an empty heading.
    """

    def test_each_row_only_carries_its_own_accounts(self):
        targets = targets_by_key(
            SOCIAL_HOLDEN_YOUTUBE_URL=YOUTUBE,
            SOCIAL_PCFL_MASTODON_URL="https://example.social/@pigscanfly",
            SOCIAL_BREAD_INSTAGRAM_URL="https://example.com/liberatedbread")

        self.assertEqual(
            [(link.label, link.url) for link in targets["holden"].links],
            [("YouTube", YOUTUBE)])
        self.assertEqual(
            [(link.label, link.url)
             for link in targets["pigs-can-fly-labs"].links],
            [("Mastodon", "https://example.social/@pigscanfly")])
        self.assertEqual(
            [(link.label, link.url)
             for link in targets["liberated-bread"].links],
            [("Instagram", "https://example.com/liberatedbread")])

    def test_a_name_with_nothing_configured_does_not_render(self):
        # Holden has no accounts set here and nothing else of her own on this
        # page, so there is no row -- a heading over a blank space reads as a
        # broken page.
        self.assertNotIn("holden", targets_by_key())

    def test_only_the_company_row_carries_the_discord(self):
        # The server is the company's, so exactly one row carries the door to
        # it -- and it survives having no socials configured, which is the
        # state the site ships in.
        targets = targets_by_key()

        company = targets["pigs-can-fly-labs"]
        self.assertTrue(company.discord)
        self.assertFalse(
            any(other.discord for other in targets.values()
                if other.key != "pigs-can-fly-labs"))
        # And it is the row that cannot vanish: the company row survives every
        # account being unset, which is the state the site ships in, because
        # the Discord flag alone keeps it from being empty.
        self.assertIsNone(company.site)
        self.assertEqual(company.links, [])

    def test_liberated_bread_points_at_the_same_site_as_everywhere_else(self):
        # The homepage card and the family page link the same constant. Two
        # links to Liberated Bread must not land in two different places.
        self.assertEqual(
            targets_by_key()["liberated-bread"].site, LIBERATED_BREAD_URL)

    def test_a_settings_value_moves_a_site_link(self):
        targets = targets_by_key(
            SOCIAL_BREAD_SITE_URL="https://bread.example.com/",
            SOCIAL_HOLDEN_SITE_URL="https://holden.example.com/")

        self.assertEqual(
            targets["liberated-bread"].site, "https://bread.example.com/")
        self.assertEqual(targets["holden"].site, "https://holden.example.com/")

    def test_a_broken_site_override_falls_back_rather_than_forward(self):
        # Same rule as the accounts: a half-finished ConfigMap edit costs the
        # override, it does not hand the browser a scheme we did not intend.
        targets = targets_by_key(SOCIAL_BREAD_SITE_URL="javascript:alert(1)",
                                 SOCIAL_HOLDEN_SITE_URL="javascript:alert(1)")

        self.assertEqual(
            targets["liberated-bread"].site, LIBERATED_BREAD_URL)
        self.assertNotIn("holden", targets)

    def test_the_rows_are_ordered_author_then_publisher_then_bread(self):
        with override_settings(**ONE_SOCIAL):
            keys = [target.key for target in follow_targets()]

        self.assertEqual(
            keys, ["holden", "pigs-can-fly-labs", "liberated-bread"])

    def test_the_links_in_a_row_follow_the_declared_network_order(self):
        # Not the order settings happen to be read in, and not alphabetical:
        # NETWORKS is the running order, and a row with several accounts is
        # where that stops being invisible.
        targets = targets_by_key(
            SOCIAL_HOLDEN_LINKEDIN_URL="https://example.com/in",
            SOCIAL_HOLDEN_MASTODON_URL="https://example.social/@h",
            SOCIAL_HOLDEN_YOUTUBE_URL=YOUTUBE)

        self.assertEqual([link.label for link in targets["holden"].links],
                         ["Mastodon", "YouTube", "LinkedIn"])

    def test_every_settings_name_the_rows_read_is_defined(self):
        """A name built by concatenation that nothing defines is silent.

        follow_targets() reads settings.SOCIAL_<WHO>_<NETWORK>_URL with a
        getattr default, so a typo in a prefix, a network added to NETWORKS
        without one in settings.Base, or a settings name misspelt on its own
        line costs a link -- or, if it is the only one a target has, the
        whole row -- on the page and in every receipt, with nothing raised
        and nothing logged. This is the failure.
        """
        from pigscanfly.settings import Base

        missing = [name for name in setting_names()
                   if not hasattr(Base, name)]

        self.assertEqual(missing, [])
        # And the count is what the two tables say it should be: a target or
        # a network dropped from the specs would leave settings defining
        # names nothing reads, which is the same drift the other way.
        self.assertEqual(len(setting_names()),
                         len(TARGETS) * (len(NETWORKS) + 1))
        self.assertEqual(
            len([name for name in dir(Base) if name.startswith("SOCIAL_")]),
            len(setting_names()))

    def test_the_manifest_lists_every_name_an_operator_can_set(self):
        """deploy.yaml is where settings.py sends the reader to set one.

        None of these is set, so there is no key in the ConfigMap to find by
        grepping for a value -- the commented block is the only place the
        available names exist for somebody with a Mastodon handle to add. A
        name added to the code and not to that block is a name nobody knows
        to set, which is how a configurable account stays unconfigured.
        """
        manifest = (REPO_ROOT / "deploy.yaml").read_text()

        missing = [name for name in setting_names() if name not in manifest]

        self.assertEqual(missing, [])

    def test_the_shipped_default_is_the_channel_the_about_page_links(self):
        # Every other test here overrides it, so the one value that actually
        # renders in production is otherwise asserted nowhere -- and it is a
        # link on a page about who wrote the book somebody just bought.
        from pigscanfly.settings import Base

        self.assertEqual(Base.SOCIAL_HOLDEN_YOUTUBE_URL, YOUTUBE)
        self.assertIn(
            YOUTUBE, (REPO_ROOT / "main" / "templates" / "about.html")
            .read_text())


class InterestForProductsTest(OrderTestBase):
    """Which list the success page pre-selects.

    Only ever a pre-selection: nothing is subscribed on the strength of it,
    which is why matching a product name against a list title is good enough
    here and would not be anywhere else.
    """

    def products(self, *pks):
        return [Product.objects.get(pk=pk) for pk in pks]

    def test_a_books_own_list_is_picked_for_every_edition_of_it(self):
        # 104 is the print edition, 105 the executive edition, 106 the e-book.
        # One list covers the work, so all three land on it.
        for pk in (104, 105, 106):
            with self.subTest(pk=pk):
                self.assertEqual(
                    mailing.interest_for_products(self.products(pk)), "dc4k")

    def test_an_edition_suffix_does_not_stop_the_match(self):
        for pk in (101, 108):
            with self.subTest(pk=pk):
                self.assertEqual(
                    mailing.interest_for_products(self.products(pk)),
                    "high-performance-spark")

    def test_a_book_with_no_list_of_its_own_gets_the_books_list(self):
        self.assertEqual(
            mailing.interest_for_products(self.products(100)), "books")

    def test_an_order_spanning_two_topics_gets_the_general_list(self):
        # Two answers is no answer: pre-selecting either would be a guess
        # about which half of the order they care about.
        self.assertEqual(
            mailing.interest_for_products(self.products(104, 101)), "general")

    def test_two_editions_of_the_same_book_still_get_its_list(self):
        self.assertEqual(
            mailing.interest_for_products(self.products(104, 106)), "dc4k")

    def test_nothing_at_all_gets_the_general_list(self):
        self.assertEqual(mailing.interest_for_products([]), "general")

    def test_a_deleted_product_row_cannot_vote(self):
        # An OrderItem keeps the name of what was sold, but the Product may be
        # gone -- that is a None here, not a crash.
        self.assertEqual(
            mailing.interest_for_products([None] + self.products(104)), "dc4k")

    def test_a_hidden_list_is_not_pre_selected(self):
        # Consistent with the signup form, which does not offer one either.
        Newsletter.objects.filter(slug="dc4k").update(visible=False)

        self.assertEqual(
            mailing.interest_for_products(self.products(104)), "books")

    def test_a_two_letter_list_title_cannot_claim_the_catalogue(self):
        # "Le" is a prefix of "Learning Spark", and of plenty else. A title
        # that short is not a topic, so it does not get to pre-select.
        Newsletter.objects.create(
            slug="le", title="Le", email="list@example.com", sender="Us")

        self.assertEqual(
            mailing.interest_for_products(self.products(100)), "books")


@override_settings(**ORDER_TEST_SETTINGS)
class PostPurchaseBlockTest(OrderTestBase):
    """What the success page offers once the order is on it."""

    def paid_order(self, product_pk=104):
        order = self.place_order(product_pk=product_pk, quantity=1)
        self.deliver(self.event_body(order))
        order.refresh_from_db()
        return order

    def success_page(self, order=None, session_id=None):
        if session_id is None:
            session_id = order.stripe_session_id if order else None
        url = "/checkout/success"
        if session_id:
            url += f"?session_id={session_id}"
        return self.client.get(url)

    @override_settings(**ONE_SOCIAL)
    def test_the_page_offers_the_list_the_socials_and_the_question(self):
        order = self.paid_order()

        response = self.success_page(order)
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/mailing-list/subscribe"', html)
        self.assertIn("https://www.youtube.com/user/holdenkarau", html)
        self.assertIn('href="/discord"', html)
        self.assertIn(f'action="{FEEDBACK_URL}"', html)
        self.assertIn(
            f'name="session_id" value="{order.stripe_session_id}"', html)

    def follow_rows(self, html):
        """{name: row HTML} for each row of the follow block.

        The rows have to be sliced apart before anything is asserted about
        them. "Pigs Can Fly Labs" appears in the footer copyright and
        "Liberated Bread" in the signup dropdown, so asserting either string
        against the whole page passes with the entire block deleted.
        """
        return {
            match.group(2): match.group(0)
            for match in re.finditer(
                r'<div class="follow-target" id="follow-([^"]+)">'
                r'.*?<h5>(.*?)</h5>.*?</div>', html, re.DOTALL)}

    @override_settings(**ONE_SOCIAL)
    def test_the_page_names_who_it_is_offering_to_follow(self):
        # Not one strip of icons: whose account each link is is the whole
        # point of the block, since a book buyer may want the author and not
        # the bakery, or the other way round.
        html = self.success_page(self.paid_order()).content.decode()

        self.assertEqual(list(self.follow_rows(html)),
                         ["Holden", "Pigs Can Fly Labs", "Liberated Bread"])
        # And the list is still the ask underneath all three.
        self.assertIn('action="/mailing-list/subscribe"', html)

    @override_settings(**{**ONE_SOCIAL,
                          "SOCIAL_PCFL_MASTODON_URL": PCFL_MASTODON})
    def test_a_link_renders_inside_the_row_whose_account_it_is(self):
        # The grouping happens in the template, so asserting a URL is
        # somewhere in the document asserts nothing about it: every link
        # hoisted into one list would pass that. Each one has to be inside
        # its own row.
        rows = self.follow_rows(
            self.success_page(self.paid_order()).content.decode())

        self.assertIn(YOUTUBE, rows["Holden"])
        self.assertNotIn(YOUTUBE, rows["Pigs Can Fly Labs"])
        self.assertIn(PCFL_MASTODON, rows["Pigs Can Fly Labs"])
        self.assertIn('href="/discord"', rows["Pigs Can Fly Labs"])
        self.assertNotIn('href="/discord"', rows["Holden"])
        self.assertIn(LIBERATED_BREAD_URL, rows["Liberated Bread"])
        self.assertNotIn(LIBERATED_BREAD_URL, rows["Holden"])

    @override_settings(**ONE_SOCIAL)
    def test_an_outbound_follow_link_carries_the_usual_rel(self):
        # rel="me" is what Mastodon reads to verify a link back; the other two
        # are the precautions every target="_blank" link needs.
        rows = self.follow_rows(
            self.success_page(self.paid_order()).content.decode())

        self.assertIn(
            f'<a href="{YOUTUBE}" target="_blank" '
            f'rel="me noopener noreferrer">',
            rows["Holden"])
        # The Discord link is ours and same-origin: no target, no rel.
        self.assertIn('<a href="/discord">', rows["Pigs Can Fly Labs"])

    @override_settings(**{**ONE_SOCIAL,
                          "SOCIAL_BREAD_BLUESKY_URL": "https://example.com/b"})
    def test_a_row_lists_its_site_before_its_accounts(self):
        # Home first, then everywhere else -- the order the template and the
        # receipt both use, worth pinning in one of them.
        rows = self.follow_rows(
            self.success_page(self.paid_order()).content.decode())

        row = rows["Liberated Bread"]
        self.assertLess(row.index(LIBERATED_BREAD_URL),
                        row.index("https://example.com/b"))

    @override_settings(**NO_SOCIALS)
    def test_a_name_with_no_accounts_is_absent_rather_than_empty(self):
        # Nothing configured for Holden, so no Holden row -- while the two
        # rows that have somewhere to go stay.
        html = self.success_page(self.paid_order()).content.decode()

        self.assertEqual(list(self.follow_rows(html)),
                         ["Pigs Can Fly Labs", "Liberated Bread"])
        self.assertIn('href="/discord"', html)

    def test_the_signup_box_starts_on_the_address_the_receipt_went_to(self):
        order = self.paid_order()
        self.assertEqual(order.customer_email, "buyer@example.com")

        html = self.success_page(order).content.decode()

        self.assertIn('value="buyer@example.com"', html)
        # Pre-filling a form field is not a signup. Nothing may be on a list
        # until they submit it and confirm.
        self.assertFalse(Subscription.objects.exists())

    def test_the_list_for_the_book_they_bought_starts_selected(self):
        order = self.paid_order(product_pk=104)

        html = self.success_page(order).content.decode()

        self.assertIn('<option value="dc4k" selected>', html)
        self.assertNotIn('<option value="general" selected>', html)

    def test_a_page_without_an_order_still_offers_the_list_and_socials(self):
        # A bare GET of the success URL: nothing to attach an answer to, so
        # there is no question -- but the other two asks cost nothing.
        with override_settings(**ONE_SOCIAL):
            html = self.success_page(session_id=None).content.decode()

        self.assertIn('action="/mailing-list/subscribe"', html)
        self.assertIn("https://www.youtube.com/user/holdenkarau", html)
        self.assertNotIn(f'action="{FEEDBACK_URL}"', html)

    def test_an_unknown_session_id_is_not_offered_the_question_either(self):
        self.paid_order()

        html = self.success_page(session_id="cs_not_ours").content.decode()

        self.assertNotIn(f'action="{FEEDBACK_URL}"', html)

    def test_the_question_stops_being_asked_once_the_order_is_full(self):
        order = self.paid_order()
        for index in range(PurchaseFeedback.MAX_PER_ORDER):
            PurchaseFeedback.objects.create(order=order, reason=f"{index}")

        html = self.success_page(order).content.decode()

        self.assertNotIn(f'action="{FEEDBACK_URL}"', html)
        # The other two asks are unaffected by having answered the third.
        self.assertIn('action="/mailing-list/subscribe"', html)


@override_settings(**ORDER_TEST_SETTINGS)
class PurchaseFeedbackSubmissionTest(OrderTestBase):
    """The feedback endpoint. The Stripe session id is the only authority."""

    def setUp(self):
        super().setUp()
        # The rate limiter counts in the process-local cache, which outlives a
        # test case; without this these poison each other by running order.
        cache.clear()

    def paid_order(self, session_id="cs_test_session"):
        order = self.place_order(quantity=1, session_id=session_id)
        self.deliver(self.event_body(order))
        mail.outbox.clear()
        order.refresh_from_db()
        return order

    def submit(self, order=None, session_id=None, **fields):
        data = {"reason": "My kid asked for it."}
        if session_id is None and order is not None:
            session_id = order.stripe_session_id
        data["session_id"] = session_id or ""
        data.update(fields)
        return self.client.post(FEEDBACK_URL, data)

    def feedback_emails(self):
        return [m for m in mail.outbox if "Why they bought" in m.subject]

    def test_an_answer_is_stored_against_the_order(self):
        order = self.paid_order()

        response = self.submit(order, reason="A friend recommended it.",
                               may_quote="1", quote_name="  Sam   T  ")

        self.assertEqual(response.status_code, 200)
        feedback = PurchaseFeedback.objects.get()
        self.assertEqual(feedback.order, order)
        self.assertEqual(feedback.reason, "A friend recommended it.")
        self.assertTrue(feedback.may_quote)
        # Collapsed: this is the string that would sit beside a quote.
        self.assertEqual(feedback.quote_name, "Sam T")

    def test_the_answer_is_mailed_to_whoever_gets_order_notifications(self):
        order = self.paid_order()

        self.submit(order, reason="The cover.", may_quote="1",
                    quote_name="Sam")

        message = self.feedback_emails()[0]
        self.assertEqual(message.to, [OWNER_EMAIL])
        self.assertIn(f"#{order.pk}", message.subject)
        self.assertIn("The cover.", message.body)
        self.assertIn("yes, as Sam", message.body)

    def test_quotable_without_a_name_is_its_own_answer(self):
        # Permission to be quoted anonymously is not permission to be named,
        # and the owner reads this line to decide what may go on the site.
        order = self.paid_order()

        self.submit(order, reason="Quote away.", may_quote="1", quote_name="")

        self.assertIn("yes, but they did not say what to call them",
                      self.feedback_emails()[0].body)

    def test_not_being_quotable_is_what_the_email_says_by_default(self):
        order = self.paid_order()

        self.submit(order, reason="Because.")

        self.assertIn("May we quote it: no", self.feedback_emails()[0].body)
        self.assertFalse(PurchaseFeedback.objects.get().may_quote)

    def test_a_mail_failure_does_not_lose_the_answer(self):
        # The row is the record; the email is a convenience on top of it.
        order = self.paid_order()

        with mock.patch("main.models.send_mail",
                        side_effect=RuntimeError("no smtp")):
            response = self.submit(order, reason="Still worth keeping.")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PurchaseFeedback.objects.get().reason,
                         "Still worth keeping.")

    def test_a_session_id_that_matches_no_order_writes_nothing(self):
        self.paid_order()

        response = self.submit(session_id="cs_not_ours")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PurchaseFeedback.objects.exists())
        self.assertEqual(self.feedback_emails(), [])

    def test_an_unplaceable_submission_is_thanked_in_the_same_words(self):
        # A submission that could not be placed is not told so: "no such
        # order" is a sentence written for whoever is trying session ids.
        #
        # Not byte-for-byte identical, and deliberately not asserted to be:
        # a page that resolved an order pre-fills the signup box with the
        # address the receipt went to. That tells the holder of a session id
        # something they already have -- the success page renders their whole
        # order off the same id -- so what matters is that the *answer* to the
        # submission does not vary.
        order = self.paid_order()

        real = self.submit(order, reason="Same words either way.")
        unknown = self.submit(session_id="cs_not_ours",
                              reason="Same words either way.")

        self.assertEqual(real.status_code, unknown.status_code)
        for response in (real, unknown):
            html = response.content.decode()
            self.assertIn("Thank you", html)
            self.assertNotIn("could not", html)
            self.assertNotIn("no order", html)

    def test_no_session_id_at_all_writes_nothing(self):
        self.paid_order()

        response = self.submit(session_id="")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PurchaseFeedback.objects.exists())

    def test_an_empty_answer_is_refused_rather_than_stored(self):
        order = self.paid_order()

        response = self.submit(order, reason="   ")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PurchaseFeedback.objects.exists())
        self.assertIn("nothing in the box", response.content.decode())

    def test_a_refusal_still_offers_the_form_again(self):
        # They typed nothing by accident; the page they were on is gone, so
        # the retry has to be here or it is not a retry.
        order = self.paid_order()

        html = self.submit(order, reason="").content.decode()

        self.assertIn(f'action="{FEEDBACK_URL}"', html)
        self.assertIn(
            f'name="session_id" value="{order.stripe_session_id}"', html)

    def test_a_stored_answer_is_not_asked_for_twice_on_the_same_page(self):
        order = self.paid_order()

        html = self.submit(order).content.decode()

        self.assertNotIn(f'action="{FEEDBACK_URL}"', html)
        # The list and the socials come along, though: this is where somebody
        # who answered the question ends up.
        self.assertIn('action="/mailing-list/subscribe"', html)

    def test_a_honeypotted_submission_is_answered_like_a_real_one(self):
        order = self.paid_order()

        response = self.submit(order, website="http://spam.example")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PurchaseFeedback.objects.exists())
        self.assertEqual(self.feedback_emails(), [])

    @override_settings(PURCHASE_FEEDBACK_RATE_LIMIT=2)
    def test_a_flood_from_one_source_stops_being_stored(self):
        order = self.paid_order()

        for index in range(4):
            self.submit(order, reason=f"Note {index}")

        self.assertEqual(PurchaseFeedback.objects.count(), 2)

    @override_settings(PURCHASE_FEEDBACK_RATE_LIMIT=0)
    def test_the_limit_can_be_turned_off(self):
        order = self.paid_order()

        for index in range(3):
            self.submit(order, reason=f"Note {index}")

        self.assertEqual(PurchaseFeedback.objects.count(), 3)

    def test_one_order_cannot_be_used_to_fill_the_table(self):
        order = self.paid_order()
        for index in range(PurchaseFeedback.MAX_PER_ORDER):
            PurchaseFeedback.objects.create(order=order, reason=f"{index}")

        response = self.submit(order, reason="One too many.")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PurchaseFeedback.objects.count(),
                         PurchaseFeedback.MAX_PER_ORDER)

    def test_a_second_thought_is_welcome(self):
        order = self.paid_order()

        self.submit(order, reason="First thought.")
        self.submit(order, reason="Second thought.")

        self.assertEqual(
            list(PurchaseFeedback.objects.values_list("reason", flat=True)),
            ["Second thought.", "First thought."])

    def test_a_very_long_answer_is_trimmed_rather_than_rejected(self):
        # Somebody who wrote two pages is telling us something at length.
        # Losing all of it to a validation error is the worse outcome.
        order = self.paid_order()

        response = self.submit(
            order, reason="x" * (PurchaseFeedback.MAX_REASON_LENGTH + 500))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(PurchaseFeedback.objects.get().reason),
                         PurchaseFeedback.MAX_REASON_LENGTH)

    def test_the_endpoint_needs_a_csrf_token(self):
        # Unlike the mailing list signup next door, this form is only ever
        # rendered by us on a page of ours.
        order = self.paid_order()
        strict = Client(enforce_csrf_checks=True)

        response = strict.post(
            FEEDBACK_URL,
            {"session_id": order.stripe_session_id, "reason": "No token."})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PurchaseFeedback.objects.exists())

    def test_following_the_action_url_by_hand_lands_somewhere_useful(self):
        response = self.client.get(FEEDBACK_URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")

    def test_an_answer_cannot_be_written_onto_an_order_by_number(self):
        # The form names an order by Stripe session id precisely so that an
        # order primary key in the markup is not a number anybody can change.
        order = self.paid_order()

        response = self.client.post(
            FEEDBACK_URL, {"order": order.pk, "reason": "Not mine."})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PurchaseFeedback.objects.exists())


@override_settings(**ORDER_TEST_SETTINGS)
class ReceiptStayInTouchTest(OrderTestBase):
    """The receipt carries the same asks, for the tab that got closed."""

    def paid_order(self):
        order = self.place_order(quantity=1)
        self.deliver(self.event_body(order))
        order.refresh_from_db()
        return order

    @override_settings(**ONE_SOCIAL)
    def test_the_receipt_names_the_list_the_discord_and_the_socials(self):
        body = self.paid_order().receipt_body()

        # Absolute URLs: a receipt is built inside a Stripe webhook, where
        # there is no request whose host could be borrowed.
        self.assertIn("https://www.pigscanfly.ca/subscribe", body)
        self.assertIn("https://www.pigscanfly.ca/discord", body)
        self.assertIn(YOUTUBE, body)
        self.assertIn("what made you buy this?", body)

    def receipt_rows(self, body):
        """{name: [lines under it]} for the follow block of a receipt.

        Sliced apart for the same reason the page's rows are: "heading X
        appears before link Y" is satisfied by every link in the mail so long
        as the first heading is first, so it cannot tell a correctly grouped
        receipt from one that filed Holden's channel under the bakery. The
        indent is the structure -- a heading is flush left, its links are
        indented under it.
        """
        rows, current = {}, None
        for line in body.splitlines():
            if line.startswith("  ") and current is not None:
                rows[current].append(line.strip())
            elif line.strip() in [target.name for target in TARGETS]:
                current = line.strip()
                rows[current] = []
        return rows

    @override_settings(**{**ONE_SOCIAL,
                          "SOCIAL_BREAD_BLUESKY_URL": "https://example.com/b"})
    def test_the_receipt_says_whose_account_each_link_is(self):
        # The same grouping as the page, for the buyer who closed the tab:
        # a bare list of URLs in a receipt does not say who is at the end of
        # each one.
        rows = self.receipt_rows(self.paid_order().receipt_body())

        self.assertEqual(
            rows,
            {"Holden": [f"YouTube: {YOUTUBE}"],
             "Pigs Can Fly Labs": [
                 "Discord: https://www.pigscanfly.ca/discord"],
             "Liberated Bread": [f"Website: {LIBERATED_BREAD_URL}",
                                 "Bluesky: https://example.com/b"]})

    @override_settings(**ONE_SOCIAL)
    def test_the_receipt_carries_the_names_without_the_sales_copy(self):
        # The blurbs are copy for a page somebody chose to be on. A receipt
        # earns its place in an inbox by being a receipt.
        body = self.paid_order().receipt_body()

        self.assertIn("\nHolden\n", body)
        for target in TARGETS:
            self.assertNotIn(target.blurb, body)

    @override_settings(**NO_SOCIALS)
    def test_a_receipt_carries_no_heading_for_a_name_with_no_links(self):
        rows = self.receipt_rows(self.paid_order().receipt_body())

        self.assertEqual(list(rows), ["Pigs Can Fly Labs", "Liberated Bread"])

    def test_the_receipt_still_says_what_went_wrong_first(self):
        # The asks go last. A receipt is a receipt.
        body = self.paid_order().receipt_body()

        self.assertLess(body.index("If something is wrong with your order"),
                        body.index("Stay in touch"))

    def test_a_receipt_pre_subscribes_nobody(self):
        order = self.place_order(quantity=1)
        self.deliver(self.event_body(order))

        self.assertFalse(Subscription.objects.exists())
        self.assertEqual(
            Order.objects.get(pk=order.pk).customer_email,
            "buyer@example.com")
