from django.contrib import admin

from .models import ListingSubscriptionPlan, SaloonSubscription, SubscriptionWebhookEvent


@admin.register(ListingSubscriptionPlan)
class ListingSubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ("name", "amount", "strike_amount", "billing_period", "trial_days", "is_active")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(SaloonSubscription)
class SaloonSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("saloon", "plan", "status", "trial_ends_at", "razorpay_subscription_id", "updated_at")
    list_filter = ("status", "plan")
    search_fields = ("saloon__name", "saloon__owner__username", "razorpay_subscription_id")


@admin.register(SubscriptionWebhookEvent)
class SubscriptionWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_name", "razorpay_subscription_id", "created_at")
    search_fields = ("event_name", "razorpay_subscription_id")
