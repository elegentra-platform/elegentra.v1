from django.contrib import admin
from .models import Service, ServiceCategory, MainCategory


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "main_category", "is_approved", "is_active", "created_by_saloon", "created_at")
    list_editable = ("main_category", "is_approved", "is_active")
    list_filter = ("is_approved", "is_active", "main_category", "created_by_saloon")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("name",)
    actions = ("approve_categories", "disapprove_categories")

    @admin.action(description="Approve selected categories")
    def approve_categories(self, request, queryset):
        queryset.update(is_approved=True)

    @admin.action(description="Disapprove selected categories")
    def disapprove_categories(self, request, queryset):
        queryset.update(is_approved=False)


@admin.register(MainCategory)
class MainCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "icon_image", "display_order", "is_active")
    list_editable = ("icon_image", "display_order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("display_order", "name")


