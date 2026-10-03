import uuid
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen
from django.conf import settings
from django.db import models
from django.utils.text import slugify
from .utils import normalize_saloon_slug


# =========================
# LOCATION MASTER
# =========================

class State(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            count = 1
            while State.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{count}"
                count += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class District(models.Model):
    state = models.ForeignKey(
        State,
        on_delete=models.CASCADE,
        related_name="districts"
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("state", "name"), ("state", "slug")]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            count = 1
            while District.objects.filter(state=self.state, slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{count}"
                count += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name}, {self.state.name}"


# =========================
# SALOON (CORE)
# =========================

class Saloon(models.Model):

    APPROVAL_SUBMITTING = "Submitting"
    APPROVAL_PENDING = "pending"
    APPROVAL_APPROVED = "approved"
    APPROVAL_REJECTED = "rejected"

    APPROVAL_STATUS_CHOICES = [
        (APPROVAL_SUBMITTING, "Submitting"),
        (APPROVAL_PENDING, "Pending"),
        (APPROVAL_APPROVED, "Approved"),
        (APPROVAL_REJECTED, "Rejected"),
    ]

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saloon"
    )

    name = models.CharField(max_length=255, blank=True)
    slug = models.SlugField(unique=True, blank=True)

    whatsapp_number = models.CharField(max_length=20)
    is_whatsapp_verified = models.BooleanField(default=False)

    # Step 3 – Public branding
    banner_image = models.ImageField(
        upload_to="saloons/banner/",
        null=True,
        blank=True
    )

    is_verified = models.BooleanField(default=False)

    myfestivo_handoff_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_test_saloon = models.BooleanField(
        default=False,
        help_text="Internal launch/test listing. It can appear publicly but never consumes founder partner slots.",
    )
    test_saloon_marked_at = models.DateTimeField(null=True, blank=True)
    test_saloon_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="test_saloons_marked",
    )
    test_saloon_converted_at = models.DateTimeField(null=True, blank=True)
    test_saloon_hidden_at = models.DateTimeField(null=True, blank=True)

    approval_status = models.CharField(
        max_length=20,
        choices=APPROVAL_STATUS_CHOICES,
        default=APPROVAL_SUBMITTING
    )

    registration_step = models.PositiveSmallIntegerField(
        default=1,
        help_text="Current onboarding step"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            base = self.name or self.owner.username
            slug = normalize_saloon_slug(base) or slugify(base)
            unique_slug = slug
            count = 1

            while Saloon.objects.filter(slug=unique_slug).exists():
                unique_slug = f"{slug}-{count}"
                count += 1

            self.slug = unique_slug

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.owner.email} - Saloon"


# =========================
# STEP 1 – BASIC PROFILE
# =========================

class SaloonProfile(models.Model):
    saloon = models.OneToOneField(
        Saloon,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    saloon_name = models.CharField(max_length=255, blank=True)
    owner_full_name = models.CharField(max_length=255)
    contact_number = models.CharField(max_length=20)

    CATEGORY_CHOICES = [
        ("men", "Men"),
        ("women", "Women"),
        ("unisex", "Unisex"),
    ]

    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES)

    state = models.ForeignKey(
        State,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="saloon_profiles",
    )
    district = models.ForeignKey(
        District,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="saloon_profiles",
    )
    city = models.CharField(max_length=100)
    locality = models.CharField(max_length=255)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    google_map_link = models.URLField(blank=True, null=True)
    about = models.TextField(blank=True)
    whatsapp_prefill_template = models.TextField(
        blank=True,
        help_text="Optional WhatsApp message template. Use {salon_name} and {services} placeholders.",
    )

    opening_time = models.TimeField(null=True, blank=True)
    closing_time = models.TimeField(null=True, blank=True)
    operating_days = models.JSONField(null=True, blank=True)

    @staticmethod
    def _parse_coordinate_pair(value):
        if not value:
            return None

        match = re.search(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", value)
        if not match:
            return None

        try:
            # Google Maps URLs commonly encode coordinates with 7+ decimal
            # digits, but the latitude/longitude fields only store 6
            # (~11cm precision, plenty for a salon pin). Round instead of
            # rejecting, so a pasted link doesn't silently fail validation
            # further down the line.
            latitude = Decimal(match.group(1)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            longitude = Decimal(match.group(2)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError):
            return None

        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None

        return latitude, longitude

    @classmethod
    def extract_coordinates_from_map_link(cls, link):
        if not link:
            return None

        decoded_link = unquote(link)
        pair = cls._parse_coordinate_pair(decoded_link)
        if pair:
            return pair

        parsed = urlparse(decoded_link)
        query = parse_qs(parsed.query)
        for key in ["q", "query", "ll", "sll", "daddr"]:
            values = query.get(key) or []
            for value in values:
                pair = cls._parse_coordinate_pair(value)
                if pair:
                    return pair

        return None

    @staticmethod
    def expand_google_map_link(link):
        if not link:
            return link

        if "maps.app.goo.gl" not in link and "goo.gl/maps" not in link:
            return link

        try:
            request = Request(
                link,
                headers={
                    "User-Agent": "Mozilla/5.0",
                },
            )
            with urlopen(request, timeout=8) as response:
                return response.geturl() or link
        except Exception:
            return link

    def get_google_maps_url(self):
        if self.google_map_link:
            return self.google_map_link
        if self.latitude is None or self.longitude is None:
            return ""
        return f"https://www.google.com/maps?q={self.latitude},{self.longitude}"

    def get_whatsapp_prefill_template(self):
        if self.whatsapp_prefill_template.strip():
            return self.whatsapp_prefill_template.strip()
        return (
            "Hello {salon_name}, I would like to check availability for these services:\n"
            "{services}\n\n"
            "Please share the available time slots and anything I should know before visiting."
        )

    def save(self, *args, **kwargs):
        if self.google_map_link:
            self.google_map_link = self.expand_google_map_link(self.google_map_link)

        extracted_pair = self.extract_coordinates_from_map_link(self.google_map_link)
        if extracted_pair:
            self.latitude, self.longitude = extracted_pair

        if not self.google_map_link and self.latitude is not None and self.longitude is not None:
            self.google_map_link = self.get_google_maps_url()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.saloon} – Profile"


class SaloonReview(models.Model):
    saloon = models.ForeignKey(
        Saloon,
        on_delete=models.CASCADE,
        related_name="reviews"
    )
    reviewer_name = models.CharField(max_length=120)
    rating = models.PositiveSmallIntegerField()
    review_text = models.TextField()
    is_visible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.saloon} - {self.rating} star review"


class SaloonProfileView(models.Model):
    saloon = models.ForeignKey(
        Saloon,
        on_delete=models.CASCADE,
        related_name="profile_view_events"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["saloon", "created_at"]),
        ]

    def __str__(self):
        return f"{self.saloon} profile view"


class SaloonMapClick(models.Model):
    saloon = models.ForeignKey(
        Saloon,
        on_delete=models.CASCADE,
        related_name="map_click_events"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["saloon", "created_at"]),
        ]

    def __str__(self):
        return f"{self.saloon} map click"


# =========================
# STEP 2 – VERIFICATION
# =========================

class SaloonVerification(models.Model):
    saloon = models.OneToOneField(
        Saloon,
        on_delete=models.CASCADE,
        related_name="verification"
    )

    owner_id_proof = models.FileField(
        upload_to="saloons/verification/id/"
    )
    owner_id_number = models.CharField(max_length=100)

    inside_image_1 = models.ImageField(
        upload_to="saloons/verification/inside/"
    )
    inside_image_2 = models.ImageField(
        upload_to="saloons/verification/inside/",
        null=True,
        blank=True
    )
    inside_image_3 = models.ImageField(
        upload_to="saloons/verification/inside/",
        null=True,
        blank=True
    )

    outside_image_1 = models.ImageField(
        upload_to="saloons/verification/outside/"
    )
    outside_image_2 = models.ImageField(
        upload_to="saloons/verification/outside/",
        null=True,
        blank=True
    )
    outside_image_3 = models.ImageField(
        upload_to="saloons/verification/outside/",
        null=True,
        blank=True
    )

    account_holder_name = models.CharField(max_length=255, blank=True)
    account_number = models.CharField(max_length=50, blank=True)
    ifsc_code = models.CharField(max_length=20, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.saloon} – Verification"


# =========================
# ADMIN REJECTION SYSTEM
# =========================

class SaloonRejectionReason(models.Model):
    saloon = models.ForeignKey(
        Saloon,
        on_delete=models.CASCADE,
        related_name="rejection_reasons"
    )

    field_key = models.CharField(
        max_length=100,
        help_text="Example: banner_image, inside_image_1"
    )

    message = models.CharField(max_length=255)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.saloon} – {self.field_key}"


# =========================
# TRUSTED DEVICES
# =========================

class TrustedDevice(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trusted_devices"
    )

    device_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.email} - {self.device_token}"


# =========================
# GALLERY
# =========================

class GalleryPost(models.Model):
    saloon = models.ForeignKey(
        Saloon,
        on_delete=models.CASCADE,
        related_name="gallery_posts"
    )
    service = models.ForeignKey(
        "services.Service",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gallery_posts"
    )
    title = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    views = models.PositiveIntegerField(default=0)
    is_hidden = models.BooleanField(default=False)
    moderation_reason = models.TextField(blank=True)
    moderated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="moderated_gallery_posts",
    )
    moderated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def image_media(self):
        return [media for media in self.media.all() if media.media_type == GalleryMedia.TYPE_IMAGE]

    @property
    def first_image(self):
        return next(iter(self.image_media), None)

    def __str__(self):
        return f"{self.saloon} - Gallery Post"


class GalleryMedia(models.Model):
    TYPE_IMAGE = "image"
    TYPE_VIDEO = "video"

    TYPE_CHOICES = [
        (TYPE_IMAGE, "Image"),
        (TYPE_VIDEO, "Video"),
    ]

    post = models.ForeignKey(
        GalleryPost,
        on_delete=models.CASCADE,
        related_name="media"
    )
    file = models.FileField(upload_to="saloons/gallery/")
    media_type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    order = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.post_id} - {self.media_type}"


# =========================
# ADMIN PROXY MODELS
# =========================

class SaloonApprovalQueue(Saloon):
    class Meta:
        proxy = True
        verbose_name = "Manage Approval"
        verbose_name_plural = "Manage Approvals"


class ApprovedSaloon(Saloon):
    class Meta:
        proxy = True
        verbose_name = "Approved Saloon"
        verbose_name_plural = "Approved Saloons"

