from django.contrib import admin

from .models import (
    Saloon,
    SaloonProfile,
    SaloonVerification,
    SaloonRejectionReason,
    TrustedDevice,
)

from services.models import Service


# ==================================================
# INLINES
# ==================================================

class SaloonProfileInline(admin.StackedInline):
    model = SaloonProfile
    extra = 0
    can_delete = False


class SaloonVerificationInline(admin.StackedInline):
    model = SaloonVerification
    extra = 0
    can_delete = False


class ServiceInline(admin.TabularInline):
    """
    Used ONLY for onboarding review
    Admin can see first 3 services added during onboarding
    """
    model = Service
    extra = 0
    max_num = 3
    fields = (
        "name",
        "category",
        "price",
        "offer_price",
        "image",
        "is_active",
        "is_visible",
    )
    readonly_fields = ("created_at",)


class SaloonRejectionInline(admin.TabularInline):
    model = SaloonRejectionReason
    extra = 1


# ==================================================
# MANAGE APPROVALS (MAIN ADMIN)
# ==================================================

@admin.register(Saloon)
class ManageApprovalAdmin(admin.ModelAdmin):

    list_display = (
        "name",
        "owner",
        "approval_status",
        "registration_step",
        "is_active",
    )

    list_filter = (
        "approval_status",
        "registration_step",
        "is_active",
    )

    search_fields = (
        "name",
        "owner__email",
        "whatsapp_number",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Owner", {
            "fields": ("owner",)
        }),
        ("Salon Details", {
            "fields": (
                "name",
                "slug",
                "whatsapp_number",
            )
        }),
        ("Approval Control", {
            "fields": (
                "approval_status",
                "registration_step",
                "is_active",
                "is_whatsapp_verified",
            )
        }),
        ("Public Branding – Step 3", {
            "fields": ("banner_image",)
        }),
        ("System", {
            "fields": (
                "created_at",
                "updated_at",
            )
        }),
    )

    inlines = [
        SaloonProfileInline,
        SaloonVerificationInline,
        ServiceInline,          # onboarding services preview
        SaloonRejectionInline,
    ]

    # 🔥 THIS IS THE MISSING BRIDGE
    def save_model(self, request, obj, form, change):
        """
        When a saloon is approved for the first time,
        activate services added during onboarding.
        """
        was_approved = False

        if change:
            previous = Saloon.objects.get(pk=obj.pk)
            was_approved = previous.approval_status == Saloon.APPROVAL_APPROVED

        super().save_model(request, obj, form, change)

        # JUST APPROVED → ACTIVATE ONBOARDING SERVICES
        if (
            obj.approval_status == Saloon.APPROVAL_APPROVED
            and not was_approved
        ):
            Service.objects.filter(
                saloon=obj,
                added_during_onboarding=True
            ).update(
                added_during_onboarding=False,
                is_active=True
            )

    def delete_queryset(self, request, queryset):
        """
        Explicit cascade safety.
        Deletes services before saloon to avoid permission errors.
        """
        for saloon in queryset:
            saloon.services.all().delete()
            saloon.delete()



# ==================================================
# SERVICES ADMIN (FULL CONTROL)
# ==================================================

@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):

    list_display = (
        "name",
        "saloon",
        "category",
        "price",
        "is_visible",
        "is_active",
        "is_promoted",
    )

    list_filter = (
        "category",
        "is_active",
        "is_visible",
        "is_promoted",
    )

    search_fields = (
        "name",
        "saloon__name",
        "saloon__owner__email",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return True

    def has_delete_permission(self, request, obj=None):
        return True


# ==================================================
# TRUSTED DEVICES
# ==================================================

@admin.register(TrustedDevice)
class TrustedDeviceAdmin(admin.ModelAdmin):

    list_display = (
        "user",
        "device_token",
        "last_used_at",
    )

    readonly_fields = (
        "device_token",
        "created_at",
        "last_used_at",
    )
