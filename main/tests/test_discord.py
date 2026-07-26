"""Tests for /discord, the captcha-gated Discord invite page.

The invariant worth defending here is narrow and easy to break by accident:
the invite URL must never appear in a response as a URL. It goes out as two
halves, only after a captcha, and only the browser joins them. Every "assert
the joined link is not in the body" below is guarding that.
"""

import re
import time
from pathlib import Path

import yaml
from django.test import TestCase, override_settings

from main import captcha
from main.views import DiscordJoinView


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

PART_ONE = "https://discord.gg/testInv"
PART_TWO = "iteCode"
JOINED = PART_ONE + PART_TWO


@override_settings(THUMBNAIL_DEBUG=False,
                   DISCORD_INVITE_PART_ONE=PART_ONE,
                   DISCORD_INVITE_PART_TWO=PART_TWO)
class DiscordJoinPageTest(TestCase):

    def answer_for_the_pending_challenge(self):
        """The answer the server is holding for this client's session."""
        challenge = self.client.session[captcha.SESSION_KEY]
        return str(challenge["answer"])

    def solve(self, **extra):
        """Ask for a question and answer it correctly."""
        self.client.get("/discord")
        data = {"captcha_answer": self.answer_for_the_pending_challenge()}
        data.update(extra)
        return self.client.post("/discord", data)

    def test_the_page_renders_with_a_challenge(self):
        response = self.client.get("/discord")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "discord.html")
        self.assertContains(response, "plus")
        self.assertContains(response, "captcha_answer")

    def test_the_unsolved_page_hands_out_no_part_of_the_invite(self):
        # The gate is worth nothing if the halves ship with the question.
        response = self.client.get("/discord")
        self.assertNotContains(response, JOINED)
        self.assertNotContains(response, PART_ONE)
        self.assertNotContains(response, PART_TWO)

    def test_the_answer_is_never_sent_to_the_browser(self):
        # If it were, the captcha would be a formality a bot reads off the
        # page. The sum is 2..10 and those digits appear in unrelated markup
        # (the copyright years, image widths), so look only at the form.
        response = self.client.get("/discord")
        answer = self.answer_for_the_pending_challenge()
        form = re.search(r"<form.*?</form>", response.content.decode(),
                         re.DOTALL)
        self.assertIsNotNone(form, "the captcha form is missing")
        assert form is not None  # for mypy
        self.assertIn("captcha_answer", form.group(0))
        self.assertNotIn(f">{answer}<", form.group(0))
        self.assertNotIn(f'value="{answer}"', form.group(0))

    def test_a_correct_answer_hands_over_both_halves(self):
        response = self.solve()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, PART_ONE)
        self.assertContains(response, PART_TWO)

    def test_a_solved_page_still_never_contains_the_joined_link(self):
        # The whole point: a crawler that solved nothing, and even one that
        # did, cannot lift a ready-made invite out of the HTML.
        self.assertNotContains(self.solve(), JOINED)

    def test_a_solved_page_ships_the_script_that_joins_the_halves(self):
        response = self.solve()
        self.assertContains(response, "discord-invite-part-one")
        self.assertContains(response, "discord-invite-part-two")
        self.assertContains(response, "discord-join-link")

    def test_the_join_button_starts_hidden_and_needs_the_script(self):
        # Only the script knows the URL, so a button that shipped visible
        # would be a link to "#" for anyone whose JavaScript never ran.
        response = self.solve()
        self.assertContains(response, 'id="discord-join-button" hidden')

    def test_a_solved_page_tells_a_visitor_with_no_javascript_what_to_do(self):
        self.assertContains(self.solve(), "<noscript>")

    def test_a_wrong_answer_does_not_hand_over_the_halves(self):
        self.client.get("/discord")
        response = self.client.post("/discord", {"captcha_answer": "1000"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, PART_TWO)
        self.assertContains(response, "captcha_answer")

    def test_a_wrong_answer_gets_a_fresh_question(self):
        # The old one is spent; without a replacement the page would be a
        # dead end for anyone who mistyped.
        self.client.get("/discord")
        self.client.post("/discord", {"captcha_answer": "1000"})
        self.assertIn(captcha.SESSION_KEY, self.client.session)

    def test_an_answer_cannot_be_replayed(self):
        self.client.get("/discord")
        answer = self.answer_for_the_pending_challenge()
        self.assertContains(
            self.client.post("/discord", {"captcha_answer": answer}), PART_TWO)
        # A bot that watched a successful POST cannot repeat it: the solved
        # page issues no new challenge, so the replay is answering nothing.
        self.assertNotIn(captcha.SESSION_KEY, self.client.session)
        self.assertNotContains(
            self.client.post("/discord", {"captcha_answer": answer}), PART_TWO)

    def test_a_post_with_no_challenge_at_all_is_refused(self):
        # Straight to the POST, no page load, no session -- every possible
        # sum. A fresh client each time because a refused POST answers with a
        # new question, which the next guess would be answering.
        for guess in range(2, 11):
            with self.subTest(guess=guess):
                response = self.client_class().post(
                    "/discord", {"captcha_answer": str(guess)})
                self.assertNotContains(response, PART_TWO)

    def test_an_expired_challenge_is_refused(self):
        self.client.get("/discord")
        answer = self.answer_for_the_pending_challenge()
        session = self.client.session
        session[captcha.SESSION_KEY]["issued_at"] = (
            time.time() - captcha.TTL_SECONDS - 1)
        session.save()
        response = self.client.post("/discord", {"captcha_answer": answer})
        self.assertNotContains(response, PART_TWO)

    def test_a_filled_honeypot_fails_even_with_the_right_answer(self):
        response = self.solve(email_confirm="bot@example.com")
        self.assertNotContains(response, PART_TWO)

    def test_the_honeypot_field_is_the_one_the_page_renders(self):
        # The bait and the trap have to have the same name.
        self.assertContains(
            self.client.get("/discord"), f'name="{DiscordJoinView.HONEYPOT_FIELD}"')

    def test_the_page_asks_not_to_be_indexed(self):
        self.assertContains(
            self.client.get("/discord"), 'name="robots"')

    def test_the_page_points_at_support_when_the_invite_is_broken(self):
        self.assertContains(self.client.get("/discord"),
                            "mailto:support@pigscanfly.ca")
        self.assertContains(self.solve(), "mailto:support@pigscanfly.ca")

    def test_answers_spelled_out_in_words_are_accepted(self):
        # The question is worded, so a worded answer is a fair reading of it.
        self.client.get("/discord")
        answer = int(self.answer_for_the_pending_challenge())
        response = self.client.post(
            "/discord", {"captcha_answer": captcha.NUMBER_WORDS[answer]})
        self.assertContains(response, PART_TWO)


@override_settings(THUMBNAIL_DEBUG=False)
class DiscordFallbackTest(TestCase):
    """What /discord does when there is no usable invite to hand out.

    This is both the failure mode of a bad ConfigMap edit and the deliberate
    escape hatch: if the invite starts collecting bots, unset the halves and
    the page becomes "e-mail us for an invite".
    """

    def assert_email_fallback(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mailto:support@pigscanfly.ca")
        # No point making someone answer a question that leads nowhere.
        self.assertNotContains(response, "captcha_answer")

    @override_settings(DISCORD_INVITE_PART_ONE="",
                       DISCORD_INVITE_PART_TWO="")
    def test_unset_halves_render_the_email_fallback(self):
        with self.assertLogs("main.views", level="WARNING"):
            self.assert_email_fallback(self.client.get("/discord"))

    @override_settings(DISCORD_INVITE_PART_ONE="https://discord.gg/",
                       DISCORD_INVITE_PART_TWO="")
    def test_a_half_that_went_missing_renders_the_email_fallback(self):
        # Rather than a button pointing at an inviteless discord.gg URL.
        with self.assertLogs("main.views", level="WARNING"):
            self.assert_email_fallback(self.client.get("/discord"))

    @override_settings(DISCORD_INVITE_PART_ONE="javascript:alert(",
                       DISCORD_INVITE_PART_TWO="1)")
    def test_halves_that_are_not_a_discord_link_are_refused(self):
        # A ConfigMap is not a trust boundary the page should extend to the
        # browser: whatever is in it ends up as the href of a link we ask
        # people to click, so it has to be a discord.gg URL or nothing.
        with self.assertLogs("main.views", level="WARNING"):
            response = self.client.get("/discord")
        self.assert_email_fallback(response)
        self.assertNotContains(response, "javascript:alert(")

    @override_settings(DISCORD_INVITE_PART_ONE="http://discord.gg/plain",
                       DISCORD_INVITE_PART_TWO="Text")
    def test_a_plain_http_invite_is_refused(self):
        with self.assertLogs("main.views", level="WARNING"):
            self.assert_email_fallback(self.client.get("/discord"))

    @override_settings(DISCORD_INVITE_PART_ONE="https://discord.gg.evil.example/x",
                       DISCORD_INVITE_PART_TWO="yz")
    def test_a_lookalike_host_is_refused(self):
        with self.assertLogs("main.views", level="WARNING"):
            self.assert_email_fallback(self.client.get("/discord"))

    @override_settings(DISCORD_INVITE_PART_ONE="https://discord.gg/only",
                       DISCORD_INVITE_PART_TWO="")
    def test_an_empty_second_half_renders_the_email_fallback(self):
        # A working invite is never one half; an empty second half means the
        # ConfigMap lost a key.
        with self.assertLogs("main.views", level="WARNING"):
            self.assert_email_fallback(self.client.get("/discord"))


class DiscordCaptchaTest(TestCase):
    """The captcha itself, away from the view."""

    def setUp(self):
        self.session = self.client.session

    def test_a_challenge_is_answerable(self):
        question = captcha.new_challenge(self.session)
        self.assertIn("plus", question)
        answer = self.session[captcha.SESSION_KEY]["answer"]
        self.assertTrue(captcha.check_answer(self.session, str(answer)))

    def test_the_question_spells_its_numbers_out(self):
        # A digit-matching bot should not be able to read the operands off the
        # question and add them.
        question = captcha.new_challenge(self.session)
        self.assertFalse(any(char.isdigit() for char in question))

    def test_a_wrong_answer_fails(self):
        captcha.new_challenge(self.session)
        answer = self.session[captcha.SESSION_KEY]["answer"]
        self.assertFalse(captcha.check_answer(self.session, str(answer + 1)))

    def test_junk_answers_fail_rather_than_raise(self):
        for junk in ["", "   ", "seven-ish", "3.0", "NaN", "1e1", "+4"]:
            with self.subTest(answer=junk):
                captcha.new_challenge(self.session)
                self.assertFalse(captcha.check_answer(self.session, junk))

    def test_a_challenge_is_consumed_by_the_first_attempt(self):
        captcha.new_challenge(self.session)
        answer = self.session[captcha.SESSION_KEY]["answer"]
        self.assertTrue(captcha.check_answer(self.session, str(answer)))
        self.assertFalse(captcha.check_answer(self.session, str(answer)))

    def test_a_wrong_attempt_also_consumes_the_challenge(self):
        # Otherwise the same question can be brute-forced eight ways.
        captcha.new_challenge(self.session)
        answer = self.session[captcha.SESSION_KEY]["answer"]
        captcha.check_answer(self.session, "1000")
        self.assertFalse(captcha.check_answer(self.session, str(answer)))

    def test_checking_with_no_challenge_fails(self):
        self.assertFalse(captcha.check_answer(self.session, "4"))


class DiscordInviteConfigTest(TestCase):
    """The halves as they are actually shipped, in settings and in the
    manifest. A split that does not reassemble is a dead page, and neither the
    view tests (which override the halves) nor a YAML parse would notice."""

    def joined_is_a_valid_invite(self, first, second):
        return DiscordJoinView.INVITE_PATTERN.match(first + second) is not None

    def test_the_default_halves_join_into_an_invite(self):
        from pigscanfly.settings import Base

        self.assertTrue(self.joined_is_a_valid_invite(
            Base.DISCORD_INVITE_PART_ONE, Base.DISCORD_INVITE_PART_TWO))

    def test_the_manifest_halves_join_into_an_invite(self):
        with open(REPO_ROOT / "deploy.yaml") as fh:
            docs = [doc for doc in yaml.safe_load_all(fh) if doc]
        config_maps = [doc for doc in docs if doc.get("kind") == "ConfigMap"]
        data = {}
        for config_map in config_maps:
            data.update(config_map.get("data", {}))
        self.assertIn("DISCORD_INVITE_PART_ONE", data)
        self.assertIn("DISCORD_INVITE_PART_TWO", data)
        self.assertTrue(self.joined_is_a_valid_invite(
            data["DISCORD_INVITE_PART_ONE"], data["DISCORD_INVITE_PART_TWO"]))

    def test_whitespace_around_a_half_is_trimmed(self):
        # A ConfigMap value written as a YAML block, or pasted with a trailing
        # newline, would otherwise put whitespace in the middle of the joined
        # URL and turn the page into the e-mail fallback.
        from pigscanfly.settings import parse_invite_half

        self.assertEqual(
            parse_invite_half(" https://discord.gg/abc\n"),
            "https://discord.gg/abc")
        self.assertEqual(parse_invite_half("  def  "), "def")

    def test_the_split_cuts_through_the_invite_code(self):
        # Splitting at the last slash would pass every other test here and
        # defeat the exercise: the invite code -- the part that actually joins
        # a server -- would sit intact in one half, one obvious regex away
        # from being scraped. The cut has to go through the code itself.
        from pigscanfly.settings import Base

        first = Base.DISCORD_INVITE_PART_ONE
        second = Base.DISCORD_INVITE_PART_TWO
        code = (first + second).rsplit("/", 1)[-1]
        self.assertNotIn(code, first)
        self.assertNotIn(code, second)
