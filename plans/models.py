from decimal import Decimal

from django.db import models
from django.utils import timezone

from saloons.models import Saloon


class ListingSubscriptionPlan(models.Model):
    BILLING_PERIOD_CHOICES = [
        ("monthly", "Monthly"),
        ("yearly", "Yearly"),
    ]

    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("699.00"))
    strike_amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("999.00"))
    billing_period = models.CharField(max_length=20, choices=BILLING_PERIOD_CHOICES, default="monthly")
    interval = models.PositiveSmallIntegerField(default=1)
    trial_days = models.PositiveSmallIntegerField(default=14)
    currency = models.CharField(max_length=8, default="INR")
    razorpay_plan_id = models.CharField(max_length=250, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.name} - {self.currency} {self.amount}"

    @property
    def amount_paise(self):
        return int(self.amount * 100)

    @property
    def strike_amount_paise(self):
        return int(self.strike_amount * 100)


class SaloonSubscription(models.Model):
    STATUS_TRIALING = "trialing"
    STATUS_PENDING_AUTH = "pending_auth"
    STATUS_ACTIVE = "active"
    STATUS_PAST_DUE = "past_due"
    STATUS_CANCELLED = "cancelled"
    STATUS_EXPIRED = "expired"

    STATUS_CHOICES = [
        (STATUS_TRIALING, "Trial Active"),
        (STATUS_PENDING_AUTH, "Autopay Pending"),
        (STATUS_ACTIVE, "Autopay Active"),
        (STATUS_PAST_DUE, "Payment Due"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_EXPIRED, "Expired"),
    ]

    saloon = models.ForeignKey(Saloon, on_delete=models.CASCADE, related_name="subscriptions")
    plan = models.ForeignKey(ListingSubscriptionPlan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_TRIALING)
    trial_started_at = models.DateTimeField(default=timezone.now)
    trial_ends_at = models.DateTimeField()
    autopay_confirmed_at = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    is_founder_partner = models.BooleanField(default=False)
    founder_partner_number = models.PositiveSmallIntegerField(null=True, blank=True)
    approved_trial_days = models.PositiveSmallIntegerField(default=14)
    trial_assigned_at = models.DateTimeField(null=True, blank=True)
    grace_period_started_at = models.DateTimeField(null=True, blank=True)
    grace_period_ends_at = models.DateTimeField(null=True, blank=True)
    razorpay_plan_id = models.CharField(max_length=250, blank=True)
    razorpay_subscription_id = models.CharField(max_length=250, blank=True)
    razorpay_customer_id = models.CharField(max_length=512, blank=True)
    razorpay_status = models.CharField(max_length=80, blank=True)
    auth_payment_id = models.CharField(max_length=80, blank=True)
    last_payment_id = models.CharField(max_length=80, blank=True)
    last_payment_status = models.CharField(max_length=40, blank=True)
    cancel_at_cycle_end = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    notes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["saloon", "status"]),
            models.Index(fields=["razorpay_subscription_id"]),
        ]

    def __str__(self):
        return f"{self.saloon.name} - {self.get_status_display()}"

    @property
    def trial_days_left(self):
        remaining = self.trial_ends_at - timezone.now()
        return max(0, remaining.days + (1 if remaining.seconds > 0 else 0))


class SubscriptionWebhookEvent(models.Model):
    STATUS_PENDING = "pending"
    STATUS_PROCESSED = "processed"
    STATUS_DUPLICATE = "duplicate"
    STATUS_FAILED = "failed"
    STATUS_REJECTED = "rejected"

    PROCESSING_STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSED, "Processed"),
        (STATUS_DUPLICATE, "Duplicate"),
        (STATUS_FAILED, "Failed"),
        (STATUS_REJECTED, "Rejected"),
    ]

    event_name = models.CharField(max_length=120)
    razorpay_subscription_id = models.CharField(max_length=250, blank=True)
    idempotency_key = models.CharField(max_length=250, blank=True, unique=True, null=True)
    webhook_signature = models.CharField(max_length=512, blank=True)
    webhook_timestamp = models.CharField(max_length=80, blank=True)
    verification_status = models.CharField(max_length=40, default="unverified")
    processing_status = models.CharField(max_length=24, choices=PROCESSING_STATUS_CHOICES, default=STATUS_PENDING)
    error_message = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["event_name", "processing_status"]),
            models.Index(fields=["razorpay_subscription_id"]),
        ]

    def __str__(self):
        return self.event_name


class SubscriptionPaymentHistory(models.Model):
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_PENDING = "pending"

    STATUS_CHOICES = [
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_PENDING, "Pending"),
    ]

    subscription = models.ForeignKey(SaloonSubscription, on_delete=models.CASCADE, related_name="payment_history")
    saloon = models.ForeignKey(Saloon, on_delete=models.CASCADE, related_name="subscription_payments")
    plan = models.ForeignKey(ListingSubscriptionPlan, on_delete=models.PROTECT, related_name="payment_history")
    paid_at = models.DateTimeField(default=timezone.now)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=8, default="INR")
    payment_status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    payment_method = models.CharField(max_length=120, blank=True)
    renewal_type = models.CharField(max_length=80, blank=True)
    razorpay_payment_id = models.CharField(max_length=250, blank=True)
    razorpay_subscription_id = models.CharField(max_length=250, blank=True)
    transaction_reference = models.CharField(max_length=250, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_at", "-id"]
        indexes = [
            models.Index(fields=["saloon", "payment_status"]),
            models.Index(fields=["razorpay_subscription_id"]),
            models.Index(fields=["razorpay_payment_id"]),
        ]

    def __str__(self):
        return f"{self.saloon.name} - {self.payment_status} - {self.amount}"
