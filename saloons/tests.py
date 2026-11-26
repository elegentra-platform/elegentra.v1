from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from plans.billing import get_default_subscription_plan
from plans.models import SaloonSubscription

from .models import (
    Saloon,
    TrustedDevice,
    SaloonProfile,
    SaloonReview,
    SaloonProfileView,
    SaloonMapClick,
)
from services.models import ServiceCategory, Service, ServiceInterest


User = get_user_model()


class WhatsAppCloudOtpTests(TestCase):
    """OTP goes through Meta's WhatsApp Cloud API using the authentication template."""

    ENV = {
        "WHATSAPP_ACCESS_TOKEN": "unit-token",
        "WHATSAPP_PHONE_NUMBER_ID": "1384322154772584",
        "WHATSAPP_OTP_TEMPLATE": "elegentra_otp",
    }

    def test_otp_is_sent_as_authentication_template(self):
        import json

        from .utils import send_otp_whatsapp

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["payload"] = json.loads(request.data.decode("utf-8"))

            class Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *args):
                    return False

                def read(self_inner):
                    return json.dumps({"messages": [{"id": "wamid.unit"}]}).encode("utf-8")

            return Resp()

        with patch.dict("os.environ", self.ENV), patch("saloons.utils.urlopen", side_effect=fake_urlopen):
            message_id = send_otp_whatsapp("+919074492286", 482913)

        self.assertEqual(message_id, "wamid.unit")
        self.assertIn("/1384322154772584/messages", captured["url"])
        self.assertEqual(captured["auth"], "Bearer unit-token")
        payload = captured["payload"]
        self.assertEqual(payload["to"], "919074492286")
        self.assertEqual(payload["template"]["name"], "elegentra_otp")
        body, button = payload["template"]["components"]
        self.assertEqual(body["parameters"][0]["text"], "482913")
        self.assertEqual(button["sub_type"], "url")
        self.assertEqual(button["parameters"][0]["text"], "482913")

    def test_missing_credentials_raise_outside_debug(self):
        from django.core.exceptions import ImproperlyConfigured

        from .utils import send_otp_whatsapp

        with patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": "", "WHATSAPP_PHONE_NUMBER_ID": ""}), \
                self.settings(DEBUG=False):
            with self.assertRaises(ImproperlyConfigured):
                send_otp_whatsapp("+919074492286", 123456)

    def test_meta_error_is_raised_with_reason(self):
        import io
        from urllib.error import HTTPError

        from .utils import send_otp_whatsapp

        error_body = io.BytesIO(b'{"error": {"code": 132001, "message": "Template name does not exist"}}')
        error = HTTPError("https://graph.facebook.com", 404, "Not Found", {}, error_body)

        with patch.dict("os.environ", self.ENV), patch("saloons.utils.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "132001"):
                send_otp_whatsapp("+919074492286", 123456)

    def test_sms_otp_uses_fast2sms_otp_route(self):
        import json

        from .utils import send_otp_sms

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["payload"] = json.loads(request.data.decode("utf-8"))

            class Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *args):
                    return False

                def read(self_inner):
                    return b'{"return": true, "request_id": "req123", "message": ["SMS sent successfully."]}'

            return Resp()

        with patch.dict("os.environ", {"FAST2SMS_API_KEY": "unit-key"}), \
                patch("saloons.utils.urlopen", side_effect=fake_urlopen):
            request_id = send_otp_sms("+919074492286", 482913)

        self.assertEqual(request_id, "req123")
        self.assertEqual(captured["url"], "https://www.fast2sms.com/dev/bulkV2")
        self.assertEqual(captured["auth"], "unit-key")
        self.assertEqual(captured["payload"], {"route": "otp", "variables_values": "482913", "numbers": "9074492286"})

    def test_sms_rejection_raises_with_reason(self):
        from .utils import send_otp_sms

        class Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                return b'{"status_code": 996, "message": "Before using OTP Message API, complete website verification."}'

        with patch.dict("os.environ", {"FAST2SMS_API_KEY": "unit-key"}), \
                patch("saloons.utils.urlopen", return_value=Resp()):
            with self.assertRaisesRegex(RuntimeError, "website verification"):
                send_otp_sms("+919074492286", 123456)

    def test_sms_only_for_indian_numbers(self):
        from .utils import send_otp_sms

        with patch.dict("os.environ", {"FAST2SMS_API_KEY": "unit-key"}):
            with self.assertRaisesRegex(RuntimeError, r"\+91"):
                send_otp_sms("+14155550123", 123456)

    def test_generated_otps_are_six_digits(self):
        from .utils import generate_otp

        codes = {generate_otp() for _ in range(200)}
        self.assertTrue(all(100000 <= code <= 999999 for code in codes))
        self.assertGreater(len(codes), 150)


FIREBASE_TEST_CONFIG = {
    "apiKey": "test-api-key",
    "authDomain": "elegentra-app.firebaseapp.com",
    "projectId": "elegentra-app",
    "appId": "1:123:web:abc",
}


@override_settings(FIREBASE_WEB_CONFIG=FIREBASE_TEST_CONFIG)
class FirebaseSmsVerificationTests(TestCase):
    """SMS codes sent by Firebase; the server only trusts a Google-verified ID token."""

    def setUp(self):
        self.user = User.objects.create_user(username="fbowner", email="fb@example.com", password="pass12345")
        self.saloon = Saloon.objects.create(
            owner=self.user,
            name="Firebase Salon",
            whatsapp_number="+919074492286",
            is_whatsapp_verified=False,
            registration_step=5,
        )
        self.client.force_login(self.user)
        self.page_url = reverse("verify_whatsapp")
        self.url = reverse("verify_whatsapp_firebase")

    def _post(self, token="token"):
        import json

        return self.client.post(self.url, data=json.dumps({"id_token": token}), content_type="application/json")

    @patch("saloons.utils.send_otp_whatsapp")
    def test_page_shows_firebase_sms_and_does_not_auto_send(self, send_otp_mock):
        from django.core import mail

        response = self.client.get(self.page_url)

        self.assertEqual(response.context["step"], "phone")
        self.assertContains(response, "firebasejs/13.2.0/firebase-auth.js")
        self.assertContains(response, "Get the code by email instead")
        send_otp_mock.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)

    @patch("saloons.views.verify_phone_id_token", return_value="+919074492286")
    def test_matching_verified_number_completes_verification(self, verify_mock):
        response = self._post()

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["redirect_url"], reverse("saloon_dashboard", kwargs={"username": "fbowner"}))
        self.assertIn("salon_device", response.cookies)
        self.saloon.refresh_from_db()
        self.assertTrue(self.saloon.is_whatsapp_verified)

    @patch("saloons.views.verify_phone_id_token", return_value="+919000000001")
    def test_different_verified_number_is_rejected(self, verify_mock):
        response = self._post()

        self.assertEqual(response.status_code, 400)
        self.saloon.refresh_from_db()
        self.assertFalse(self.saloon.is_whatsapp_verified)

    @patch("saloons.views.verify_phone_id_token", return_value="+919111111111")
    def test_new_partner_creates_salon_with_verified_number(self, verify_mock):
        new_user = User.objects.create_user(username="newpartner", email="np@example.com", password="pass12345")
        self.client.force_login(new_user)

        response = self._post()

        self.assertEqual(response.json()["redirect_url"], reverse("saloon_onboarding_step_one"))
        salon = Saloon.objects.get(owner=new_user)
        self.assertEqual(salon.whatsapp_number, "+919111111111")
        self.assertTrue(salon.is_whatsapp_verified)

    @patch("saloons.views.verify_phone_id_token", return_value="+919222222222")
    def test_number_of_another_verified_salon_is_rejected(self, verify_mock):
        other = User.objects.create_user(username="other", email="o@example.com", password="pass12345")
        Saloon.objects.create(owner=other, name="Other", whatsapp_number="+919222222222", is_whatsapp_verified=True)
        new_user = User.objects.create_user(username="taker", email="t@example.com", password="pass12345")
        self.client.force_login(new_user)

        response = self._post()

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Saloon.objects.filter(owner=new_user).exists())

    def test_invalid_token_is_rejected(self):
        from .firebase_auth import FirebaseVerificationError

        with patch("saloons.views.verify_phone_id_token", side_effect=FirebaseVerificationError("bad token")):
            response = self._post()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["message"], "bad token")

    @override_settings(FIREBASE_WEB_CONFIG={})
    def test_disabled_when_not_configured(self):
        response = self._post()
        self.assertEqual(response.status_code, 400)

    def _claims(self, **overrides):
        import time

        claims = {
            "iss": "https://securetoken.google.com/elegentra-app",
            "aud": "elegentra-app",
            "auth_time": time.time() - 30,
            "phone_number": "+919074492286",
            "firebase": {"sign_in_provider": "phone"},
        }
        claims.update(overrides)
        return claims

    def test_token_verifier_accepts_fresh_phone_sign_in(self):
        from .firebase_auth import verify_phone_id_token

        with patch("google.oauth2.id_token.verify_firebase_token", return_value=self._claims()) as google_mock:
            self.assertEqual(verify_phone_id_token("token"), "+919074492286")
        self.assertEqual(google_mock.call_args.kwargs["audience"], "elegentra-app")

    def test_token_verifier_rejects_old_wrong_provider_or_wrong_project(self):
        import time

        from .firebase_auth import FirebaseVerificationError, verify_phone_id_token

        bad_claims = [
            self._claims(auth_time=time.time() - 3600),
            self._claims(firebase={"sign_in_provider": "password"}),
            self._claims(iss="https://securetoken.google.com/someone-else"),
            self._claims(phone_number=""),
        ]
        for claims in bad_claims:
            with patch("google.oauth2.id_token.verify_firebase_token", return_value=claims):
                with self.assertRaises(FirebaseVerificationError):
                    verify_phone_id_token("token")


@override_settings(FIREBASE_WEB_CONFIG={})
class VerifyWhatsAppSecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="pass12345",
        )
        self.saloon = Saloon.objects.create(
            owner=self.user,
            name="Safe Salon",
            whatsapp_number="+919999999999",
            is_whatsapp_verified=True,
            registration_step=5,
        )
        self.client.force_login(self.user)
        self.url = reverse("verify_whatsapp")

    @patch("saloons.utils.send_otp_sms")
    @patch("saloons.utils.send_otp_whatsapp", side_effect=RuntimeError("template not available"))
    def test_auto_fallback_skips_fast2sms(self, send_otp_mock, send_sms_mock):
        from django.core import mail

        self.client.post(self.url, {"send_otp": "1"})

        send_sms_mock.assert_not_called()
        self.assertEqual(self.client.session["vendor_otp_channel"], "email")
        self.assertEqual(len(mail.outbox), 1)

    @patch("saloons.utils.send_otp_sms")
    @patch("saloons.utils.send_otp_whatsapp")
    def test_sms_mode_uses_fast2sms(self, send_otp_mock, send_sms_mock):
        with patch.dict("os.environ", {"SALON_OTP_CHANNEL": "sms"}):
            response = self.client.post(self.url, {"send_otp": "1"})

        send_otp_mock.assert_not_called()
        send_sms_mock.assert_called_once_with(self.saloon.whatsapp_number, int(self.client.session["vendor_otp"]))
        self.assertContains(response, "by SMS")

    @patch("saloons.utils.send_otp_sms", side_effect=RuntimeError("website verification pending"))
    @patch("saloons.utils.send_otp_whatsapp", side_effect=RuntimeError("template not available"))
    def test_falls_back_to_email_when_whatsapp_and_sms_unavailable(self, send_otp_mock, send_sms_mock):
        from django.core import mail

        response = self.client.post(self.url, {"send_otp": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["step"], "otp")
        self.assertEqual(self.client.session["vendor_otp_channel"], "email")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@example.com"])
        self.assertIn(self.client.session["vendor_otp"], mail.outbox[0].body)
        self.assertContains(response, "o***r@example.com")

        # The emailed code verifies the salon like a WhatsApp code would.
        verify = self.client.post(self.url, {"verify_otp": "1", "otp": self.client.session["vendor_otp"]})
        self.assertEqual(verify.status_code, 302)

    @patch("saloons.utils.send_otp_sms")
    @patch("saloons.utils.send_otp_whatsapp", side_effect=RuntimeError("template not available"))
    def test_whatsapp_only_mode_does_not_fall_back(self, send_otp_mock, send_sms_mock):
        from django.core import mail

        with patch.dict("os.environ", {"SALON_OTP_CHANNEL": "whatsapp"}):
            response = self.client.post(self.url, {"send_otp": "1"})

        self.assertEqual(response.context["step"], "phone")
        send_sms_mock.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)

    @patch("saloons.utils.send_otp_whatsapp")
    def test_email_only_mode_skips_whatsapp(self, send_otp_mock):
        from django.core import mail

        with patch.dict("os.environ", {"SALON_OTP_CHANNEL": "email"}):
            response = self.client.post(self.url, {"send_otp": "1"})

        send_otp_mock.assert_not_called()
        self.assertEqual(response.context["step"], "otp")
        self.assertEqual(len(mail.outbox), 1)

    @patch("saloons.utils.send_otp_whatsapp")
    def test_existing_saloon_verification_uses_stored_number_only(self, send_otp_mock):
        response = self.client.post(
            self.url,
            {"send_otp": "1", "phone": "+911111111111"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["vendor_phone"], self.saloon.whatsapp_number)
        send_otp_mock.assert_called_once_with(self.saloon.whatsapp_number, int(self.client.session["vendor_otp"]))
        self.assertContains(response, self.saloon.whatsapp_number[-4:])
        self.assertNotContains(response, 'name="phone"', html=False)

    @patch("saloons.utils.send_otp_whatsapp")
    def test_existing_saloon_verification_never_changes_saved_whatsapp(self, send_otp_mock):
        session = self.client.session
        session["vendor_phone"] = self.saloon.whatsapp_number
        session["vendor_otp"] = "123456"
        session.save()

        response = self.client.post(
            self.url,
            {"verify_otp": "1", "otp": "123456"},
        )

        self.assertEqual(response.status_code, 302)
        self.saloon.refresh_from_db()
        self.assertEqual(self.saloon.whatsapp_number, "+919999999999")
        self.assertTrue(self.saloon.is_whatsapp_verified)
        self.assertTrue(TrustedDevice.objects.filter(user=self.user).exists())

    @patch("saloons.utils.send_otp_whatsapp")
    def test_existing_saloon_get_resets_tampered_session_phone(self, send_otp_mock):
        session = self.client.session
        session["vendor_phone"] = "+911111111111"
        session["vendor_otp"] = "654321"
        session.save()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        session = self.client.session
        self.assertEqual(session["vendor_phone"], self.saloon.whatsapp_number)
        self.assertNotEqual(session["vendor_otp"], "654321")
        self.assertEqual(response.context["step"], "otp")
        send_otp_mock.assert_called_once()
        self.assertContains(response, self.saloon.whatsapp_number[-4:])


class PublicReviewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="vendor",
            email="vendor@example.com",
            password="pass12345",
        )
        self.customer = User.objects.create_user(
            username="customer",
            email="customer@example.com",
            password="pass12345",
            first_name="Anu",
        )
        self.saloon = Saloon.objects.create(
            owner=self.user,
            name="Glow Salon",
            slug="glow-salon",
            whatsapp_number="+919888888888",
            is_whatsapp_verified=True,
            registration_step=5,
            approval_status=Saloon.APPROVAL_APPROVED,
        )
        SaloonProfile.objects.create(
            saloon=self.saloon,
            saloon_name="Glow Salon",
            owner_full_name="Owner",
            contact_number="+919888888888",
            category="unisex",
            city="Kochi",
            locality="MG Road",
        )
        # Public pages only list salons with a live trial/subscription.
        SaloonSubscription.objects.create(
            saloon=self.saloon,
            plan=get_default_subscription_plan(),
            trial_ends_at=timezone.now() + timedelta(days=14),
        )
        self.category = ServiceCategory.objects.create(name="Haircut")
        self.service = Service.objects.create(
            saloon=self.saloon,
            category=self.category,
            name="Haircut",
            price="250.00",
            image="services/images/test.jpg",
            is_active=True,
        )

    def test_public_review_submission_creates_review(self):
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse("public_saloon_add_review", kwargs={"slug": self.saloon.slug}),
            {
                "rating": "5",
                "review_text": "Very clean place and friendly staff.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SaloonReview.objects.filter(saloon=self.saloon).count(), 1)
        review = SaloonReview.objects.get(saloon=self.saloon)
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.reviewer_name, "customer")

    def test_public_saloon_shows_real_review_summary(self):
        SaloonReview.objects.create(
            saloon=self.saloon,
            reviewer_name="Anu",
            rating=4,
            review_text="Good service",
        )
        SaloonReview.objects.create(
            saloon=self.saloon,
            reviewer_name="Ria",
            rating=5,
            review_text="Loved it",
        )

        response = self.client.get(reverse("public_saloon", kwargs={"slug": self.saloon.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["review_summary"]["average"], 4.5)
        self.assertEqual(response.context["review_summary"]["total"], 2)

    def test_guest_cannot_submit_review(self):
        response = self.client.post(
            reverse("public_saloon_add_review", kwargs={"slug": self.saloon.slug}),
            {
                "rating": "5",
                "review_text": "Nice salon",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SaloonReview.objects.filter(saloon=self.saloon).count(), 0)

    def test_logged_in_user_can_edit_own_review(self):
        review = SaloonReview.objects.create(
            saloon=self.saloon,
            reviewer_name=self.customer.username,
            rating=4,
            review_text="Good service",
        )
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("public_saloon_edit_review", kwargs={"slug": self.saloon.slug, "review_id": review.id}),
            {
                "rating": "5",
                "review_text": "Great service",
            },
        )

        self.assertEqual(response.status_code, 302)
        review.refresh_from_db()
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.review_text, "Great service")

    def test_logged_in_user_can_delete_own_review(self):
        review = SaloonReview.objects.create(
            saloon=self.saloon,
            reviewer_name=self.customer.username,
            rating=4,
            review_text="Good service",
        )
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("public_saloon_delete_review", kwargs={"slug": self.saloon.slug, "review_id": review.id}),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SaloonReview.objects.filter(id=review.id).exists())

    def test_public_service_interest_is_recorded(self):
        response = self.client.post(
            reverse("public_service_interest", kwargs={"slug": self.saloon.slug}),
            {"service_id": str(self.service.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ServiceInterest.objects.filter(service=self.service).count(), 1)

    def test_public_saloon_page_records_profile_view(self):
        response = self.client.get(reverse("public_saloon", kwargs={"slug": self.saloon.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SaloonProfileView.objects.filter(saloon=self.saloon).count(), 1)

    def test_public_map_click_is_recorded(self):
        response = self.client.post(reverse("public_saloon_map_click", kwargs={"slug": self.saloon.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SaloonMapClick.objects.filter(saloon=self.saloon).count(), 1)


class DashboardMetricsCutoffTests(TestCase):
    """Analytics shown up to a cut-off date must ignore every later event."""

    def test_contact_clicks_after_cutoff_are_not_counted(self):
        from .dashboard_views import _build_dashboard_metrics
        from .models import SaloonContactClick

        owner = User.objects.create_user(username="cutoff", email="c@example.com", password="pass12345")
        saloon = Saloon.objects.create(owner=owner, name="Cutoff Salon", whatsapp_number="+919800000001")
        cutoff = timezone.now() - timedelta(days=5)

        before = SaloonContactClick.objects.create(saloon=saloon, kind=SaloonContactClick.WHATSAPP)
        SaloonContactClick.objects.filter(pk=before.pk).update(created_at=cutoff - timedelta(days=1))
        SaloonContactClick.objects.create(saloon=saloon, kind=SaloonContactClick.WHATSAPP)  # after cut-off
        SaloonContactClick.objects.create(saloon=saloon, kind=SaloonContactClick.CALL)  # after cut-off

        metrics = _build_dashboard_metrics(saloon, analytics_until=cutoff)

        self.assertEqual(metrics["whatsapp_click_total"], 1)
        self.assertEqual(metrics["phone_click_total"], 0)


class MonthlyReportTests(TestCase):
    """Monthly WhatsApp report: wording, template choice, sending and opt-out."""

    def setUp(self):
        from plans.billing import get_or_create_saloon_subscription

        self.report_date = date(2026, 12, 29)
        self.owner = User.objects.create_user(
            username="glowsalon", email="g@example.com", password="pass12345", first_name="Rahul"
        )
        self.saloon = Saloon.objects.create(
            owner=self.owner,
            name="Glow Salon",
            whatsapp_number="+919800000002",
            is_whatsapp_verified=True,
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
        )
        SaloonProfile.objects.get_or_create(saloon=self.saloon)
        subscription, _ = get_or_create_saloon_subscription(self.saloon)
        SaloonSubscription.objects.filter(pk=subscription.pk).update(
            trial_assigned_at=timezone.now() - timedelta(days=60),
            trial_ends_at=timezone.now() + timedelta(days=60),
        )

    def _events(self, model, count, when, **fields):
        for _ in range(count):
            obj = model.objects.create(saloon=self.saloon, **fields)
            model.objects.filter(pk=obj.pk).update(created_at=when)

    def _at(self, month, day):
        return timezone.make_aware(datetime(2026, month, day, 12, 0))

    def test_wording(self):
        from . import monthly_report as r

        self.assertEqual(r.visitors_text(0), "No one")
        self.assertEqual(r.visitors_text(1), "1 person")
        self.assertEqual(r.visitors_text(4), "4 people")
        self.assertEqual(r.enquiries_text(1), "1 WhatsApp enquiry")
        self.assertEqual(r.enquiries_text(0), "No WhatsApp enquiries")
        self.assertEqual(r.calls_text(1), "1 call")
        self.assertEqual(r.customers_text(0), "No customers")
        self.assertEqual(r.comparison_text(124, 154), "+30 visits (124 → 154)")
        self.assertEqual(r.comparison_text(108, 96), "−12 visits (108 → 96)")
        self.assertEqual(r.comparison_text(120, 120), "no change (120 → 120)")
        self.assertEqual(r.comparison_text(3, 4), "+1 visit (3 → 4)")

    def test_review_and_service_text(self):
        from . import monthly_report as r

        self.assertEqual(r.reviews_text(r.SalonStats()), "No new reviews this month")
        self.assertEqual(
            r.reviews_text(r.SalonStats(new_reviews=1, average_rating=5.0)), "1 new review · 5★"
        )
        self.assertEqual(
            r.reviews_text(r.SalonStats(new_reviews=3, average_rating=4.6667)),
            "3 new reviews · 4.7★ average",
        )
        self.assertEqual(r.top_service_text(r.SalonStats()), "No service interest yet")
        self.assertEqual(
            r.top_service_text(r.SalonStats(top_service="Bridal makeup", top_service_interest=21)),
            "Bridal makeup · 21 people interested",
        )

    def test_period_compares_same_days(self):
        from . import monthly_report as r

        period = r.report_period(self.report_date)
        self.assertEqual(period.key, "2026-12")
        self.assertEqual(period.label, "December")
        self.assertEqual(period.previous_label, "November")
        self.assertEqual(timezone.localtime(period.end).day, 29)  # 1st-28th, end is exclusive
        self.assertEqual(timezone.localtime(period.previous_end).day, 29)
        march = r.report_period(date(2027, 3, 29))
        self.assertEqual(timezone.localtime(march.previous_end).month, 3)  # all of Feb
        self.assertTrue(r.is_report_day(date(2027, 2, 28)))
        self.assertFalse(r.is_report_day(date(2026, 12, 28)))

    def test_counts_only_this_period(self):
        from . import monthly_report as r
        from .models import SaloonContactClick

        self._events(SaloonProfileView, 5, self._at(12, 10))
        self._events(SaloonProfileView, 2, self._at(12, 29))  # report day itself: not counted
        self._events(SaloonProfileView, 3, self._at(11, 15))  # previous month
        self._events(SaloonProfileView, 4, self._at(11, 30))  # outside Nov 1-28
        self._events(SaloonMapClick, 2, self._at(12, 3))
        self._events(SaloonContactClick, 1, self._at(12, 4), kind=SaloonContactClick.WHATSAPP)
        self._events(SaloonContactClick, 2, self._at(12, 4), kind=SaloonContactClick.CALL)

        stats = r.collect_stats(self.saloon, r.report_period(self.report_date))
        self.assertEqual((stats.visits, stats.previous_visits), (5, 3))
        self.assertEqual((stats.location_checks, stats.whatsapp, stats.calls), (2, 1, 2))

    def test_template_choice(self):
        from . import monthly_report as r
        from .models import SaloonMonthlyReport

        self.assertEqual(r.choose_template(self.saloon, r.SalonStats(visits=1)), r.TEMPLATE_FIRST_MONTH)
        SaloonMonthlyReport.objects.create(
            saloon=self.saloon, period="2026-11", template_name=r.TEMPLATE_FIRST_MONTH,
            status=SaloonMonthlyReport.STATUS_SENT,
        )

        def choose(prev, cur):
            return r.choose_template(self.saloon, r.SalonStats(visits=cur, previous_visits=prev))

        self.assertEqual(choose(100, 84), r.TEMPLATE_DOWN)  # 16% drop
        self.assertEqual(choose(100, 85), r.TEMPLATE_UP)    # exactly 15%: not quieter
        self.assertEqual(choose(10, 2), r.TEMPLATE_UP)      # small numbers never "quieter"
        self.assertEqual(choose(20, 16), r.TEMPLATE_DOWN)
        self.assertEqual(choose(50, 80), r.TEMPLATE_UP)

    @patch("saloons.monthly_report.send_whatsapp_template", return_value="wamid.TEST")
    def test_send_records_and_is_sent_once(self, send):
        from .models import SaloonMonthlyReport

        call_command("send_monthly_reports", date="2026-12-29", stdout=StringIO())
        call_command("send_monthly_reports", date="2026-12-29", stdout=StringIO())

        self.assertEqual(send.call_count, 1)
        phone, template, language, components = send.call_args.args
        self.assertEqual(phone, "+919800000002")
        self.assertEqual(template, "elegentra_salon_report_first")
        body = components[0]["parameters"]
        self.assertEqual(len(body), 10)
        self.assertEqual(body[0]["text"], "Rahul")
        self.assertEqual(body[3]["text"], "No one")
        self.assertEqual(components[1]["parameters"][0]["text"], "glowsalon")
        report = SaloonMonthlyReport.objects.get(saloon=self.saloon)
        self.assertEqual((report.period, report.status, report.message_id), ("2026-12", "sent", "wamid.TEST"))

    @patch("saloons.monthly_report.send_whatsapp_template", return_value="wamid.TEST")
    def test_not_sent_on_other_days_or_when_turned_off(self, send):
        call_command("send_monthly_reports", date="2026-12-20", stdout=StringIO())
        self.assertEqual(send.call_count, 0)

        self.saloon.profile.monthly_report_enabled = False
        self.saloon.profile.save()
        call_command("send_monthly_reports", date="2026-12-29", stdout=StringIO())
        self.assertEqual(send.call_count, 0)

    @patch("saloons.monthly_report.send_whatsapp_template", return_value="wamid.TEST")
    def test_skips_test_unverified_and_new_salons(self, send):
        Saloon.objects.filter(pk=self.saloon.pk).update(is_test_saloon=True)
        call_command("send_monthly_reports", date="2026-12-29", stdout=StringIO())
        Saloon.objects.filter(pk=self.saloon.pk).update(is_test_saloon=False, is_whatsapp_verified=False)
        call_command("send_monthly_reports", date="2026-12-29", stdout=StringIO())
        Saloon.objects.filter(pk=self.saloon.pk).update(is_whatsapp_verified=True)
        SaloonSubscription.objects.filter(saloon=self.saloon).update(trial_assigned_at=self._at(12, 26))
        call_command("send_monthly_reports", date="2026-12-29", stdout=StringIO())
        self.assertEqual(send.call_count, 0)

    @patch("saloons.monthly_report.send_whatsapp_template", side_effect=RuntimeError("boom"))
    def test_failure_is_recorded_and_retried(self, send):
        from .models import SaloonMonthlyReport

        call_command("send_monthly_reports", date="2026-12-29", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(SaloonMonthlyReport.objects.get(saloon=self.saloon).status, "failed")
        send.side_effect = None
        send.return_value = "wamid.OK"
        call_command("send_monthly_reports", date="2026-12-29", stdout=StringIO())
        self.assertEqual(SaloonMonthlyReport.objects.get(saloon=self.saloon).status, "sent")

    @patch("saloons.monthly_report.send_whatsapp_template", return_value="wamid.PREVIEW")
    def test_preview_is_not_recorded(self, send):
        from .models import SaloonMonthlyReport

        call_command(
            "send_monthly_reports", date="2026-12-20", only_saloon=self.saloon.pk, to="9876543210",
            stdout=StringIO(),
        )
        self.assertEqual(send.call_args.args[0], "+919876543210")
        self.assertFalse(SaloonMonthlyReport.objects.exists())

    def test_settings_toggle(self):
        device = TrustedDevice.objects.create(user=self.owner)
        self.client.force_login(self.owner)
        self.client.cookies["salon_device"] = str(device.device_token)
        url = reverse("saloon_dashboard_settings", kwargs={"username": "glowsalon"})
        self.client.post(url, {"form": "monthly_report"})
        self.saloon.profile.refresh_from_db()
        self.assertFalse(self.saloon.profile.monthly_report_enabled)
        self.client.post(url, {"form": "monthly_report", "monthly_report_enabled": "1"})
        self.saloon.profile.refresh_from_db()
        self.assertTrue(self.saloon.profile.monthly_report_enabled)


class PhoneNumberValidationTests(TestCase):
    def test_indian_numbers_must_have_ten_digits_starting_6_to_9(self):
        from .utils import format_whatsapp_number as fmt

        self.assertEqual(fmt("+919074492286"), "+919074492286")
        self.assertEqual(fmt("+91 90744 92286"), "+919074492286")
        self.assertEqual(fmt("+9190874492286"), "")   # extra digit (real typo found locally)
        self.assertEqual(fmt("+91907449228"), "")     # one digit short
        self.assertEqual(fmt("+915074492286"), "")    # Indian mobiles start 6-9
        self.assertEqual(fmt("+14155552671"), "+14155552671")  # other countries unchanged


@override_settings(FIREBASE_WEB_CONFIG=FIREBASE_TEST_CONFIG)
class InvalidSavedNumberRepairTests(TestCase):
    """A salon saved with an impossible number re-enters and proves the right one."""

    def setUp(self):
        self.user = User.objects.create_user(username="typo", email="typo@example.com", password="pass12345")
        self.saloon = Saloon.objects.create(
            owner=self.user, name="Typo Salon", whatsapp_number="+9190874492286",
            is_whatsapp_verified=True, registration_step=5,
        )
        self.client.force_login(self.user)

    def _verify(self, number):
        import json

        with patch("saloons.views.verify_phone_id_token", return_value=number):
            return self.client.post(
                reverse("verify_whatsapp_firebase"), data=json.dumps({"id_token": "t"}),
                content_type="application/json",
            )

    def test_page_asks_for_the_number_again(self):
        response = self.client.get(reverse("verify_whatsapp"))
        self.assertContains(response, "doesn't look right")
        self.assertContains(response, 'id="phone"')

    def test_verified_correct_number_replaces_the_typo(self):
        response = self._verify("+919074492286")
        self.assertEqual(response.status_code, 200)
        self.saloon.refresh_from_db()
        self.assertEqual(self.saloon.whatsapp_number, "+919074492286")

    def test_cannot_take_a_number_another_salon_uses(self):
        other = User.objects.create_user(username="other", email="o@example.com", password="pass12345")
        Saloon.objects.create(owner=other, whatsapp_number="+919074492286", is_whatsapp_verified=True)
        response = self._verify("+919074492286")
        self.assertEqual(response.status_code, 400)
        self.saloon.refresh_from_db()
        self.assertEqual(self.saloon.whatsapp_number, "+9190874492286")

    def test_valid_saved_number_is_never_replaced(self):
        Saloon.objects.filter(pk=self.saloon.pk).update(whatsapp_number="+919074492286")
        response = self._verify("+919999999999")
        self.assertEqual(response.status_code, 400)
        self.saloon.refresh_from_db()
        self.assertEqual(self.saloon.whatsapp_number, "+919074492286")
