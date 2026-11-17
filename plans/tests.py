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

    def test_first_approved_salon_gets_founder_trial_and_price(self):
        subscription, plan = assign_subscription_trial_on_approval(self._saloon(1))

        self.assertTrue(subscription.is_founder_partner)
        self.assertEqual(subscription.founder_partner_number, 1)
        self.assertEqual(subscription.approved_trial_days, 30)
        self.assertIsNotNone(subscription.trial_assigned_at)
        self.assertEqual(subscription.status, SaloonSubscription.STATUS_TRIALING)
        self.assertEqual(str(plan.amount), "499.00")
        self.assertEqual(subscription.plan_id, plan.id)

    def test_salons_after_first_25_get_standard_trial_and_price(self):
        for index in range(1, 26):
            assign_subscription_trial_on_approval(self._saloon(index))

        subscription, plan = assign_subscription_trial_on_approval(self._saloon(26))

        self.assertFalse(subscription.is_founder_partner)
        self.assertIsNone(subscription.founder_partner_number)
        self.assertEqual(subscription.approved_trial_days, 14)
        self.assertEqual(str(plan.amount), "699.00")

    def test_page_load_does_not_reset_founder_to_standard_price(self):
        from .billing import get_or_create_saloon_subscription

        saloon = self._saloon(1)
        assign_subscription_trial_on_approval(saloon)

        subscription, plan = get_or_create_saloon_subscription(saloon)

        self.assertEqual(str(plan.amount), "499.00")
        self.assertEqual(str(subscription.plan.amount), "499.00")

    def test_live_mandate_keeps_its_plan(self):
        from .billing import get_default_subscription_plan, get_or_create_saloon_subscription

        saloon = self._saloon(1)
        subscription, _ = assign_subscription_trial_on_approval(saloon)
        # e.g. an older founder whose autopay was approved on the ₹699 plan
        subscription.plan = get_default_subscription_plan()
        subscription.razorpay_subscription_id = "sub_old_plan"
        subscription.autopay_confirmed_at = timezone.now()
        subscription.save()

        _, plan = get_or_create_saloon_subscription(saloon)

        self.assertEqual(str(plan.amount), "699.00")

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
        os.environ["RAZORPAY_FOUNDER_PLAN_ID"] = "plan_founder_unit"
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

    def _make_lapsed(self):
        """Paid once, trial over, period ended without a renewal reaching us."""
        now = timezone.now()
        self.subscription.razorpay_subscription_id = "sub_unit_old"
        self.subscription.razorpay_status = "authenticated"
        self.subscription.autopay_confirmed_at = now - timedelta(days=45)
        self.subscription.trial_ends_at = now - timedelta(days=46)
        self.subscription.current_period_start = now - timedelta(days=45)
        self.subscription.current_period_end = now - timedelta(days=15)
        self.subscription.status = SaloonSubscription.STATUS_EXPIRED
        self.subscription.save()

    def _subscription_url(self):
        return reverse("saloon_dashboard_subscription", kwargs={"username": self.user.username})

    def test_expired_subscription_page_shows_expired_not_autopay_active(self):
        self._make_lapsed()

        response = self.client.get(self._subscription_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["billing_state"], "expired")
        self.assertFalse(response.context["autopay_ready"])
        content = response.content.decode()
        self.assertIn("Your subscription has expired.", content)
        self.assertIn("Renew Subscription", content)
        self.assertNotIn("Autopay Active", content)

    def test_renewal_recovers_when_razorpay_shows_paid_period(self):
        self._make_lapsed()
        future_end = int((timezone.now() + timedelta(days=20)).timestamp())
        past_start = int((timezone.now() - timedelta(days=10)).timestamp())
        remote = {"id": "sub_unit_old", "status": "active", "current_start": past_start, "current_end": future_end}

        with patch("saloons.dashboard_views._razorpay_api_request", return_value=remote) as api_request:
            response = self.client.post(self._create_url())

        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["already_active"])
        api_request.assert_called_once_with("GET", "/v1/subscriptions/sub_unit_old")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_ACTIVE)
        self.assertGreater(self.subscription.current_period_end, timezone.now())

    def test_renewal_cancels_old_mandate_before_creating_new_one(self):
        self._make_lapsed()
        calls = []

        def fake_api(method, path, payload=None):
            calls.append((method, path))
            if method == "GET":
                return {"id": "sub_unit_old", "status": "halted"}
            if path.endswith("/cancel"):
                return {"id": "sub_unit_old", "status": "cancelled"}
            return {"id": "sub_unit_new", "status": "created"}

        with patch("saloons.dashboard_views._razorpay_api_request", side_effect=fake_api), \
                patch("saloons.dashboard_views._ensure_remote_plan", return_value="plan_TSNahl35IvIB4c"):
            response = self.client.post(self._create_url())

        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["subscription_id"], "sub_unit_new")
        self.assertEqual(
            calls,
            [
                ("GET", "/v1/subscriptions/sub_unit_old"),
                ("POST", "/v1/subscriptions/sub_unit_old/cancel"),
                ("POST", "/v1/subscriptions"),
            ],
        )
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.razorpay_subscription_id, "sub_unit_new")
        self.assertEqual(self.subscription.notes.get("replaced_razorpay_subscription_id"), "sub_unit_old")

    def test_renewal_does_not_create_new_mandate_when_razorpay_unreachable(self):
        self._make_lapsed()

        with patch("saloons.dashboard_views._razorpay_api_request", side_effect=RuntimeError("down")) as api_request:
            response = self.client.post(self._create_url())

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        api_request.assert_called_once_with("GET", "/v1/subscriptions/sub_unit_old")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.razorpay_subscription_id, "sub_unit_old")

    def test_founder_checkout_uses_founder_plan(self):
        self.assertTrue(self.subscription.is_founder_partner)
        created_plans = []
        subscription_payloads = []

        def fake_api(method, path, payload=None):
            if path == "/v1/plans":
                created_plans.append(payload["item"]["amount"])
                return {"id": "plan_founder_created"}
            subscription_payloads.append(payload)
            return {"id": "sub_unit_founder", "status": "created"}

        # No preconfigured founder plan id: Elegentra creates the ₹499 plan in Razorpay.
        with patch.dict(os.environ, {"RAZORPAY_FOUNDER_PLAN_ID": ""}), \
                patch("saloons.dashboard_views._razorpay_api_request", side_effect=fake_api):
            from .billing import get_founder_subscription_plan

            founder_plan = get_founder_subscription_plan()
            founder_plan.razorpay_plan_id = ""
            founder_plan.save()
            response = self.client.post(self._create_url())

        self.assertTrue(response.json()["ok"])
        self.assertEqual(created_plans, [49900])
        self.assertEqual(subscription_payloads[0]["plan_id"], "plan_founder_created")
        self.subscription.refresh_from_db()
        self.assertEqual(str(self.subscription.plan.amount), "499.00")
        self.assertIn("₹499", response.json()["description"])

    def test_founder_who_paid_then_lapsed_renews_at_standard_price(self):
        self._make_lapsed()
        SubscriptionPaymentHistory.objects.create(
            subscription=self.subscription,
            saloon=self.saloon,
            plan=self.subscription.plan,
            amount="499.00",
            payment_status=SubscriptionPaymentHistory.STATUS_SUCCESS,
            razorpay_payment_id="pay_founder_1",
        )
        subscription_payloads = []

        def fake_api(method, path, payload=None):
            if method == "GET":
                return {"id": "sub_unit_old", "status": "cancelled"}
            subscription_payloads.append(payload)
            return {"id": "sub_unit_new", "status": "created"}

        with patch("saloons.dashboard_views._razorpay_api_request", side_effect=fake_api):
            response = self.client.post(self._create_url())

        self.assertTrue(response.json()["ok"])
        self.subscription.refresh_from_db()
        self.assertEqual(str(self.subscription.plan.amount), "699.00")
        self.assertTrue(self.subscription.notes.get("founder_price_forfeited"))
        self.assertTrue(self.subscription.is_founder_partner)  # badge/number kept
        self.assertEqual(subscription_payloads[0]["plan_id"], "plan_TSNahl35IvIB4c")  # the ₹699 plan

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


class SpotifyStyleAutopayTests(TestCase):
    """Monthly autopay lifecycle: charge, failure + grace, cancel in UPI app, re-enable."""

    def setUp(self):
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = "lifecycle-secret"
        os.environ["RAZORPAY_KEY_ID"] = "rzp_test_unit"
        os.environ["RAZORPAY_KEY_SECRET"] = "unit_secret"
        self.client = Client()
        self.user = get_user_model().objects.create_user(
            username="lifecycleowner", email="lifecycle@example.com", password="pass12345"
        )
        self.saloon = Saloon.objects.create(
            owner=self.user,
            name="Lifecycle Salon",
            whatsapp_number="919876500000",
            approval_status=Saloon.APPROVAL_APPROVED,
            is_active=True,
            is_whatsapp_verified=True,
            registration_step=5,
        )
        self.subscription, _ = assign_subscription_trial_on_approval(self.saloon)
        now = timezone.now()
        # Trial over, autopay on, a paid month running for 20 more days.
        self.subscription.razorpay_subscription_id = "sub_life"
        self.subscription.razorpay_status = "active"
        self.subscription.autopay_confirmed_at = now - timedelta(days=10)
        self.subscription.trial_ends_at = now - timedelta(days=10)
        self.subscription.current_period_start = now - timedelta(days=10)
        self.subscription.current_period_end = now + timedelta(days=20)
        self.subscription.status = SaloonSubscription.STATUS_ACTIVE
        self.subscription.save()

    def _post_event(self, event, status, current_start=None, current_end=None, payment=None, event_id=None):
        entity = {"id": "sub_life", "status": status}
        if current_start:
            entity["current_start"] = int(current_start.timestamp())
        if current_end:
            entity["current_end"] = int(current_end.timestamp())
        body_payload = {"subscription": {"entity": entity}}
        if payment:
            body_payload["payment"] = {"entity": payment}
        body = json.dumps({"event": event, "payload": body_payload}).encode("utf-8")
        signature = hmac.new(b"lifecycle-secret", body, hashlib.sha256).hexdigest()
        headers = {"HTTP_X_RAZORPAY_SIGNATURE": signature}
        if event_id:
            headers["HTTP_X_RAZORPAY_EVENT_ID"] = event_id
        return self.client.post(
            reverse("razorpay_subscription_webhook"), data=body, content_type="application/json", **headers
        )

    def _access(self):
        from .billing import subscription_has_platform_access

        self.subscription.refresh_from_db()
        return subscription_has_platform_access(self.subscription)

    def test_monthly_charge_extends_period_and_records_payment(self):
        new_start = self.subscription.current_period_end
        new_end = new_start + timedelta(days=30)
        payment = {"id": "pay_month2", "amount": 69900, "currency": "INR", "method": "upi", "vpa": "owner@okicici"}

        response = self._post_event("subscription.charged", "active", new_start, new_end, payment)

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_ACTIVE)
        self.assertEqual(int(self.subscription.current_period_end.timestamp()), int(new_end.timestamp()))
        history = SubscriptionPaymentHistory.objects.get(razorpay_payment_id="pay_month2")
        self.assertEqual(history.payment_status, SubscriptionPaymentHistory.STATUS_SUCCESS)
        self.assertEqual(str(history.amount), "699.00")
        self.assertIn("UPI AutoPay owner@okicici", history.payment_method)

    def test_failed_renewal_gives_grace_period_then_locks(self):
        # Paid month has just ended and the renewal charge failed.
        self.subscription.current_period_end = timezone.now() - timedelta(hours=1)
        self.subscription.save()

        self._post_event("subscription.pending", "pending")

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_PAST_DUE)
        self.assertIsNotNone(self.subscription.grace_period_ends_at)
        self.assertTrue(self._access())  # still open during grace

        self.subscription.grace_period_ends_at = timezone.now() - timedelta(minutes=1)
        self.subscription.save()
        self.assertFalse(self._access())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_EXPIRED)

    def test_cancel_in_upi_app_keeps_access_until_paid_period_ends(self):
        self._post_event("subscription.cancelled", "cancelled")

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_CANCELLED)
        self.assertIsNotNone(self.subscription.cancelled_at)
        self.assertTrue(self._access())  # 20 paid days left

        self.subscription.current_period_end = timezone.now() - timedelta(minutes=1)
        self.subscription.save()
        self.assertFalse(self._access())

    def test_cancelled_salon_stays_publicly_visible_until_period_end(self):
        from .billing import live_subscription_q

        self._post_event("subscription.cancelled", "cancelled")
        self.assertTrue(SaloonSubscription.objects.filter(live_subscription_q(), pk=self.subscription.pk).exists())

        SaloonSubscription.objects.filter(pk=self.subscription.pk).update(
            current_period_end=timezone.now() - timedelta(minutes=1)
        )
        self.assertFalse(SaloonSubscription.objects.filter(live_subscription_q(), pk=self.subscription.pk).exists())

    def test_duplicate_webhook_is_processed_once(self):
        payment = {"id": "pay_dup", "amount": 69900, "currency": "INR", "method": "upi"}
        end = self.subscription.current_period_end + timedelta(days=30)

        self._post_event("subscription.charged", "active", None, end, payment, event_id="evt_1")
        self._post_event("subscription.charged", "active", None, end, payment, event_id="evt_1")

        self.assertEqual(SubscriptionWebhookEvent.objects.filter(idempotency_key="evt_1").count(), 1)
        self.assertEqual(SubscriptionPaymentHistory.objects.filter(razorpay_payment_id="pay_dup").count(), 1)

    def test_renewal_delay_does_not_lock_paying_salon(self):
        # Period ended 2 hours ago; the renewal charge webhook has not arrived yet.
        self.subscription.current_period_end = timezone.now() - timedelta(hours=2)
        self.subscription.save()
        self.assertTrue(self._access())

    def _login_dashboard(self):
        device = TrustedDevice.objects.create(user=self.user)
        self.client.login(username="lifecycleowner", password="pass12345")
        self.client.cookies["salon_device"] = str(device.device_token)

    def test_cancel_from_dashboard_stops_mandate_and_keeps_access(self):
        self._login_dashboard()
        url = reverse("saloon_dashboard_subscription_cancel", kwargs={"username": self.user.username})

        with patch("saloons.dashboard_views._razorpay_api_request", return_value={"status": "cancelled"}) as api:
            response = self.client.post(url)

        self.assertTrue(response.json()["ok"])
        api.assert_called_once_with("POST", "/v1/subscriptions/sub_life/cancel", {"cancel_at_cycle_end": 0})
        self.assertTrue(self._access())
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_CANCELLED)

        page = self.client.get(reverse("saloon_dashboard_subscription", kwargs={"username": self.user.username}))
        self.assertEqual(page.context["billing_state"], "cancelled_active")
        self.assertContains(page, "Turn Autopay Back On")
        self.assertNotContains(page, "Autopay Active")

    def test_turning_autopay_back_on_starts_after_paid_period(self):
        self._post_event("subscription.cancelled", "cancelled")
        self._login_dashboard()
        captured = {}

        def fake_api(method, path, payload=None):
            captured[(method, path)] = payload
            return {"id": "sub_life_new", "status": "created"}

        with patch("saloons.dashboard_views._razorpay_api_request", side_effect=fake_api), \
                patch("saloons.dashboard_views._ensure_remote_plan", return_value="plan_x"):
            response = self.client.post(
                reverse("saloon_dashboard_subscription_recurring_create", kwargs={"username": self.user.username})
            )

        self.assertTrue(response.json()["ok"])
        create_payload = captured[("POST", "/v1/subscriptions")]
        self.subscription.refresh_from_db()
        # No charge now: the new mandate starts when the already-paid month ends.
        self.assertEqual(create_payload["start_at"], int(self.subscription.current_period_end.timestamp()))
        # Still shown as cancelled until the new mandate is approved.
        self.assertEqual(self.subscription.status, SaloonSubscription.STATUS_CANCELLED)