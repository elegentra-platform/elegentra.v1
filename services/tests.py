from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from saloons.models import Saloon
from .models import MainCategory, Service, ServiceCategory


class ServiceCategorySelectionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="test-pass-123")
        other_owner = User.objects.create_user(username="other", password="test-pass-123")
        self.saloon = Saloon.objects.create(
            owner=self.owner,
            name="Owner Salon",
            whatsapp_number="+919000000001",
            approval_status=Saloon.APPROVAL_APPROVED,
        )
        other_saloon = Saloon.objects.create(
            owner=other_owner,
            name="Other Salon",
            whatsapp_number="+919000000002",
            approval_status=Saloon.APPROVAL_APPROVED,
        )
        self.main = MainCategory.objects.create(name="Haircut & Styling")
        self.approved = ServiceCategory.objects.create(
            name="Haircut",
            main_category=self.main,
            is_approved=True,
        )
        self.own_pending = ServiceCategory.objects.create(
            name="Beard Shape",
            created_by_saloon=self.saloon,
        )
        self.other_pending = ServiceCategory.objects.create(
            name="Other Raw Submission",
            created_by_saloon=other_saloon,
        )

    def test_add_form_groups_main_subcategory_and_only_own_pending(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("service_create"))

        self.assertContains(response, "Haircut &amp; Styling")
        self.assertNotContains(response, "(main category)")
        self.assertContains(response, "— Haircut")
        self.assertContains(response, "Beard Shape")
        self.assertNotContains(response, "Other Raw Submission")

    def test_service_can_use_main_category_directly(self):
        service = Service.objects.create(
            saloon=self.saloon,
            main_category=self.main,
            name="General styling",
            price="500.00",
        )

        self.assertIsNone(service.category)
        self.assertEqual(service.category_label, "Haircut & Styling")


class StaffCategoryAndServiceReviewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_superuser(
            username="admin-reviewer",
            email="admin@example.com",
            password="test-pass-123",
        )
        owner = User.objects.create_user(username="review-owner", password="test-pass-123")
        self.saloon = Saloon.objects.create(
            owner=owner,
            name="Review Salon",
            whatsapp_number="+919000000003",
            approval_status=Saloon.APPROVAL_APPROVED,
        )
        self.main = MainCategory.objects.create(name="Spa & Massage")
        self.category = ServiceCategory.objects.create(
            name="Masage",
            main_category=self.main,
            is_approved=True,
        )
        self.service = Service.objects.create(
            saloon=self.saloon,
            category=self.category,
            name="Bad servise name",
            price="600.00",
            is_active=True,
            is_visible=True,
        )
        self.client.force_login(self.staff)

    def test_staff_can_edit_an_already_mapped_category(self):
        response = self.client.post(
            reverse("staffpanel:category_update", args=[self.category.id]),
            {
                "category_name": "Massage",
                "main_category_id": str(self.main.id),
                "is_approved": "on",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("staffpanel:category_queue"))
        self.category.refresh_from_db()
        self.assertEqual(self.category.name, "Massage")
        self.assertTrue(self.category.is_approved)

    def test_category_page_has_main_category_filter(self):
        response = self.client.get(reverse("staffpanel:category_queue"))

        self.assertContains(response, 'id="categoryMainFilter"')
        self.assertContains(response, "All main categories")
        self.assertContains(response, "Spa &amp; Massage")

    def test_staff_can_correct_and_remap_a_service(self):
        response = self.client.post(
            reverse("staffpanel:service_review_update", args=[self.service.id]),
            {
                "service_name": "Relaxing treatment",
                "category_choice": f"main:{self.main.id}",
                "is_active": "on",
                "is_visible": "on",
            },
        )

        self.assertRedirects(response, reverse("staffpanel:category_queue"))
        self.service.refresh_from_db()
        self.assertEqual(self.service.name, "Relaxing treatment")
        self.assertEqual(self.service.main_category, self.main)
        self.assertIsNone(self.service.category)

    def test_deleting_used_category_disables_it_safely(self):
        response = self.client.post(reverse("staffpanel:category_delete", args=[self.category.id]))

        self.assertRedirects(response, reverse("staffpanel:category_queue"))
        self.category.refresh_from_db()
        self.assertFalse(self.category.is_active)
        self.assertFalse(self.category.is_approved)
