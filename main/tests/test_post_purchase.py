"""The post-checkout asks: join the list, follow us, say why you bought.

Three things happen on the page Stripe sends a buyer back to, and none of
them may happen *to* the buyer: the mailing list signup is the same double
opt-in form as everywhere else, the social links are the accounts settings
say we have and no others, and the "what made you buy this?" form writes a
row only for somebody holding the Stripe session id for a real order.

Most of what is asserted here is therefore an absence -- nothing subscribed,
no invented link, no note written onto an order the submitter cannot name.
"""

from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import Client, SimpleTestCase, override_settings

from newsletter.models import Newsletter, Subscription

from main import mailing
from main.models import Order, Product, PurchaseFeedback
from main.socials import social_links, usable_url
from main.tests.base import ORDER_TEST_SETTINGS, OrderTestBase, OWNER_EMAIL


FEEDBACK_URL = "/checkout/feedback"

# One configured account and nothing else, so a test asserting which links
# appear is not asserting today's contents of settings.py.
ONE_SOCIAL = dict(
    SOCIAL_YOUTUBE_URL="https://www.youtube.com/user/holdenkarau",
    SOCIAL_MASTODON_URL="",
    SOCIAL_BLUESKY_URL="",
    SOCIAL_TWITCH_URL="",
    SOCIAL_INSTAGRAM_URL="",
    SOCIAL_LINKEDIN_URL="")


class SocialLinkSettingsTest(SimpleTestCase):
    """Only https URLs to a real host become links."""

    @override_settings(**ONE_SOCIAL)
    def test_only_the_configured_networks_render(self):
        links = social_links()

        self.assertEqual([link.label for link in links], ["YouTube"])
        self.assertEqual(
            links[0].url, "https://www.youtube.com/user/holdenkarau")

    @override_settings(**{**ONE_SOCIAL, "SOCIAL_MASTODON_URL": " ",
                          "SOCIAL_BLUESKY_URL": ""})
    def test_a_blank_setting_is_not_a_link(self):
        # The normal state of most of them: an account we do not have must not
        # appear as a link to nowhere.
        self.assertEqual([link.label for link in social_links()], ["YouTube"])

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

    @override_settings(**{**ONE_SOCIAL,
                          "SOCIAL_MASTODON_URL": "javascript:alert(1)"})
    def test_a_bad_url_drops_only_its_own_link(self):
        self.assertEqual([link.label for link in social_links()], ["YouTube"])


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
        self.assertIn("https://www.youtube.com/user/holdenkarau", body)
        self.assertIn("what made you buy this?", body)

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
