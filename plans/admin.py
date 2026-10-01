from django.contrib import admin

from .models import ListingSubscriptionPlan, SaloonSubscription, SubscriptionPaymentHistory, SubscriptionWebhookEvent


@admin.register(ListingSubscriptionPlan)
class ListingSubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ("name", "amount", "strike_amount", "billing_period", "trial_days", "is_active")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(SaloonSubscription)
class SaloonSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("saloon", "plan", "status", "is_founder_partner", "approved_trial_days", "trial_ends_at", "razorpay_subscription_id", "updated_at")
    list_filter = ("status", "plan", "is_founder_partner")
    search_fields = ("saloon__name", "saloon__owner__username", "razorpay_subscription_id")


@admin.register(SubscriptionPaymentHistory)
class SubscriptionPaymentHistoryAdmin(admin.ModelAdmin):
    list_display = ("saloon", "plan", "payment_status", "amount", "payment_method", "paid_at", "razorpay_payment_id")
    list_filter = ("payment_status", "plan")
    search_fields = ("saloon__name", "razorpay_payment_id", "razorpay_subscription_id", "transaction_reference")


@admin.register(SubscriptionWebhookEvent)
class SubscriptionWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_name", "razorpay_subscription_id", "verification_status", "processing_status", "created_at")
    list_filter = ("verification_status", "processing_status", "event_name")
    search_fields = ("event_name", "razorpay_subscription_id", "idempotency_key")
    readonly_fields = ("payload", "webhook_signature", "webhook_timestamp", "error_message")
