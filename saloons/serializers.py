from rest_framework import serializers
from .models import (
    Saloon,
    SaloonProfile,
    SaloonVerification,
)

# =========================
# STEP 1 — BASIC DETAILS
# =========================
class SaloonOnboardingStepOneSerializer(serializers.Serializer):

    saloon_name = serializers.CharField(max_length=255)
    owner_full_name = serializers.CharField(max_length=255)
    contact_number = serializers.CharField(max_length=20)

    saloon_type = serializers.ChoiceField(
        choices=SaloonProfile.CATEGORY_CHOICES
    )

    area_locality = serializers.CharField(max_length=255)
    city = serializers.CharField(max_length=100)

    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    google_map_link = serializers.URLField(
        required=False,
        allow_blank=True
    )
    opening_time = serializers.TimeField(required=False, allow_null=True)
    closing_time = serializers.TimeField(required=False, allow_null=True)
    operating_days = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
    )

    def validate(self, attrs):
        latitude = attrs.get("latitude")
        longitude = attrs.get("longitude")

        if (latitude is None) ^ (longitude is None):
            raise serializers.ValidationError("Please set both latitude and longitude for the salon location.")

        return attrs

    def save(self, user):
        saloon, _ = Saloon.objects.get_or_create(owner=user)

        # ❗ DO NOT SET approval_status HERE
        saloon.name = self.validated_data["saloon_name"]
        saloon.registration_step = max(saloon.registration_step, 2)
        saloon.save()

        profile, _ = SaloonProfile.objects.get_or_create(saloon=saloon)

        profile.saloon_name = self.validated_data["saloon_name"]
        profile.owner_full_name = self.validated_data["owner_full_name"]
        profile.contact_number = self.validated_data["contact_number"]
        profile.category = self.validated_data["saloon_type"]
        profile.city = self.validated_data["city"]
        profile.locality = self.validated_data["area_locality"]
        profile.latitude = self.validated_data.get("latitude")
        profile.longitude = self.validated_data.get("longitude")
        profile.google_map_link = self.validated_data.get("google_map_link", "")
        profile.opening_time = self.validated_data.get("opening_time")
        profile.closing_time = self.validated_data.get("closing_time")
        profile.operating_days = self.validated_data.get("operating_days", [])

        profile.save()
        return saloon


# =========================
# STEP 2 — VERIFICATION
# =========================
class SaloonOnboardingStepTwoSerializer(serializers.Serializer):

    owner_id_number = serializers.CharField(max_length=100)

    account_holder_name = serializers.CharField(required=False, allow_blank=True)
    account_number = serializers.CharField(required=False, allow_blank=True)
    ifsc_code = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data):
        request = self.context["request"]
        saloon = self.context["saloon"]

        rejected_fields = set(
            saloon.rejection_reasons.values_list("field_key", flat=True)
        )

        owner_id = request.FILES.get("owner_id_proof")
        inside = request.FILES.getlist("inside_images")
        outside = request.FILES.getlist("outside_images")

        if "owner_id_proof" in rejected_fields and not owner_id:
            raise serializers.ValidationError({
                "owner_id_proof": "Please reupload corrected ID proof"
            })

        if any(k.startswith("inside_image") for k in rejected_fields) and not inside:
            raise serializers.ValidationError({
                "inside_images": "Please reupload corrected inside images"
            })

        if any(k.startswith("outside_image") for k in rejected_fields) and not outside:
            raise serializers.ValidationError({
                "outside_images": "Please reupload corrected outside images"
            })

        data["_files"] = {
            "owner_id": owner_id,
            "inside": inside,
            "outside": outside,
        }

        return data

    def save(self):
        saloon = self.context["saloon"]
        files = self.validated_data.pop("_files")

        verification, _ = SaloonVerification.objects.get_or_create(
            saloon=saloon
        )

        if files["owner_id"]:
            verification.owner_id_proof = files["owner_id"]

        verification.owner_id_number = self.validated_data["owner_id_number"]
        verification.account_holder_name = self.validated_data.get("account_holder_name", "")
        verification.account_number = self.validated_data.get("account_number", "")
        verification.ifsc_code = self.validated_data.get("ifsc_code", "")

        if files["inside"]:
            for i in range(1, 4):
                setattr(verification, f"inside_image_{i}", None)
            for i, img in enumerate(files["inside"][:3], 1):
                setattr(verification, f"inside_image_{i}", img)

        if files["outside"]:
            for i in range(1, 4):
                setattr(verification, f"outside_image_{i}", None)
            for i, img in enumerate(files["outside"][:3], 1):
                setattr(verification, f"outside_image_{i}", img)

        verification.save()

        # ✅ move step forward only
        saloon.registration_step = max(saloon.registration_step, 3)
        saloon.save()

        return verification
