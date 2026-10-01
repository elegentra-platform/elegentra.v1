from django.db import models
from django.utils.text import slugify
from saloons.models import Saloon


# =========================
# SERVICE CATEGORY (GLOBAL)
# =========================

class ServiceCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True, blank=True)

    is_approved = models.BooleanField(
        default=False,
        help_text="Only approved categories appear to buyers"
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Inactive categories won't appear in suggestions"
    )

    created_by_saloon = models.ForeignKey(
        Saloon,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_service_categories"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    main_category = models.ForeignKey(
        "MainCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_categories",
        help_text="Mapped buyer-facing category",
    )

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            count = 1
            while ServiceCategory.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{count}"
                count += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class MainCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    icon_image = models.ImageField(
        upload_to="categories/icons/",
        null=True,
        blank=True,
        help_text="Category icon image shown in buyer app circles.",
    )
    display_order = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["display_order", "name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            count = 1
            while MainCategory.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{count}"
                count += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


# =========================
# SERVICE (CORE)
# =========================

class Service(models.Model):

    saloon = models.ForeignKey(
        Saloon,
        on_delete=models.CASCADE,
        related_name="services"
    )

    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.PROTECT,
        related_name="services"
    )

    name = models.CharField(max_length=255)

    description = models.TextField(blank=True)

    price = models.DecimalField(
        max_digits=8,
        decimal_places=2
    )

    offer_price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Optional discounted price"
    )

    image = models.ImageField(
        upload_to="services/images/",
        null=True,
        blank=True,
    )

    # 🔹 SOFT DELETE (ADDED – SAFE)
    is_deleted = models.BooleanField(
        default=False,
        help_text="Soft delete for undo & recovery"
    )
    deleted_at = models.DateTimeField(
        null=True,
        blank=True
    )

    # Salon control
    is_visible = models.BooleanField(
        default=True,
        help_text="Temporarily hide service from public view"
    )

    # Platform control
    is_active = models.BooleanField(
        default=False,
        help_text="Service becomes active after saloon approval"
    )

    # Promotion
    is_promoted = models.BooleanField(
        default=False,
        help_text="Boost service visibility"
    )

    # Flow tracking
    added_during_onboarding = models.BooleanField(
        default=False
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["category"]),
            models.Index(fields=["is_promoted"]),
            models.Index(fields=["is_visible"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["is_deleted"]),  # 🔹 important
        ]

    def __str__(self):
        return f"{self.name} – {self.saloon.name}"


class ServiceInterest(models.Model):
    service = models.ForeignKey(
        Service,
        on_delete=models.CASCADE,
        related_name="interest_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["service", "created_at"]),
        ]

    def __str__(self):
        return f"{self.service.name} interest"

