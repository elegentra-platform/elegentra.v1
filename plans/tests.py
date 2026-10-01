import base64
import hashlib
import hmac
import json
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from saloons.models import Saloon, TrustedDevice
from .billing import assign_subscription_trial_on_approval
from .models import SaloonSubscription, SubscriptionPaymentHistory, SubscriptionWebhookEvent


class BillingTrialTests(TestCase):
    def _saloon(self, index):
        user = get_user_model().objects.create_user(
            username=f"owner{index}",
            email=f"owner{index}@example.com",
            password="pass12345",
        )
        return Saloon.objects.create(
            owner=user,
            name=f"Salon {index}",
            whatsapp_number=f"919000000{index:03d}",
            approval_status=Saloon.APPROVAL_APPROVED,
            is_active=True,
            is_whatsapp_verified=True,
            registration_step=5,
        )

    def test_first_approved_salon_gets_permanent_founder_trial(self):
        subscription, _ = assign_subscription_trial_on_approval(self._saloon(1))

        self.assertTrue(subscription.is_founder_partner)
        self.assertEqual(subscription.founder_partner_number, 1)
        self.assertEqual(subscription.approved_trial_days, 90)
        self.assertIsNotNone(subscription.trial_assigned_at)
        self.assertEqual(subscription.status, SaloonSubscription.STATUS_TRIALING)

    def test_salons_after_first_50_get_standard_trial(self):
        for index in range(1, 51):
            assign_subscription_trial_on_approval(self._saloon(index))

        subscription, _ = assign_subscription_trial_on_approval(self._saloon(51))

        self.assertFalse(subscription.is_founder_partner)
        self.assertIsNone(subscription.founder_partner_number)
        self.assertEqual(subscription.approved_trial_days, 14)

    def test_trial_assignment_is_not_recalculated(self):
        saloon = self._saloon(1)
        first, _ = assign_subscription_trial_on_approval(saloon)
        original_ends_at = first.trial_ends_at

        second, _ = assign_subscription_trial_on_approval(saloon)

        self.assertEqual(second.id, first.id)
        self.assertEqual(second.trial_ends_at, original_ends_at)
        self.assertTrue(second.is_founder_partner)


class RazorpaySubscriptionCheckoutTests(TestCase):
    def setUp(self):
        os.environ["RAZORPAY_KEY_ID"] = "rzp_test_unit"
        os.environ["RAZORPAY_KEY_SECRET"] = "unit_secret"
        os.environ["RAZORPAY_SUBSCRIPTION_PLAN_ID"] = "plan_TSNahl35IvIB4c"
        self.client = Client()
        self.user = get_user_model().objects.create_user(
            username="checkoutowner",
            email="checkoutowner@example.com",
            password="pass12345",
        )
        self.saloon = Saloon.objects.create(
            owner=self.user,
            name="Checkout Salon",
            whatsapp_number="919876540000",
            approval_status=Saloon.APPROVAL_APPROVED,
            is_active=True,
            is_whatsapp_verified=True,
            registration_step=5,
        )
        self.subscription, self.plan = assign_subscription_trial_on_approval(self.saloon)
        self.device = TrustedDevice.objects.create(user=self.user)
        self.client.login(username="checkoutowner", password="pass12345")
        self.client.cookies["salon_device"] = str(self.device.device_token)

    def _create_url(self):
        return reverse("saloon_dashboard_subscription_recurring_create", kwargs={"username": self.user.username})

    def _verify_url(self):
        return reverse("saloon_dashboard_subscription_recurring_verify", kwargs={"username": self.user.username})

    def test_first_activation_creates_razorpay_subscription(self):
        response_payload = {
            "id": "sub_unit_created",
            "status": "created",
            "plan_id": "plan_TSNahl35IvIB4c",
        }
        with patch("saloons.dashboard_views._razorpay_api_request", return_value=response_payload) as api_request:
            response = self.client.post(self._create_url())

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["subscription_id"], "sub_unit_created")
        api_request.assert_called_once()
        self.assertEqual(api_request.call_args.args[0], "POST")
        self.assertEqual(api_request.call_args.args[1], "/v1/subscriptions")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.razorpay_subscription_id, "sub_unit_created")
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_PENDING_AUTH)

    def test_successful_verification_sets_autopay_and_authenticated_subscription_id(self):
        self.subscription.razorpay_subscription_id = "sub_unit_authenticated"
        self.subscription.status = SaloonSubscription.STATUS_PENDING_AUTH
        self.subscription.save(update_fields=["razorpay_subscription_id", "status", "updated_at"])

        with patch("saloons.dashboard_views._verify_subscription_signature", return_value=True):
            response = self.client.post(self._verify_url(), data={
                "razorpay_payment_id": "pay_unit_auth",
                "razorpay_subscription_id": "sub_unit_authenticated",
                "razorpay_signature": "valid",
            })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.razorpay_subscription_id, "sub_unit_authenticated")
        self.assertEqual(self.subscription.auth_payment_id, "pay_unit_auth")
        self.assertIsNotNone(self.subscription.autopay_confirmed_at)
        self.assertEqual(self.subscription.razorpay_status, "authenticated")
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_TRIALING)

    def test_subsequent_activation_does_not_create_or_overwrite_authenticated_subscription(self):
        self.subscription.razorpay_subscription_id = "sub_unit_authenticated"
        self.subscription.razorpay_status = "authenticated"
        self.subscription.auth_payment_id = "pay_unit_auth"
        self.subscription.last_payment_id = "pay_unit_auth"
        self.subscription.last_payment_status = "SUCCESS"
        self.subscription.autopay_confirmed_at = timezone.now()
        self.subscription.status = SaloonSubscription.STATUS_TRIALING
        self.subscription.save(update_fields=[
            "razorpay_subscription_id",
            "razorpay_status",
            "auth_payment_id",
            "last_payment_id",
            "last_payment_status",
            "autopay_confirmed_at",
            "status",
            "updated_at",
        ])

        with patch("saloons.dashboard_views._razorpay_api_request") as api_request:
            response = self.client.post(self._create_url())

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["already_active"])
        api_request.assert_not_called()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.razorpay_subscription_id, "sub_unit_authenticated")
        self.assertEqual(self.subscription.razorpay_status, "authenticated")
        self.assertIsNotNone(self.subscription.autopay_confirmed_at)

    def test_one_time_standard_checkout_create_order_is_unaffected(self):
        response_payload = {
            "id": "order_unit_123",
            "amount": 69900,
            "currency": "INR",
        }
        with patch("saloons.dashboard_views._razorpay_api_request", return_value=response_payload) as api_request:
            response = self.client.post(
                reverse("saloon_dashboard_subscription_create", kwargs={"username": self.user.username})
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["order_id"], "order_unit_123")
        api_request.assert_called_once()
        self.assertEqual(api_request.call_args.args[1], "/v1/orders")

class RazorpayWebhookTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.secret = "test-webhook-secret"
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = self.secret
        user = get_user_model().objects.create_user(
            username="webhookowner",
            email="webhookowner@example.com",
            password="pass12345",
        )
        self.saloon = Saloon.objects.create(
            owner=user,
            name="Webhook Salon",
            whatsapp_number="919876543210",
            approval_status=Saloon.APPROVAL_APPROVED,
            is_active=True,
            is_whatsapp_verified=True,
            registration_step=5,
        )
        self.subscription, _ = assign_subscription_trial_on_approval(self.saloon)
        self.subscription.razorpay_subscription_id = "sub_test_123"
        self.subscription.trial_ends_at = timezone.now() - timedelta(days=1)
        self.subscription.status = SaloonSubscription.STATUS_PENDING_AUTH
        self.subscription.save(update_fields=["razorpay_subscription_id", "trial_ends_at", "status", "updated_at"])

    def _signed_headers(self, body):
        signature = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return {"HTTP_X_RAZORPAY_SIGNATURE": signature}

    def _payload(self, status="active", event="subscription.activated"):
        now = timezone.now()
        return {
            "event": event,
            "payload": {
                "subscription": {
                    "entity": {
                        "id": "sub_test_123",
                        "status": status,
                        "current_start": int(now.timestamp()),
                        "current_end": int((now + timedelta(days=30)).timestamp()),
                    }
                }
            },
        }

    def test_valid_webhook_activates_subscription_and_logs_event(self):
        body = json.dumps(self._payload()).encode("utf-8")

        response = self.client.post(
            reverse("razorpay_subscription_webhook"),
            data=body,
            content_type="application/json",
            **self._signed_headers(body),
        )

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_ACTIVE)
        self.assertEqual(self.subscription.razorpay_status, "active")
        self.assertEqual(SubscriptionWebhookEvent.objects.filter(razorpay_subscription_id="sub_test_123").count(), 1)

    def test_invalid_signature_is_rejected(self):
        body = json.dumps(self._payload()).encode("utf-8")

        response = self.client.post(
            reverse("razorpay_subscription_webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="bad-signature",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(SubscriptionWebhookEvent.objects.count(), 0)

    def test_cancelled_webhook_marks_subscription_cancelled(self):
        body = json.dumps(self._payload(status="cancelled", event="subscription.cancelled")).encode("utf-8")

        response = self.client.post(
            reverse("razorpay_subscription_webhook"),
            data=body,
            content_type="application/json",
            **self._signed_headers(body),
        )

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_CANCELLED)
        self.assertIsNotNone(self.subscription.cancelled_at)