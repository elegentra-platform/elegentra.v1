from types import SimpleNamespace
from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.socialaccount import app_settings as social_settings
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from .adapters import CustomSocialAccountAdapter

User = get_user_model()


class GoogleLoginEmailVerificationTests(TestCase):
    """Google-verified emails never need our confirmation email."""

    def _sociallogin(self, email, verified=True, existing=False):
        return SimpleNamespace(
            user=User(email=email),
            email_addresses=[EmailAddress(email=email, verified=verified)],
            is_existing=existing,
            connect=lambda request, user: None,
        )

    def test_social_logins_skip_email_verification(self):
        self.assertEqual(social_settings.EMAIL_VERIFICATION, "none")

    def test_existing_unverified_account_is_marked_verified_on_google_login(self):
        user = User.objects.create_user(username="old", email="old@example.com", password="pass12345")
        EmailAddress.objects.create(user=user, email="old@example.com", verified=False, primary=True)
        request = RequestFactory().get("/")

        with patch("accounts.adapters.user_email", return_value="old@example.com"):
            CustomSocialAccountAdapter().pre_social_login(request, self._sociallogin("old@example.com"))

        self.assertTrue(EmailAddress.objects.get(user=user, email="old@example.com").verified)

    def test_unverified_google_email_is_not_trusted(self):
        user = User.objects.create_user(username="old2", email="old2@example.com", password="pass12345")
        EmailAddress.objects.create(user=user, email="old2@example.com", verified=False, primary=True)
        request = RequestFactory().get("/")

        with patch("accounts.adapters.user_email", return_value="old2@example.com"):
            CustomSocialAccountAdapter().pre_social_login(
                request, self._sociallogin("old2@example.com", verified=False)
            )

        self.assertFalse(EmailAddress.objects.get(user=user, email="old2@example.com").verified)


GOOGLE_TEST_PROVIDERS = {
    "google": {
        "SCOPE": ["profile", "email"],
        "APPS": [{"client_id": "test-client-id.apps.googleusercontent.com", "secret": "s", "key": ""}],
    }
}


@override_settings(SOCIALACCOUNT_PROVIDERS=GOOGLE_TEST_PROVIDERS)
class GoogleOnlyLoginTests(TestCase):
    """Google is the only way in: no passwords, no sign-up form, plus One Tap."""

    def test_login_page_has_google_only(self):
        response = self.client.get(reverse("account_login") + "?next=/favorites/")
        self.assertContains(response, "Continue with Google")
        self.assertContains(response, "googleOneTapForm")  # One Tap popup is set up
        self.assertContains(response, "accounts.google.com/gsi/client")
        self.assertNotContains(response, 'type="password"')
        self.assertNotContains(response, "Forgot password")

    def test_password_login_is_refused(self):
        User.objects.create_user(username="pw", email="pw@example.com", password="pass12345")
        response = self.client.post(reverse("account_login"), {"login": "pw@example.com", "password": "pass12345"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signup_link_goes_to_login(self):
        response = self.client.get(reverse("account_signup") + "?next=/saloons/partner/")
        self.assertRedirects(
            response, reverse("account_login") + "?next=/saloons/partner/", fetch_redirect_response=False
        )

    def test_one_tap_logs_in_and_returns_to_page(self):
        identity = {
            "iss": "https://accounts.google.com",
            "aud": "test-client-id.apps.googleusercontent.com",
            "sub": "1234567890",
            "email": "onetap@gmail.com",
            "email_verified": True,
            "name": "One Tap",
            "given_name": "One",
            "family_name": "Tap",
        }
        with patch("allauth.socialaccount.providers.google.views._verify_and_decode", return_value=identity):
            response = self.client.post(
                reverse("google_one_tap"), {"credential": "fake.jwt.token", "next": "/salon/glow/"}
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/salon/glow/")
        user = User.objects.get(email="onetap@gmail.com")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_one_tap_links_existing_account_with_same_email(self):
        existing = User.objects.create_user(username="old3", email="old3@gmail.com", password="pass12345")
        identity = {
            "iss": "https://accounts.google.com",
            "aud": "test-client-id.apps.googleusercontent.com",
            "sub": "999",
            "email": "old3@gmail.com",
            "email_verified": True,
        }
        with patch("allauth.socialaccount.providers.google.views._verify_and_decode", return_value=identity):
            self.client.post(reverse("google_one_tap"), {"credential": "fake.jwt.token"})

        self.assertEqual(int(self.client.session["_auth_user_id"]), existing.pk)
        self.assertEqual(User.objects.filter(email__iexact="old3@gmail.com").count(), 1)

    def test_one_tap_rejects_bad_token(self):
        from allauth.socialaccount.providers.oauth2.client import OAuth2Error

        with patch(
            "allauth.socialaccount.providers.google.views._verify_and_decode", side_effect=OAuth2Error("bad")
        ):
            response = self.client.post(reverse("google_one_tap"), {"credential": "forged"})

        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotEqual(response.status_code, 302)

    def test_one_tap_ignores_outside_redirects(self):
        identity = {
            "iss": "https://accounts.google.com",
            "aud": "test-client-id.apps.googleusercontent.com",
            "sub": "555",
            "email": "safe@gmail.com",
            "email_verified": True,
        }
        with patch("allauth.socialaccount.providers.google.views._verify_and_decode", return_value=identity):
            response = self.client.post(
                reverse("google_one_tap"), {"credential": "t", "next": "https://evil.example.com/"}
            )
        self.assertNotIn("evil.example.com", response.url)

    def test_one_tap_get_not_allowed(self):
        self.assertEqual(self.client.get(reverse("google_one_tap")).status_code, 405)
