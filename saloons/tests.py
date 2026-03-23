from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

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

    @patch("saloons.views.send_otp_whatsapp")
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

    @patch("saloons.views.send_otp_whatsapp")
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

    @patch("saloons.views.send_otp_whatsapp")
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
