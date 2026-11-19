from types import SimpleNamespace
from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.socialaccount import app_settings as social_settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import RequestFactory, TestCase
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


class EmailSignupCodeVerificationTests(TestCase):
    """Email + password sign-ups confirm with a 6-digit code typed on the page."""

    def setUp(self):
        # allauth rate-limits confirmation emails (kept in the cache); start each test clean.
        from django.core.cache import cache

        cache.clear()

    def _signup(self):
        return self.client.post(
            reverse("account_signup"),
            {"email": "newcustomer@example.com", "password1": "Strong-pass-9137", "password2": "Strong-pass-9137"},
        )

    def _code_from_email(self):
        import re

        match = re.search(r"\b(\d{6})\b", mail.outbox[-1].body)
        self.assertIsNotNone(match, "email should contain a 6-digit code")
        return match.group(1)

    def test_signup_emails_a_numeric_code_and_shows_code_page(self):
        response = self._signup()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your Elegentra verification code")
        self.assertIn("newcustomer@example.com", mail.outbox[0].to)
        self._code_from_email()
        self.assertNotIn("Localhost", mail.outbox[0].body)

        page = self.client.get(response.url) if response.status_code == 302 else response
        self.assertContains(page, "Enter 6-digit code")
        self.assertFalse(EmailAddress.objects.get(email="newcustomer@example.com").verified)

    def test_correct_code_verifies_email(self):
        self._signup()
        code = self._code_from_email()

        self.client.post(reverse("account_email_verification_sent"), {"code": code})

        self.assertTrue(EmailAddress.objects.get(email="newcustomer@example.com").verified)

    def test_wrong_code_does_not_verify(self):
        self._signup()
        code = self._code_from_email()
        wrong = "000000" if code != "000000" else "111111"

        self.client.post(reverse("account_email_verification_sent"), {"code": wrong})

        self.assertFalse(EmailAddress.objects.get(email="newcustomer@example.com").verified)
