from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
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
    def test_falls_back_to_sms_when_whatsapp_otp_unavailable(self, send_otp_mock, send_sms_mock):
        from django.core import mail

        response = self.client.post(self.url, {"send_otp": "1"})

        send_sms_mock.assert_called_once_with(self.saloon.whatsapp_number, int(self.client.session["vendor_otp"]))
        self.assertEqual(self.client.session["vendor_otp_channel"], "sms")
        self.assertEqual(len(mail.outbox), 0)
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
