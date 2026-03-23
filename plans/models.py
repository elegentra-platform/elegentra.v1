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
    amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("499.00"))
    strike_amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("999.00"))
    billing_period = models.CharField(max_length=20, choices=BILLING_PERIOD_CHOICES, default="monthly")
    interval = models.PositiveSmallIntegerField(default=1)
    trial_days = models.PositiveSmallIntegerField(default=30)
    currency = models.CharField(max_length=8, default="INR")
    razorpay_plan_id = models.CharField(max_length=80, blank=True)
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
    razorpay_plan_id = models.CharField(max_length=80, blank=True)
    razorpay_subscription_id = models.CharField(max_length=80, blank=True)
    razorpay_customer_id = models.CharField(max_length=80, blank=True)
    razorpay_status = models.CharField(max_length=40, blank=True)
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
    event_name = models.CharField(max_length=120)
    razorpay_subscription_id = models.CharField(max_length=80, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.event_name
