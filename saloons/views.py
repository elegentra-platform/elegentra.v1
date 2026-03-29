import random
from datetime import timedelta

from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.contrib import messages
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.seo import build_absolute_url, build_image_url, build_seo_payload, truncate_text
from public.models import FavoriteSaloon
from .serializers import (
    SaloonOnboardingStepOneSerializer,
    SaloonOnboardingStepTwoSerializer,
    
)
from .models import (
    Saloon,
    TrustedDevice,
    SaloonProfile,
    GalleryPost,
    SaloonReview,
    SaloonProfileView,
    SaloonMapClick,
)
from services.models import Service, ServiceCategory, ServiceInterest
from .utils import send_otp_whatsapp, format_whatsapp_number, build_service_slots
from django.utils.text import slugify
from django.db.models import Q, Avg, Count


OTP_COOLDOWN_SECONDS = 5
OTP_MAX_ATTEMPTS = 3
OTP_LOCK_SECONDS = 5
User = get_user_model()


DAY_KEYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def _format_time_label(value):
    if not value:
        return ""
    return value.strftime("%I:%M %p").lstrip("0")


def _display_name(saloon, profile=None):
    profile = profile or getattr(saloon, "profile", None)
    if profile and profile.saloon_name:
        return profile.saloon_name
    if saloon.name:
        return saloon.name
    return "Salon"


def _build_hours_summary(profile):
    if not profile or not profile.opening_time or not profile.closing_time:
        return {
            "label": "",
            "is_open": None,
            "open_time": "",
            "close_time": "",
        }

    now = timezone.localtime()
    day_key = now.strftime("%A").lower()
    operating_days = profile.operating_days or DAY_KEYS
    is_operating_today = day_key in operating_days
    open_time = profile.opening_time
    close_time = profile.closing_time
    current_time = now.time().replace(second=0, microsecond=0)
    is_open = is_operating_today and open_time <= current_time <= close_time

    if is_open:
        label = f"Open now · Closes {_format_time_label(close_time)}"
    elif is_operating_today:
        label = f"Closed now · Opens {_format_time_label(open_time)}"
    else:
        label = "Closed today"

    return {
        "label": label,
        "is_open": is_open,
        "open_time": _format_time_label(open_time),
        "close_time": _format_time_label(close_time),
    }


def _get_review_summary(saloon):
    summary = saloon.reviews.filter(is_visible=True).aggregate(
        average=Avg("rating"),
        total=Count("id"),
    )
    average = summary["average"] or 0
    total = summary["total"] or 0
    return {
        "average": round(float(average), 1) if total else 0,
        "total": total,
    }


def _saloon_card_payload(saloon):
    profile = getattr(saloon, "profile", None)
    image_url = ""
    if saloon.banner_image:
        image_url = saloon.banner_image.url
    else:
        first_service = saloon.services.filter(
            is_active=True,
            is_visible=True,
            is_deleted=False,
        ).first()
        if first_service and first_service.image:
            image_url = first_service.image.url

    location_parts = []
    if profile and profile.locality:
        location_parts.append(profile.locality)
    if profile and profile.city:
        location_parts.append(profile.city)

    return {
        "name": _display_name(saloon, profile),
        "slug": saloon.slug,
        "image": image_url or f"{settings.STATIC_URL}services/images/hero.webp",
        "location": ", ".join(location_parts) or "Nearby",
        "rating": getattr(saloon, "avg_rating", None),
        "review_count": getattr(saloon, "review_count", 0) or 0,
    }


def _related_saloons(saloon, profile):
    if not profile:
        return {"nearby": [], "city": []}

    base_queryset = (
        Saloon.objects.filter(
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
        )
        .exclude(id=saloon.id)
        .annotate(avg_rating=Avg("reviews__rating", filter=Q(reviews__is_visible=True)))
        .annotate(review_count=Count("reviews", filter=Q(reviews__is_visible=True)))
        .select_related("profile")
        .prefetch_related("services")
    )

    nearby_queryset = base_queryset.none()
    if profile.locality and profile.city:
        nearby_queryset = base_queryset.filter(
            profile__city__iexact=profile.city,
            profile__locality__iexact=profile.locality,
        )

    city_queryset = base_queryset.none()
    if profile.city:
        city_queryset = base_queryset.filter(profile__city__iexact=profile.city)
        if profile.locality:
            city_queryset = city_queryset.exclude(profile__locality__iexact=profile.locality)

    return {
        "nearby": [_saloon_card_payload(item) for item in nearby_queryset.order_by("-updated_at", "-id")[:4]],
        "city": [_saloon_card_payload(item) for item in city_queryset.order_by("-updated_at", "-id")[:4]],
    }


def _saloon_json_ld(request, saloon, profile, display_name, map_url, review_summary, services):
    locality = profile.locality if profile else ""
    city = profile.city if profile else ""
    address = {
        "@type": "PostalAddress",
        "addressLocality": city,
        "streetAddress": locality,
        "addressCountry": "IN",
    }

    data = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": display_name,
        "url": build_absolute_url(request, reverse("public_saloon", kwargs={"slug": saloon.slug})),
        "description": truncate_text(profile.about if profile else ""),
        "telephone": saloon.whatsapp_number,
        "address": address,
        "areaServed": city or locality,
        "image": build_image_url(
            request,
            saloon.banner_image.url if saloon.banner_image else "",
        ),
    }

    if map_url:
        data["hasMap"] = map_url
    if profile and profile.latitude is not None and profile.longitude is not None:
        data["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": float(profile.latitude),
            "longitude": float(profile.longitude),
        }
    if profile and profile.opening_time and profile.closing_time:
        days = profile.operating_days or DAY_KEYS
        schema_days = [f"https://schema.org/{day.capitalize()}" for day in days]
        data["openingHoursSpecification"] = [
            {
                "@type": "OpeningHoursSpecification",
                "dayOfWeek": schema_days,
                "opens": profile.opening_time.strftime("%H:%M"),
                "closes": profile.closing_time.strftime("%H:%M"),
            }
        ]
    if review_summary["total"]:
        data["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": review_summary["average"],
            "reviewCount": review_summary["total"],
        }
    if services:
        data["makesOffer"] = [
            {
                "@type": "Offer",
                "itemOffered": {
                    "@type": "Service",
                    "name": service.name,
                },
                "priceCurrency": "INR",
                "price": str(service.offer_price or service.price),
            }
            for service in services[:8]
        ]
    return data


def _saloon_seo(request, saloon, profile, display_name, review_summary, services, map_url):
    city = profile.city if profile and profile.city else "your city"
    locality = profile.locality if profile and profile.locality else ""
    service_names = ", ".join(service.name for service in services[:3])
    location_label = ", ".join([part for part in [locality, city] if part])
    description_parts = [
        f"Discover {display_name}",
        f"in {location_label}" if location_label else "",
        f"with services like {service_names}" if service_names else "",
    ]
    description = " ".join(part for part in description_parts if part).strip()
    if review_summary["total"]:
        description += f". Rated {review_summary['average']} from {review_summary['total']} reviews."
    else:
        description += ". Explore services, timings, map location, and contact details."

    return build_seo_payload(
        request,
        title=f"{display_name} in {city} | Elegentra",
        description=description,
        canonical_url=build_absolute_url(request, reverse("public_saloon", kwargs={"slug": saloon.slug})),
        image_url=saloon.banner_image.url if saloon.banner_image else "",
        og_type="business.business",
        json_ld=_saloon_json_ld(request, saloon, profile, display_name, map_url, review_summary, services),
    )


def _build_review_cards(reviews, current_user=None):
    usernames = [review.reviewer_name for review in reviews if review.reviewer_name]
    users = {
        user.username: user
        for user in User.objects.filter(username__in=usernames).prefetch_related("socialaccount_set")
    }

    cards = []
    for review in reviews:
        user = users.get(review.reviewer_name)
        avatar_url = ""
        display_name = review.reviewer_name

        if user:
            display_name = user.get_full_name().strip() or user.username
            social_account = user.socialaccount_set.first()
            if social_account:
                avatar_url = social_account.get_avatar_url()

        cards.append({
            "id": review.id,
            "display_name": display_name,
            "avatar_url": avatar_url,
            "date_label": timezone.localtime(review.created_at).strftime("%b %d, %Y · %I:%M %p").lstrip("0"),
            "rating": review.rating,
            "review_text": review.review_text,
            "initial": (display_name[:1] or "?").upper(),
            "can_edit": bool(
                current_user
                and current_user.is_authenticated
                and review.reviewer_name == current_user.username
            ),
            "edited": bool(
                review.updated_at
                and review.created_at
                and review.updated_at > (review.created_at + timedelta(seconds=1))
            ),
        })

    return cards


def _gallery_posts_with_images(saloon):
    posts = list(saloon.gallery_posts.prefetch_related("media").all())
    return [post for post in posts if post.image_media]


def build_service_slots(existing_services, rejection_map):
    """
    Always return exactly 3 slots for the UI
    """
    slots = []

    for i in range(3):
        if i < len(existing_services):
            service = existing_services[i]
            slots.append({
                "name": service["name"],
                "price": service["price"],
                "image_url": service["image_url"],
                "category_id": service.get("category_id"),
                "rejection": rejection_map.get(f"service_image_{i+1}")
            })
        else:
            slots.append({
                "name": "",
                "price": "",
                "image_url": None,
                "category_id": None,
                "rejection": None
            })

    return slots


def partner_home(request):
    if request.user.is_authenticated:
        saloon = Saloon.objects.filter(owner=request.user).first()

        if saloon:
            if saloon.registration_step == 1:
                return redirect("saloon_onboarding_step_one")

            if saloon.registration_step == 2:
                return redirect("saloon_onboarding_step_two")

            if saloon.registration_step < 4:
                return redirect("saloon_onboarding_step_three")

            return redirect("saloon_dashboard", username=request.user.username)

    return render(request, "saloons/partner_home.html")


@login_required
def dashboard(request, username):
    if request.user.username != username:
        return redirect("home")

    saloon = Saloon.objects.filter(owner=request.user).first()
    if not saloon:
        return redirect("partner_home")

    if not saloon.is_whatsapp_verified:
        return redirect("verify_whatsapp")

    device_token = request.COOKIES.get("salon_device")
    if not device_token:
        return redirect("verify_whatsapp")

    trusted = TrustedDevice.objects.filter(
        user=request.user,
        device_token=device_token
    ).first()

    if not trusted:
        return redirect("verify_whatsapp")

    trusted.last_used_at = timezone.now()
    trusted.save(update_fields=["last_used_at"])

    # 🟡 SETUP IN PROGRESS — RESUME EXACT STEP
    if saloon.registration_step < 5:
        if saloon.registration_step == 1:
            continue_url = "saloon_onboarding_step_one"
        elif saloon.registration_step == 2:
            continue_url = "saloon_onboarding_step_two"
        elif saloon.registration_step == 3:
            continue_url = "saloon_onboarding_step_three"
        else:
            continue_url = "saloon_onboarding_step_four"

        return render(
            request,
            "saloons/dashboard/submitting.html",
            {
                "saloon": saloon,
                "continue_url": continue_url,
            }
        )

    # 🟠 PENDING ADMIN REVIEW
    if saloon.approval_status == Saloon.APPROVAL_PENDING:
        return render(
            request,
            "saloons/dashboard/pending.html",
            {"saloon": saloon}
        )

    # 🔴 REJECTED / INACTIVE
    if saloon.approval_status == Saloon.APPROVAL_REJECTED or not saloon.is_active:
        return render(
            request,
            "saloons/dashboard/rejected.html",
            {"saloon": saloon}
        )

    # 🟢 APPROVED & LIVE
    return render(
        request,
        "saloons/dashboard/dashboard.html",
        {"saloon": saloon}
    )



# ============================
# WHATSAPP VERIFICATION (UNCHANGED)
# ============================
@login_required
def verify_whatsapp(request):
    saloon = Saloon.objects.filter(owner=request.user).first()
    now = timezone.now()

    otp = request.session.get("vendor_otp")
    phone = request.session.get("vendor_phone")
    attempts = request.session.get("otp_attempts", 0)
    locked_until = request.session.get("otp_locked_until")
    current_phone = format_whatsapp_number(saloon.whatsapp_number) if saloon and saloon.whatsapp_number else ""
    is_existing_saloon = bool(saloon and current_phone)

    def render_verify(step):
        return render(
            request,
            "saloons/verify_whatsapp.html",
            {
                "step": step,
                "masked_phone": phone[-4:].rjust(len(phone), "*") if phone else "",
                "is_existing_saloon": is_existing_saloon,
            },
        )

    # Existing salons must always verify against their already stored WhatsApp number.
    if is_existing_saloon:
        if phone != current_phone:
            request.session.pop("vendor_otp", None)
            request.session["vendor_phone"] = current_phone
            otp = None
        phone = current_phone

    if locked_until:
        unlock_time = timezone.datetime.fromisoformat(locked_until)
        if now < unlock_time:
            remaining = int((unlock_time - now).total_seconds())
            messages.error(request, f"Too many attempts. Try again in {remaining} seconds.")
            return render_verify("otp" if otp and phone else "phone")
        request.session.pop("otp_attempts", None)
        request.session.pop("otp_locked_until", None)
        attempts = 0

    if request.method == "GET" and is_existing_saloon and phone and not otp:
        if Saloon.objects.filter(
            whatsapp_number=phone,
            is_whatsapp_verified=True,
        ).exclude(owner=request.user).exists():
            messages.error(request, "This WhatsApp number is already linked to another salon.")
            return render_verify("phone")

        new_otp = random.randint(100000, 999999)
        request.session.update({
            "vendor_otp": str(new_otp),
            "vendor_phone": phone,
            "otp_attempts": 1,
        })
        otp = str(new_otp)
        send_otp_whatsapp(phone, new_otp)
        return render_verify("otp")

    if request.method == "POST" and "send_otp" in request.POST:
        if is_existing_saloon:
            phone = current_phone
        else:
            raw_phone = request.POST.get("phone")
            phone = format_whatsapp_number(raw_phone) if raw_phone else request.session.get("vendor_phone")

        if not phone:
            messages.error(request, "Phone number is required")
            return render_verify("phone")

        if Saloon.objects.filter(
            whatsapp_number=phone,
            is_whatsapp_verified=True,
        ).exclude(owner=request.user).exists():
            messages.error(request, "This WhatsApp number is already linked to another salon.")
            return render_verify("phone")

        attempts += 1
        if attempts > OTP_MAX_ATTEMPTS:
            lock_until = now + timedelta(seconds=OTP_LOCK_SECONDS)
            request.session["otp_locked_until"] = lock_until.isoformat()
            messages.error(request, "OTP locked. Try again later.")
            return render_verify("phone")

        new_otp = random.randint(100000, 999999)
        request.session.update({
            "vendor_otp": str(new_otp),
            "vendor_phone": phone,
            "otp_attempts": attempts,
        })
        otp = str(new_otp)
        send_otp_whatsapp(phone, new_otp)
        return render_verify("otp")

    if request.method == "POST" and "verify_otp" in request.POST:
        if is_existing_saloon:
            phone = current_phone

        if request.POST.get("otp") != otp:
            messages.error(request, "Invalid OTP")
            return render_verify("otp")

        if Saloon.objects.filter(
            whatsapp_number=phone,
            is_whatsapp_verified=True,
        ).exclude(owner=request.user).exists():
            messages.error(request, "This WhatsApp number is already verified by another salon.")
            return render_verify("phone")

        is_new_vendor = False
        if not saloon:
            saloon = Saloon.objects.create(
                owner=request.user,
                whatsapp_number=phone,
                is_whatsapp_verified=True,
                registration_step=1,
            )
            is_new_vendor = True
        else:
            saloon.is_whatsapp_verified = True
            saloon.save(update_fields=["is_whatsapp_verified"])

        trusted = TrustedDevice.objects.create(user=request.user)

        if is_new_vendor:
            redirect_to = redirect("partner_home")
        else:
            redirect_to = redirect("saloon_dashboard", username=request.user.username)

        redirect_to.set_cookie(
            "salon_device",
            str(trusted.device_token),
            max_age=60 * 60 * 24 * 60,
            httponly=True,
            samesite="Lax",
        )

        for key in ["vendor_otp", "vendor_phone", "otp_attempts", "otp_locked_until"]:
            request.session.pop(key, None)

        return redirect_to

    if otp and phone:
        return render_verify("otp")

    return render_verify("phone")


# ============================
# STEP 1 – NO ADMIN SUBMIT
# ============================
@login_required
def saloon_onboarding_step_one(request):
    saloon = Saloon.objects.filter(owner=request.user).first()
    whatsapp_verified = request.session.get("whatsapp_verified", False)

    # Allow proceeding if WhatsApp verified in session, even if Saloon object not created
    if not saloon and not whatsapp_verified:
        return redirect("verify_whatsapp")

    profile = getattr(saloon, "profile", None) if saloon else None

    context = {
        "data": {},
        "saloon_types": SaloonProfile.CATEGORY_CHOICES,
        "is_resubmission": False,
        "rejection_map": {},
    }

    if request.method == "POST":
        serializer = SaloonOnboardingStepOneSerializer(data=request.POST)

        if serializer.is_valid():
            # Save profile (creates Saloon if it doesn't exist)
            serializer.save(request.user)

            # Update registration step if Saloon exists
            if saloon:
                saloon.registration_step = 2
                saloon.save(update_fields=["registration_step"])

            return redirect("saloon_onboarding_step_two")

        context["data"] = request.POST
        context["rejection_map"] = serializer.errors
        context["is_resubmission"] = True

    else:
        context["data"] = {
            "saloon_name": saloon.name if saloon else "",
            "saloon_type": profile.category if profile else "",
            "owner_full_name": profile.owner_full_name if profile else "",
            "contact_number": profile.contact_number if profile else "",
            "area_locality": profile.locality if profile else "",
            "city": profile.city if profile else "",
            "latitude": profile.latitude if profile and profile.latitude is not None else "",
            "longitude": profile.longitude if profile and profile.longitude is not None else "",
            "google_map_link": profile.google_map_link if profile else "",
            "opening_time": profile.opening_time.strftime("%H:%M") if profile and profile.opening_time else "",
            "closing_time": profile.closing_time.strftime("%H:%M") if profile and profile.closing_time else "",
            "operating_days": profile.operating_days if profile and profile.operating_days else DAY_KEYS,
        }

    return render(request, "saloons/onboarding/onboarding_step_1.html", context)

# ============================
# STEP 2 – FINAL SUBMIT TO ADMIN
# ============================
@login_required
def saloon_onboarding_step_two(request):
    saloon = get_object_or_404(Saloon, owner=request.user)
    verification = getattr(saloon, "verification", None)

    # 🔴 normalize rejection reasons
    rejection_map = {}
    for r in saloon.rejection_reasons.all():
        if r.field_key.startswith("inside_image"):
            rejection_map["inside_images"] = r.message
        elif r.field_key.startswith("outside_image"):
            rejection_map["outside_images"] = r.message
        else:
            rejection_map[r.field_key] = r.message

    existing_images = {
        "owner_id_proof": verification.owner_id_proof.url if verification and verification.owner_id_proof else None,
        "inside_images": [],
        "outside_images": [],
        "owner_id_number": verification.owner_id_number if verification else "",
    }

    if verification:
        for i in range(1, 4):
            img = getattr(verification, f"inside_image_{i}", None)
            if img:
                existing_images["inside_images"].append(img.url)

        for i in range(1, 4):
            img = getattr(verification, f"outside_image_{i}", None)
            if img:
                existing_images["outside_images"].append(img.url)

    if request.method == "POST":
        serializer = SaloonOnboardingStepTwoSerializer(
            data=request.POST,
            context={
                "saloon": saloon,
                "request": request,
            },
        )

        if serializer.is_valid():
            serializer.save()

            saloon.rejection_reasons.all().delete()

            saloon.registration_step = 3
            saloon.save(update_fields=["registration_step"])

            return redirect("saloon_onboarding_step_three")


        rejection_map = {**rejection_map, **serializer.errors}

    return render(
        request,
        "saloons/onboarding/onboarding_step_2.html",
        {
            "rejection_map": rejection_map,
            "existing_images": existing_images,
        },
    )

# ============================
# STEP 3 – SERVICES (MAX 3)
# ============================
# ============================
# STEP 3 – SERVICES (MAX 3)
# ============================
@login_required
def saloon_onboarding_step_three(request):
    saloon = get_object_or_404(Saloon, owner=request.user)

    if saloon.registration_step < 3:
        return redirect("saloon_onboarding_step_two")

    rejection_map = {
        r.field_key: r.message
        for r in saloon.rejection_reasons.all()
    }

    categories = ServiceCategory.objects.filter(
        is_active=True
    ).filter(
        Q(is_approved=True) | Q(created_by_saloon=saloon)
    )

    existing_services_qs = Service.objects.filter(
        saloon=saloon,
        added_during_onboarding=True
    ).order_by("id")[:3]

    def normalize_services(qs):
        return [{
            "name": s.name,
            "price": s.price,
            "image_url": s.image.url if s.image else None,
            "category_id": s.category_id,
        } for s in qs]

    if request.method == "POST":
        banner = request.FILES.get("banner_image")
        services = []

        existing_services = list(existing_services_qs)

        for i in range(1, 4):
            name = request.POST.get(f"service_name_{i}", "").strip()
            price = request.POST.get(f"service_price_{i}", "").strip()
            category_id = request.POST.get(f"service_category_{i}")
            new_category = request.POST.get(f"service_new_category_{i}", "").strip()
            image = request.FILES.get(f"service_image_{i}")

            old_image = None
            if i <= len(existing_services):
                old_image = existing_services[i - 1].image

            if name or price or image or old_image:
                if new_category:
                    category, _ = ServiceCategory.objects.get_or_create(
                        name=new_category,
                        defaults={
                            "slug": slugify(new_category),
                            "is_active": True,
                            "is_approved": False,
                            "created_by_saloon": saloon,
                        }
                    )
                    category_id = category.id

                if not category_id:
                    rejection_map[f"service_category_{i}"] = "Category is required"
                    continue

                services.append({
                    "name": name,
                    "price": price,
                    "image": image or old_image,
                    "category_id": int(category_id),
                })

        if not banner and not saloon.banner_image:
            rejection_map["banner_image"] = "Banner image is required"

        if not services:
            rejection_map["services"] = "At least one service is required"

        if rejection_map:
            service_slots = build_service_slots(services, rejection_map)
            return render(request, "saloons/onboarding/onboarding_step_3.html", {
                "service_slots": service_slots,
                "banner_image": saloon.banner_image.url if saloon.banner_image else None,
                "rejection_map": rejection_map,
                "categories": categories,
            })

        # ===== SAVE =====
        if banner:
            saloon.banner_image = banner

        Service.objects.filter(
            saloon=saloon,
            added_during_onboarding=True
        ).delete()

        for s in services:
            Service.objects.create(
                saloon=saloon,
                name=s["name"],
                price=s["price"],
                image=s["image"],
                category_id=s["category_id"],
                is_active=False,
                is_visible=True,
                added_during_onboarding=True,
            )

        # 🔥 ONLY MOVE TO STEP 4
        saloon.registration_step = 4
        saloon.save()

        return redirect("saloon_onboarding_step_four")

    normalized_services = normalize_services(existing_services_qs)
    service_slots = build_service_slots(normalized_services, rejection_map)

    return render(request, "saloons/onboarding/onboarding_step_3.html", {
        "service_slots": service_slots,
        "banner_image": saloon.banner_image.url if saloon.banner_image else None,
        "rejection_map": rejection_map,
        "categories": categories,
    })
@login_required
def saloon_onboarding_step_four(request):
    saloon = get_object_or_404(Saloon, owner=request.user)

    if saloon.registration_step < 4:
        return redirect("saloon_onboarding_step_three")

    if request.method == "POST":
        # 🔥 Directly submit to admin without checkbox
        saloon.approval_status = Saloon.APPROVAL_PENDING
        saloon.registration_step = 5  # onboarding completed
        saloon.rejection_reasons.all().delete()
        saloon.save()

        return redirect("saloon_dashboard", username=request.user.username)

    return render(request, "saloons/onboarding/onboarding_step_4.html", {
        "saloon": saloon,
    })


def public_saloon(request, slug):
    saloon = get_object_or_404(
        Saloon,
        slug=slug,
        is_active=True,
        approval_status=Saloon.APPROVAL_APPROVED,
    )
    profile = getattr(saloon, "profile", None)
    SaloonProfileView.objects.create(saloon=saloon)
    map_url = profile.get_google_maps_url() if profile else ""
    services = saloon.services.filter(
        is_active=True,
        is_visible=True,
        is_deleted=False,
    ).select_related("category")
    display_name = _display_name(saloon, profile)
    whatsapp_template = profile.get_whatsapp_prefill_template() if profile else ""
    whatsapp_number = "".join(ch for ch in saloon.whatsapp_number if ch.isdigit())
    gallery_posts = _gallery_posts_with_images(saloon)[:6]
    review_summary = _get_review_summary(saloon)
    reviews = saloon.reviews.filter(is_visible=True)[:8]
    review_cards = _build_review_cards(reviews, request.user)
    hours_summary = _build_hours_summary(profile)
    related_saloons = _related_saloons(saloon, profile)
    is_favorited = bool(
        request.user.is_authenticated
        and FavoriteSaloon.objects.filter(user=request.user, saloon=saloon).exists()
    )
    return render(
        request,
        "saloons/public_saloon.html",
        {
            "saloon": saloon,
            "profile": profile,
            "display_name": display_name,
            "map_url": map_url,
            "services": services,
            "whatsapp_template": whatsapp_template,
            "public_whatsapp_number": whatsapp_number,
            "gallery_posts": gallery_posts,
            "reviews": reviews,
            "review_cards": review_cards,
            "review_summary": review_summary,
            "hours_summary": hours_summary,
            "is_favorited": is_favorited,
            "related_nearby_saloons": related_saloons["nearby"],
            "related_city_saloons": related_saloons["city"],
            "seo": _saloon_seo(request, saloon, profile, display_name, review_summary, list(services), map_url),
            "public_has_saloon": bool(request.user.is_authenticated and Saloon.objects.filter(owner=request.user).exists()),
        },
    )


@login_required
@require_POST
def public_saloon_favorite_toggle(request, slug):
    saloon = get_object_or_404(
        Saloon,
        slug=slug,
        is_active=True,
        approval_status=Saloon.APPROVAL_APPROVED,
    )
    favorite, created = FavoriteSaloon.objects.get_or_create(user=request.user, saloon=saloon)
    if created:
        return JsonResponse({"ok": True, "is_favorited": True})

    favorite.delete()
    return JsonResponse({"ok": True, "is_favorited": False})


@require_POST
def public_saloon_map_click(request, slug):
    saloon = get_object_or_404(
        Saloon,
        slug=slug,
        is_active=True,
        approval_status=Saloon.APPROVAL_APPROVED,
    )
    SaloonMapClick.objects.create(saloon=saloon)
    return JsonResponse({"ok": True})


@require_POST
def public_service_interest(request, slug):
    saloon = get_object_or_404(
        Saloon,
        slug=slug,
        is_active=True,
        approval_status=Saloon.APPROVAL_APPROVED,
    )
    service_id = (request.POST.get("service_id") or "").strip()
    service = get_object_or_404(
        Service,
        id=service_id,
        saloon=saloon,
        is_active=True,
        is_deleted=False,
    )
    ServiceInterest.objects.create(service=service)
    total_interest = service.interest_events.count()
    return JsonResponse({
        "ok": True,
        "service_id": service.id,
        "interest_total": total_interest,
    })


def public_saloon_add_review(request, slug):
    saloon = get_object_or_404(
        Saloon,
        slug=slug,
        is_active=True,
        approval_status=Saloon.APPROVAL_APPROVED,
    )

    if not request.user.is_authenticated:
        messages.error(request, "Please log in to submit a review.")
        return redirect(f"{reverse('account_login')}?next={reverse('public_saloon', kwargs={'slug': slug})}#about")

    if request.method != "POST":
        return redirect("public_saloon", slug=slug)

    review_text = (request.POST.get("review_text") or "").strip()
    rating_raw = (request.POST.get("rating") or "").strip()

    try:
        rating = int(rating_raw)
    except (TypeError, ValueError):
        rating = 0

    reviewer_name = request.user.username

    if not review_text or rating not in [1, 2, 3, 4, 5]:
        messages.error(request, "Please add a review and choose a valid star rating.")
        return redirect(f"{reverse('public_saloon', kwargs={'slug': slug})}#about")

    SaloonReview.objects.create(
        saloon=saloon,
        reviewer_name=reviewer_name[:120],
        rating=rating,
        review_text=review_text,
    )
    messages.success(request, "Thanks for sharing your review.")
    return redirect(f"{reverse('public_saloon', kwargs={'slug': slug})}#about")


@login_required
def public_saloon_edit_review(request, slug, review_id):
    saloon = get_object_or_404(
        Saloon,
        slug=slug,
        is_active=True,
        approval_status=Saloon.APPROVAL_APPROVED,
    )
    review = get_object_or_404(SaloonReview, id=review_id, saloon=saloon, is_visible=True)

    if review.reviewer_name != request.user.username:
        messages.error(request, "You can only edit your own review.")
        return redirect(f"{reverse('public_saloon', kwargs={'slug': slug})}#about")

    if request.method != "POST":
        return redirect(f"{reverse('public_saloon', kwargs={'slug': slug})}#about")

    review_text = (request.POST.get("review_text") or "").strip()
    rating_raw = (request.POST.get("rating") or "").strip()

    try:
        rating = int(rating_raw)
    except (TypeError, ValueError):
        rating = 0

    if not review_text or rating not in [1, 2, 3, 4, 5]:
        messages.error(request, "Please add a review and choose a valid star rating.")
        return redirect(f"{reverse('public_saloon', kwargs={'slug': slug})}#about")

    if review.rating == rating and review.review_text == review_text:
        messages.info(request, "No changes were made to your review.")
        return redirect(f"{reverse('public_saloon', kwargs={'slug': slug})}#about")

    review.rating = rating
    review.review_text = review_text
    review.save(update_fields=["rating", "review_text", "updated_at"])
    messages.success(request, "Your review was updated.")
    return redirect(f"{reverse('public_saloon', kwargs={'slug': slug})}#about")


@login_required
def public_saloon_delete_review(request, slug, review_id):
    saloon = get_object_or_404(
        Saloon,
        slug=slug,
        is_active=True,
        approval_status=Saloon.APPROVAL_APPROVED,
    )
    review = get_object_or_404(SaloonReview, id=review_id, saloon=saloon, is_visible=True)

    if review.reviewer_name != request.user.username:
        messages.error(request, "You can only delete your own review.")
        return redirect(f"{reverse('public_saloon', kwargs={'slug': slug})}#about")

    if request.method == "POST":
        review.delete()
        messages.success(request, "Your review was deleted.")

    return redirect(f"{reverse('public_saloon', kwargs={'slug': slug})}#about")


def public_gallery_detail(request, slug, post_id):
    saloon = get_object_or_404(
        Saloon,
        slug=slug,
        is_active=True,
        approval_status=Saloon.APPROVAL_APPROVED,
    )
    profile = getattr(saloon, "profile", None)
    if profile and profile.saloon_name:
        display_name = profile.saloon_name
    elif saloon.name:
        display_name = saloon.name
    else:
        display_name = "Salon"

    post = get_object_or_404(GalleryPost, id=post_id, saloon=saloon)
    if not post.image_media:
        return redirect("public_saloon", slug=saloon.slug)

    posts = _gallery_posts_with_images(saloon)
    post_ids = [p.id for p in posts]
    try:
        index = post_ids.index(post.id)
    except ValueError:
        index = 0

    prev_post = posts[index - 1] if index > 0 else None
    next_post = posts[index + 1] if index + 1 < len(posts) else None

    return render(
        request,
        "saloons/public_gallery_detail.html",
        {
            "saloon": saloon,
            "profile": profile,
            "display_name": display_name,
            "post": post,
            "ordered_posts": posts,
            "selected_post_id": post.id,
            "prev_post": prev_post,
            "next_post": next_post,
            "back_url": reverse("public_saloon", kwargs={"slug": saloon.slug}) + "#gallery",
        },
    )
