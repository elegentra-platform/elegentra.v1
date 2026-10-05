from decimal import Decimal

from django.db import migrations


def seed_default_listing_plan(apps, schema_editor):
    ListingSubscriptionPlan = apps.get_model("plans", "ListingSubscriptionPlan")
    ListingSubscriptionPlan.objects.update_or_create(
        slug="elegentra-monthly",
        defaults={
            "name": "Elegentra Listing",
            "description": "Keep your salon live on Elegentra with discovery placement, gallery, and premium support.",
            "amount": Decimal("699.00"),
            "strike_amount": Decimal("999.00"),
            "billing_period": "monthly",
            "interval": 1,
            "trial_days": 14,
            "currency": "INR",
            "razorpay_plan_id": "plan_TSNahl35IvIB4c",
            "is_active": True,
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("plans", "0002_razorpay_subscription_history"),
    ]

    operations = [
        migrations.RunPython(seed_default_listing_plan, migrations.RunPython.noop),
    ]
