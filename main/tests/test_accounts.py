"""Signup and login input handling.

These views take raw POST data and used to hand it straight to the ORM, so
the interesting cases are all the ones a browser would normally prevent.
"""

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from unittest import mock

from main.models import EmailIdentity


# Both classes follow a redirect into the home page, which thumbnails static
# assets. Those assets live in the sibling pcfweb-assets checkout and are
# gitignored here, so they are simply absent in CI -- and Dev sets
# THUMBNAIL_DEBUG=True, which makes easy_thumbnails raise on a missing source
# instead of degrading. Same override as PageSmokeTest, for the same reason.
@override_settings(THUMBNAIL_DEBUG=False)
class SignupValidationTest(TestCase):
    def test_signup_without_an_email_is_not_a_500(self):
        # This used to reach generate_username(None) and blow up on the
        # AttributeError.
        response = self.client.post("/signup", {"password": "hunter2hunter2"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("invalid=missing", response["Location"])
        self.assertFalse(User.objects.exists())

    def test_signup_without_a_password_creates_no_account(self):
        # set_password(None) silently produces an account with an unusable
        # password, which can never be logged into and looks like a working
        # signup to the person who just made it.
        response = self.client.post("/signup", {"email": "a@example.com"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("invalid=missing", response["Location"])
        self.assertFalse(User.objects.exists())

    def test_signup_rejects_an_unparseable_email(self):
        response = self.client.post(
            "/signup", {"email": "not-an-email", "password": "hunter2hunter2"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("invalid=email", response["Location"])
        self.assertFalse(User.objects.exists())

    def test_signup_creates_a_usable_account(self):
        response = self.client.post(
            "/signup", {"email": "a@example.com", "password": "hunter2hunter2"})

        self.assertRedirects(response, "/")
        user = User.objects.get()
        self.assertEqual(user.email, "a@example.com")
        self.assertTrue(user.check_password("hunter2hunter2"))
        self.assertEqual(user.email_identity.normalized_email, "a@example.com")

    def test_signup_normalizes_email_case_and_whitespace(self):
        response = self.client.post(
            "/signup",
            {"email": "  Person@Example.COM ", "password": "hunter2hunter2"})

        self.assertRedirects(response, "/")
        self.assertEqual(User.objects.get().email, "person@example.com")

        response = self.client.post(
            "/signup",
            {"email": "PERSON@example.com", "password": "another-password"})
        self.assertIn("in_use=true", response["Location"])
        self.assertEqual(User.objects.count(), 1)

    def test_signup_with_a_taken_email_says_so(self):
        User.objects.create(username="a", email="a@example.com")

        response = self.client.post(
            "/signup", {"email": "a@example.com", "password": "hunter2hunter2"})

        self.assertIn("in_use=true", response["Location"])
        self.assertEqual(User.objects.count(), 1)

    def test_a_duplicated_email_does_not_500_the_signup_page(self):
        # auth.User.email is not unique, so historic rows can share one; the
        # old .get() raised MultipleObjectsReturned here.
        User.objects.create(username="a", email="dup@example.com")
        User.objects.create(username="b", email="dup@example.com")

        response = self.client.post(
            "/signup", {"email": "dup@example.com", "password": "hunter2hunter2"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("in_use=true", response["Location"])
        self.assertEqual(User.objects.count(), 2)

    def test_database_rejects_duplicate_normalized_email_identity(self):
        EmailIdentity.objects.create(normalized_email="person@example.com")

        with self.assertRaises(IntegrityError), transaction.atomic():
            # bulk_create deliberately bypasses EmailIdentity.save(), proving
            # that case-insensitive uniqueness lives in the database too.
            EmailIdentity.objects.bulk_create([
                EmailIdentity(normalized_email="PERSON@example.com")])

    def test_signup_losing_the_email_identity_race_redirects_in_use(self):
        def reserve_during_validation(email):
            EmailIdentity.objects.create(normalized_email=email)

        with mock.patch("main.views.validate_email",
                        side_effect=reserve_during_validation):
            response = self.client.post(
                "/signup",
                {"email": "person@example.com",
                 "password": "hunter2hunter2"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("in_use=true", response["Location"])
        self.assertFalse(User.objects.exists())
        self.assertEqual(EmailIdentity.objects.count(), 1)

    def test_signup_retries_a_username_collision_without_claiming_email_in_use(self):
        User.objects.create(username="person", email="taken@example.net")

        with mock.patch(
                "main.views.generate_username",
                side_effect=["person", "person-alt"]):
            response = self.client.post(
                "/signup",
                {"email": "person@example.org",
                 "password": "hunter2hunter2"})

        self.assertRedirects(response, "/")
        user = User.objects.get(email="person@example.org")
        self.assertEqual(user.username, "person-alt")
        self.assertEqual(
            user.email_identity.normalized_email, "person@example.org")


@override_settings(THUMBNAIL_DEBUG=False)
class LoginValidationTest(TestCase):
    def test_login_normalizes_email_case_and_whitespace(self):
        user = User.objects.create(username="person", email="person@example.com")
        user.set_password("hunter2hunter2")
        user.save()

        response = self.client.post(
            "/login",
            {"email": "  PERSON@Example.COM ", "password": "hunter2hunter2"})

        self.assertRedirects(response, "/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_a_duplicated_email_does_not_500_the_login_page(self):
        wrong = User.objects.create(username="wrong", email="dup@example.com")
        wrong.set_password("not-the-one")
        wrong.save()
        right = User.objects.create(username="right", email="dup@example.com")
        right.set_password("hunter2hunter2")
        right.save()

        # Whichever row is found first, the one whose password matches is the
        # one that gets logged in.
        response = self.client.post(
            "/login", {"email": "dup@example.com", "password": "hunter2hunter2"})

        self.assertRedirects(response, "/")
        self.assertEqual(
            int(self.client.session["_auth_user_id"]), right.pk)

    def test_a_missing_password_is_a_failed_login_not_a_crash(self):
        response = self.client.post("/login", {"email": "a@example.com"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("valid=false", response["Location"])

    def test_an_unknown_email_is_a_failed_login(self):
        response = self.client.post(
            "/login", {"email": "nobody@example.com", "password": "x"})

        self.assertIn("valid=false", response["Location"])


@override_settings(THUMBNAIL_DEBUG=False)
class AccountHardeningTest(TestCase):
    """Password rules, attempt throttles, ?next= and POST-only logout."""

    def setUp(self):
        # The throttles count in the cache, which outlives a test.
        cache.clear()
        self.addCleanup(cache.clear)

    def _user(self, email="person@example.com", password="hunter2hunter2"):
        user = User.objects.create(username=email.split("@")[0], email=email)
        user.set_password(password)
        user.save()
        return user

    def test_signup_applies_the_configured_password_validators(self):
        # AUTH_PASSWORD_VALIDATORS used to be configured and never called.
        response = self.client.post(
            "/signup", {"email": "a@example.com", "password": "12345"})

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "too short", status_code=400)
        self.assertFalse(User.objects.filter(email="a@example.com").exists())
        self.assertFalse(EmailIdentity.objects.exists())

    def test_signup_is_throttled_per_source(self):
        from main.views import SIGNUP_ATTEMPTS_PER_HOUR
        for i in range(SIGNUP_ATTEMPTS_PER_HOUR):
            self.client.post("/signup", {"email": f"x{i}@example.com"})

        response = self.client.post(
            "/signup", {"email": "late@example.com",
                        "password": "hunter2hunter2"})

        self.assertIn("invalid=throttled", response["Location"])
        self.assertFalse(User.objects.filter(email="late@example.com").exists())

    def test_login_is_throttled_per_email_even_with_the_right_password(self):
        from main.views import LOGIN_ATTEMPTS_PER_HOUR_PER_EMAIL
        self._user()
        for _ in range(LOGIN_ATTEMPTS_PER_HOUR_PER_EMAIL):
            self.client.post(
                "/login", {"email": "person@example.com", "password": "nope"})

        response = self.client.post(
            "/login",
            {"email": "person@example.com", "password": "hunter2hunter2"})

        self.assertIn("valid=throttled", response["Location"])
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_follows_a_local_next(self):
        self._user()
        response = self.client.post(
            "/login", {"email": "person@example.com",
                       "password": "hunter2hunter2", "next": "/cart"})

        self.assertRedirects(response, "/cart", fetch_redirect_response=False)

    def test_login_ignores_an_offsite_next(self):
        self._user()
        response = self.client.post(
            "/login", {"email": "person@example.com",
                       "password": "hunter2hunter2",
                       "next": "https://evil.example/"})

        self.assertRedirects(response, "/")

    def test_the_login_page_carries_next_into_the_form(self):
        response = self.client.get("/login?next=/cart")

        self.assertContains(response, 'name="next" value="/cart"')

    def test_logout_by_get_does_not_log_out(self):
        self.client.force_login(self._user())

        response = self.client.get("/logout")

        self.assertEqual(response.status_code, 200)
        self.assertIn("_auth_user_id", self.client.session)

    def test_logout_by_post_logs_out(self):
        self.client.force_login(self._user())

        response = self.client.post("/logout")

        self.assertRedirects(response, "/login")
        self.assertNotIn("_auth_user_id", self.client.session)
