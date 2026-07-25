"""Signup and login input handling.

These views take raw POST data and used to hand it straight to the ORM, so
the interesting cases are all the ones a browser would normally prevent.
"""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings


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


@override_settings(THUMBNAIL_DEBUG=False)
class LoginValidationTest(TestCase):
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
