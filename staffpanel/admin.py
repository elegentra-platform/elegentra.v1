from django.contrib import admin

from .models import StaffProfile


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "status", "last_code_login_at", "created_at")
    list_filter = ("role", "status")
    search_fields = ("user__email", "user__username", "user__first_name", "user__last_name")
    readonly_fields = ("invite_code_hash", "invite_code_display", "invite_code_created_at", "last_code_login_at", "created_at", "updated_at")
