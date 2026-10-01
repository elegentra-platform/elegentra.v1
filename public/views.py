import math
import mimetypes
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings as django_settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Avg, Count, Exists, OuterRef
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from core.seo import build_absolute_url, build_seo_payload
from public.models import FavoriteSaloon, UserNotification
from plans.models import SaloonSubscription
from saloons.models import Saloon
from saloons.utils import build_saloon_public_url, extract_saloon_slug_from_host, sync_saloon_slug_from_name
from services.models import MainCategory, ServiceCategory


DEFAULT_SALOON_IMAGE = f"{django_settings.STATIC_URL}services/images/hero.webp"

CATEGORY_ICON_FALLBACKS = {
    "haircut-styling": f"{django_settings.STATIC_URL}services/images/haircut.jpg",
    "hair-coloring": f"{django_settings.STATIC_URL}services/images/haircoloring.webp",
    "bridal-makeup": f"{django_settings.STATIC_URL}services/images/about.jpg",
    "kids-grooming": f"{django_settings.STATIC_URL}services/images/hero.webp",
    "spa-massage": f"{django_settings.STATIC_URL}services/images/about.jpg",
    "nails-beauty": f"{django_settings.STATIC_URL}services/images/banner-grimrend.png",
}


def _category_icon_fallback(slug):
    return CATEGORY_ICON_FALLBACKS.get(slug, f"{django_settings.STATIC_URL}services/images/hero.webp")


def _category_icon_url(slug, uploaded_url=""):
    return uploaded_url or _category_icon_fallback(slug)


class _RangeFileWrapper:
    def __init__(self, file_obj, offset, length, block_size=8192):
        self.file_obj = file_obj
        self.remaining = length
        self.block_size = block_size
        self.file_obj.seek(offset)

    def __iter__(self):
        return self

    def __next__(self):
        chunk = self.read(self.block_size)
        if not chunk:
            self.close()
            raise StopIteration
        return chunk

    def read(self, size=-1):
        if self.remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self.block_size
        size = min(size, self.remaining)
        chunk = self.file_obj.read(size)
        self.remaining -= len(chunk)
        return chunk

    def close(self):
        try:
            self.file_obj.close()
        except Exception:
            pass


def media_range_serve(request, path):
    media_root = Path(django_settings.MEDIA_ROOT).resolve()
    requested_path = (media_root / path).resolve()

    try:
        requested_path.relative_to(media_root)
    except ValueError as exc:
        raise Http404("Invalid media path.") from exc

    if not requested_path.exists() or not requested_path.is_file():
        raise Http404("Media file not found.")

    file_size = requested_path.stat().st_size
    content_type, _ = mimetypes.guess_type(str(requested_path))
    content_type = content_type or "application/octet-stream"
    range_header = request.headers.get("Range", "").strip()

    if not range_header.startswith("bytes="):
        response = FileResponse(open(requested_path, "rb"), content_type=content_type)
        response["Content-Length"] = str(file_size)
        response["Accept-Ranges"] = "bytes"
        return response

    try:
        range_value = range_header.split("=", 1)[1]
        start_str, end_str = range_value.split("-", 1)
        if start_str == "":
            length = int(end_str)
            start = max(file_size - length, 0)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
    except (ValueError, IndexError):
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response

    if start < 0 or end < start or start >= file_size:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response

    end = min(end, file_size - 1)
    chunk_length = end - start + 1
    file_wrapper = _RangeFileWrapper(open(requested_path, "rb"), start, chunk_length)
    response = StreamingHttpResponse(file_wrapper, status=206, content_type=content_type)
    response["Content-Length"] = str(chunk_length)
    response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response["Accept-Ranges"] = "bytes"
    return response



def _platform_visible_saloons(queryset):
    now = timezone.now()
    live_subscription = SaloonSubscription.objects.filter(saloon=OuterRef("pk")).filter(
        Q(trial_ends_at__gt=now)
        | Q(status=SaloonSubscription.STATUS_ACTIVE, current_period_end__isnull=True)
        | Q(status=SaloonSubscription.STATUS_ACTIVE, current_period_end__gt=now)
        | Q(status=SaloonSubscription.STATUS_PAST_DUE, grace_period_ends_at__gt=now)
    )
    return queryset.annotate(has_platform_access=Exists(live_subscription)).filter(has_platform_access=True)

def _user_has_saloon(user):
    return bool(user.is_authenticated and Saloon.objects.filter(owner=user).exists())


def _public_categories():
    main_categories = MainCategory.objects.filter(is_active=True).order_by("display_order", "name")
    if main_categories.exists():
        return [
            {
                "slug": category.slug,
                "name": category.name,
                "icon_url": _category_icon_url(
                    category.slug,
                    category.icon_image.url if category.icon_image else "",
                ),
                "icon_fallback_url": _category_icon_fallback(category.slug),
            }
            for category in main_categories
        ]

    categories = ServiceCategory.objects.filter(
        is_approved=True,
        is_active=True,
    ).order_by("name")
    return [
        {
            "slug": category.main_category.slug if category.main_category else category.slug,
            "name": category.name,
            "icon_url": _category_icon_url(
                category.main_category.slug if category.main_category else category.slug,
                (
                    category.main_category.icon_image.url
                    if category.main_category and category.main_category.icon_image
                    else ""
                ),
            ),
            "icon_fallback_url": _category_icon_fallback(
                category.main_category.slug if category.main_category else category.slug
            ),
        }
        for category in categories
    ]


def _clean_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _saloon_display_name(saloon):
    profile = getattr(saloon, "profile", None)
    if profile and profile.saloon_name:
        return profile.saloon_name
    if saloon.name:
        return saloon.name
    return "Salon"


def _request_coords(request):
    lat = _clean_float(request.GET.get("lat"))
    lng = _clean_float(request.GET.get("lng"))

    if lat is not None and lng is not None:
        request.session["public_lat"] = lat
        request.session["public_lng"] = lng
        return lat, lng

    session_lat = _clean_float(request.session.get("public_lat"))
    session_lng = _clean_float(request.session.get("public_lng"))
    return session_lat, session_lng


def _distance_km(lat1, lng1, lat2, lng2):
    radius_km = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _clean_location_label(locality="", city=""):
    locality = (locality or "").strip()
    city = (city or "").strip()

    if locality and city:
        locality_parts = [part.strip() for part in locality.split(",") if part.strip()]
        normalized_city = city.casefold()
        locality_parts = [part for part in locality_parts if part.casefold() != normalized_city]
        locality = ", ".join(locality_parts)

        if locality and locality.casefold().endswith(normalized_city):
            trimmed = locality[: -len(city)].rstrip(" ,")
            if trimmed:
                locality = trimmed

    return ", ".join([item for item in [locality, city] if item]) or "Nearby"


def _serialize_saloons(saloons, user_lat=None, user_lng=None, favorite_slugs=None, request=None):
    favorite_slugs = favorite_slugs or set()
    result = []
    for saloon in saloons:
        saloon = sync_saloon_slug_from_name(saloon)
        image = DEFAULT_SALOON_IMAGE
        if saloon.banner_image:
            image = saloon.banner_image.url
        else:
            first_service = next(iter(saloon.services.all()), None)
            if first_service and first_service.image:
                image = first_service.image.url

        profile = getattr(saloon, "profile", None)
        locality = getattr(profile, "locality", "") if profile else ""
        city = getattr(profile, "city", "") if profile else ""
        location = _clean_location_label(locality, city)
        distance_label = location

        if (
            user_lat is not None
            and user_lng is not None
            and profile
            and profile.latitude is not None
            and profile.longitude is not None
        ):
            distance = _distance_km(user_lat, user_lng, float(profile.latitude), float(profile.longitude))
            distance_label = f"{distance:.1f} km away"

        avg_rating = getattr(saloon, "avg_rating", None)
        review_count = getattr(saloon, "review_count", 0) or 0
        rating_label = "New"
        if avg_rating:
            rating_label = f"{float(avg_rating):.1f} ({review_count})"

        result.append(
            {
                "name": _saloon_display_name(saloon),
                "distance": distance_label,
                "rating": rating_label,
                "image": image,
                "slug": saloon.slug,
                "public_url": build_saloon_public_url(saloon.slug, request=request),
                "is_favorited": saloon.slug in favorite_slugs,
                "distance_value": _clean_float(distance_label.split()[0]) if "km away" in distance_label else None,
            }
        )

    if user_lat is not None and user_lng is not None:
        result.sort(key=lambda item: item["distance_value"] if item["distance_value"] is not None else 999999)

    return result


def _favorite_card_payload(favorite, request=None):
    saloon = favorite.saloon
    saloon = sync_saloon_slug_from_name(saloon)
    profile = getattr(saloon, "profile", None)
    image = DEFAULT_SALOON_IMAGE
    if saloon.banner_image:
        image = saloon.banner_image.url
    else:
        first_service = next(iter(saloon.services.all()), None)
        if first_service and first_service.image:
            image = first_service.image.url

    locality = getattr(profile, "locality", "") if profile else ""
    city = getattr(profile, "city", "") if profile else ""
    location = _clean_location_label(locality, city)
    avg_rating = getattr(favorite, "avg_rating", None)
    review_count = getattr(favorite, "review_count", 0) or 0

    return {
        "id": favorite.id,
        "name": _saloon_display_name(saloon),
        "slug": saloon.slug,
        "public_url": build_saloon_public_url(saloon.slug, request=request),
        "image": image,
        "location": location,
        "rating": round(float(avg_rating), 1) if avg_rating else None,
        "review_count": review_count,
    }


def _home_saloons(user_lat=None, user_lng=None, request=None):
    saloons = (
        _platform_visible_saloons(Saloon.objects.filter(
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
        ))
        .annotate(avg_rating=Avg("reviews__rating", filter=Q(reviews__is_visible=True)))
        .annotate(review_count=Count("reviews", filter=Q(reviews__is_visible=True)))
        .select_related("profile")
        .prefetch_related("services")
        .order_by("-updated_at", "-id")[:12]
    )
    return _serialize_saloons(saloons, user_lat=user_lat, user_lng=user_lng, request=request)


def _approved_saloons_queryset():
    return (
        _platform_visible_saloons(Saloon.objects.filter(
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
        ))
        .annotate(avg_rating=Avg("reviews__rating", filter=Q(reviews__is_visible=True)))
        .annotate(review_count=Count("reviews", filter=Q(reviews__is_visible=True)))
        .select_related("profile")
        .prefetch_related("services")
    )


def _search_saloons(query, category_slug):
    saloons = _approved_saloons_queryset()

    if category_slug:
        saloons = saloons.filter(
            services__is_active=True,
            services__is_visible=True,
            services__is_deleted=False,
            services__category__is_active=True,
            services__category__is_approved=True,
            services__category__main_category__slug=category_slug,
        )

    if query:
        saloons = saloons.filter(
            Q(name__icontains=query)
            | Q(profile__saloon_name__icontains=query)
            | Q(profile__district__name__icontains=query)
            | Q(profile__state__name__icontains=query)
            | Q(profile__city__icontains=query)
            | Q(profile__locality__icontains=query)
            | Q(services__name__icontains=query)
            | Q(services__category__name__icontains=query)
        )

    return saloons.distinct().order_by("-updated_at", "-id")


def _resolve_place_names(district_slug, city_slug):
    place_rows = (
        _approved_saloons_queryset()
        .exclude(profile__district__name__isnull=True)
        .exclude(profile__district__name__exact="")
        .exclude(profile__city__isnull=True)
        .exclude(profile__city__exact="")
        .values("profile__district__name", "profile__city")
        .distinct()
    )
    for row in place_rows:
        district_name = row["profile__district__name"]
        city_name = row["profile__city"]
        if slugify(district_name) == district_slug and slugify(city_name) == city_slug:
            return district_name, city_name
    return "", ""


def _resolve_district_name(district_slug):
    district_names = (
        _approved_saloons_queryset()
        .exclude(profile__district__name__isnull=True)
        .exclude(profile__district__name__exact="")
        .values_list("profile__district__name", flat=True)
        .distinct()
    )
    for district_name in district_names:
        if slugify(district_name) == district_slug:
            return district_name
    return ""


def _resolve_legacy_city_places(city_slug):
    place_rows = (
        _approved_saloons_queryset()
        .exclude(profile__district__name__isnull=True)
        .exclude(profile__district__name__exact="")
        .exclude(profile__city__isnull=True)
        .exclude(profile__city__exact="")
        .values("profile__district__name", "profile__city")
        .distinct()
    )
    matches = []
    seen = set()
    for row in place_rows:
        district_name = row["profile__district__name"]
        city_name = row["profile__city"]
        key = (district_name.casefold(), city_name.casefold())
        if slugify(city_name) == city_slug and key not in seen:
            seen.add(key)
            matches.append((district_name, city_name))
    return matches


def _resolve_locality_name(district_name, city_name, locality_slug):
    locality_names = (
        _approved_saloons_queryset()
        .filter(
            profile__district__name__iexact=district_name,
            profile__city__iexact=city_name,
        )
        .exclude(profile__locality__isnull=True)
        .exclude(profile__locality__exact="")
        .values_list("profile__locality", flat=True)
        .distinct()
    )
    for locality_name in locality_names:
        if slugify(locality_name) == locality_slug:
            return locality_name
    return ""


def _resolve_category_name(category_slug, categories):
    for category in categories:
        if category["slug"] == category_slug:
            return category["name"]
    return ""


def _city_place_url(district_name, city_name):
    district_name = (district_name or "").strip()
    city_name = (city_name or "").strip()
    if not city_name:
        return ""
    if not district_name:
        return reverse("public_place_city_legacy", kwargs={"city_slug": slugify(city_name)})
    return reverse(
        "public_place_city",
        kwargs={"district_slug": slugify(district_name), "city_slug": slugify(city_name)},
    )


def _district_place_url(district_name):
    district_name = (district_name or "").strip()
    if not district_name:
        return ""
    return reverse("public_place_district", kwargs={"district_slug": slugify(district_name)})


def _locality_place_url(district_name, city_name, locality_name):
    district_name = (district_name or "").strip()
    city_name = (city_name or "").strip()
    locality_name = (locality_name or "").strip()
    if not city_name or not locality_name:
        return ""
    if not district_name:
        return reverse(
            "public_place_locality_legacy",
            kwargs={"city_slug": slugify(city_name), "locality_slug": slugify(locality_name)},
        )
    return reverse(
        "public_place_locality",
        kwargs={
            "district_slug": slugify(district_name),
            "city_slug": slugify(city_name),
            "locality_slug": slugify(locality_name),
        },
    )


def _city_category_place_url(district_name, city_name, category_slug):
    district_name = (district_name or "").strip()
    city_name = (city_name or "").strip()
    category_slug = (category_slug or "").strip()
    if not city_name or not category_slug:
        return ""
    if not district_name:
        return reverse(
            "public_place_city_legacy_category",
            kwargs={"city_slug": slugify(city_name), "category_slug": category_slug},
        )
    return reverse(
        "public_place_city_category",
        kwargs={
            "district_slug": slugify(district_name),
            "city_slug": slugify(city_name),
            "category_slug": category_slug,
        },
    )


def _district_category_place_url(district_name, category_slug):
    district_name = (district_name or "").strip()
    category_slug = (category_slug or "").strip()
    if not district_name or not category_slug:
        return ""
    return reverse(
        "public_place_district_category",
        kwargs={"district_slug": slugify(district_name), "category_slug": category_slug},
    )


def _locality_category_place_url(district_name, city_name, locality_name, category_slug):
    district_name = (district_name or "").strip()
    city_name = (city_name or "").strip()
    locality_name = (locality_name or "").strip()
    category_slug = (category_slug or "").strip()
    if not city_name or not locality_name or not category_slug:
        return ""
    if not district_name:
        return reverse(
            "public_place_locality_legacy_category",
            kwargs={
                "city_slug": slugify(city_name),
                "locality_slug": slugify(locality_name),
                "category_slug": category_slug,
            },
        )
    return reverse(
        "public_place_locality_category",
        kwargs={
            "district_slug": slugify(district_name),
            "city_slug": slugify(city_name),
            "locality_slug": slugify(locality_name),
            "category_slug": category_slug,
        },
    )


def _place_base_queryset(district_name, city_name, locality_name="", category_slug=""):
    saloons = _approved_saloons_queryset().filter(
        profile__district__name__iexact=district_name,
        profile__city__iexact=city_name,
    )
    if locality_name:
        saloons = saloons.filter(profile__locality__iexact=locality_name)
    if category_slug:
        saloons = saloons.filter(
            services__is_active=True,
            services__is_visible=True,
            services__is_deleted=False,
            services__category__is_active=True,
            services__category__is_approved=True,
            services__category__main_category__slug=category_slug,
        )
    return saloons.distinct().order_by("-updated_at", "-id")


def _district_base_queryset(district_name, category_slug=""):
    saloons = _approved_saloons_queryset().filter(profile__district__name__iexact=district_name)
    if category_slug:
        saloons = saloons.filter(
            services__is_active=True,
            services__is_visible=True,
            services__is_deleted=False,
            services__category__is_active=True,
            services__category__is_approved=True,
            services__category__main_category__slug=category_slug,
        )
    return saloons.distinct().order_by("-updated_at", "-id")


def _place_filter_categories(district_name, city_name, locality_name="", categories=None):
    categories = categories or _public_categories()
    valid_slugs = []
    for category in categories:
        has_match = _place_base_queryset(
            district_name,
            city_name,
            locality_name=locality_name,
            category_slug=category["slug"],
        ).exists()
        if has_match:
            valid_slugs.append(category["slug"])
    return [category for category in categories if category["slug"] in valid_slugs]


def _district_filter_categories(district_name, categories=None):
    categories = categories or _public_categories()
    valid_slugs = []
    for category in categories:
        has_match = _district_base_queryset(district_name, category_slug=category["slug"]).exists()
        if has_match:
            valid_slugs.append(category["slug"])
    return [category for category in categories if category["slug"] in valid_slugs]


def _top_city_links(limit=8):
    city_rows = (
        _approved_saloons_queryset()
        .exclude(profile__district__name__isnull=True)
        .exclude(profile__district__name__exact="")
        .exclude(profile__city__isnull=True)
        .exclude(profile__city__exact="")
        .values("profile__district__name", "profile__city")
        .annotate(total=Count("id"))
        .order_by("-total", "profile__district__name", "profile__city")[:limit]
    )
    return [
        {
            "name": row["profile__city"],
            "district_name": row["profile__district__name"],
            "count": row["total"],
            "url": _city_place_url(row["profile__district__name"], row["profile__city"]),
        }
        for row in city_rows
        if row["profile__district__name"] and row["profile__city"]
    ]


def _search_place_suggestions(query, selected_category, categories, limit=6):
    saloons = _search_saloons(query, selected_category) if (query or selected_category) else Saloon.objects.none()
    suggestions = []
    seen = set()

    city_rows = (
        saloons.exclude(profile__district__name__isnull=True)
        .exclude(profile__district__name__exact="")
        .exclude(profile__city__isnull=True)
        .exclude(profile__city__exact="")
        .values("profile__district__name", "profile__city")
        .annotate(total=Count("id"))
        .order_by("-total", "profile__district__name", "profile__city")[:limit]
    )

    for row in city_rows:
        district_name = row["profile__district__name"]
        city_name = row["profile__city"]
        if not district_name or not city_name:
            continue
        if selected_category:
            url = _city_category_place_url(district_name, city_name, selected_category)
            label = f"{_resolve_category_name(selected_category, categories) or 'Salons'} in {city_name}"
        else:
            url = _city_place_url(district_name, city_name)
            label = f"Browse salons in {city_name}, {district_name}"
        key = ("city", district_name.casefold(), city_name.casefold(), selected_category or "")
        if url and key not in seen:
            seen.add(key)
            suggestions.append({"label": label, "url": url, "count": row["total"]})

    locality_rows = (
        saloons.exclude(profile__district__name__isnull=True)
        .exclude(profile__district__name__exact="")
        .exclude(profile__city__isnull=True)
        .exclude(profile__city__exact="")
        .exclude(profile__locality__isnull=True)
        .exclude(profile__locality__exact="")
        .values("profile__district__name", "profile__city", "profile__locality")
        .annotate(total=Count("id"))
        .order_by("-total", "profile__district__name", "profile__city", "profile__locality")[:limit]
    )

    for row in locality_rows:
        district_name = row["profile__district__name"]
        city_name = row["profile__city"]
        locality_name = row["profile__locality"]
        if not district_name or not city_name or not locality_name:
            continue
        if selected_category:
            url = _locality_category_place_url(district_name, city_name, locality_name, selected_category)
            label = f"{_resolve_category_name(selected_category, categories) or 'Salons'} in {locality_name}, {city_name}"
        else:
            url = _locality_place_url(district_name, city_name, locality_name)
            label = f"Browse salons in {locality_name}, {city_name}"
        key = ("locality", district_name.casefold(), city_name.casefold(), locality_name.casefold(), selected_category or "")
        if url and key not in seen:
            seen.add(key)
            suggestions.append({"label": label, "url": url, "count": row["total"]})

    return suggestions[:limit]


def _place_parent_links(district_name, city_name, locality_name="", category_slug="", categories=None):
    categories = categories or _public_categories()
    links = []
    category_name = _resolve_category_name(category_slug, categories) if category_slug else ""

    if locality_name:
        links.append({
            "label": f"All salons in {city_name}, {district_name}",
            "url": _city_place_url(district_name, city_name),
        })
    if category_slug:
        links.append({
            "label": f"All salons in {', '.join(part for part in [locality_name, city_name] if part)}" if locality_name else f"All salons in {city_name}",
            "url": _locality_place_url(district_name, city_name, locality_name) if locality_name else _city_place_url(district_name, city_name),
        })
        links.append({
            "label": f"{category_name} in {city_name}" if category_name else f"Explore {city_name}",
            "url": _city_category_place_url(district_name, city_name, category_slug) if category_name else _city_place_url(district_name, city_name),
        })
    return [link for link in links if link["url"]]


def _district_parent_links(district_name, category_slug="", categories=None):
    categories = categories or _public_categories()
    links = []
    category_name = _resolve_category_name(category_slug, categories) if category_slug else ""
    if category_slug:
        links.append({
            "label": f"All salons in {district_name}",
            "url": _district_place_url(district_name),
        })
        links.append({
            "label": f"{category_name} in {district_name}" if category_name else f"Explore {district_name}",
            "url": _district_category_place_url(district_name, category_slug) if category_name else _district_place_url(district_name),
        })
    return [link for link in links if link["url"]]


def _place_seo(request, district_name, city_name, locality_name="", category_name="", canonical_url=""):
    location_label = ", ".join(part for part in [locality_name, city_name, district_name] if part)
    if category_name and locality_name:
        title = f"{category_name} in {locality_name}, {city_name}, {district_name} | Elegentra"
        description = (
            f"Discover {category_name.lower()} in {locality_name}, {city_name}, {district_name} on Elegentra. "
            "Explore approved salons, photos, services, and direct contact details."
        )
    elif category_name:
        title = f"{category_name} in {city_name}, {district_name} | Elegentra"
        description = (
            f"Discover {category_name.lower()} in {city_name}, {district_name} on Elegentra. "
            "Explore approved salons, photos, services, and direct contact details."
        )
    elif locality_name:
        title = f"Salons in {locality_name}, {city_name}, {district_name} | Elegentra"
        description = (
            f"Discover approved salons in {locality_name}, {city_name}, {district_name} on Elegentra. "
            "Compare salon photos, services, ratings, and direct contact options."
        )
    else:
        title = f"Salons in {city_name}, {district_name} | Elegentra"
        description = (
            f"Discover approved salons in {city_name}, {district_name} on Elegentra. "
            "Compare salon photos, services, ratings, and direct contact options."
        )

    json_ld = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": title,
        "url": canonical_url,
        "description": description,
        "about": {
            "@type": "Place",
            "name": location_label or city_name,
        },
    }
    if category_name:
        json_ld["mentions"] = {
            "@type": "Service",
            "name": category_name,
        }

    return build_seo_payload(
        request,
        title=title,
        description=description,
        canonical_url=canonical_url,
        json_ld=json_ld,
    )


def _district_seo(request, district_name, category_name="", canonical_url=""):
    if category_name:
        title = f"{category_name} in {district_name} | Elegentra"
        description = (
            f"Discover {category_name.lower()} in {district_name} on Elegentra. "
            "Explore approved salons, photos, services, and direct contact details."
        )
    else:
        title = f"Salons in {district_name} | Elegentra"
        description = (
            f"Discover approved salons in {district_name} on Elegentra. "
            "Compare salon photos, services, ratings, and direct contact options."
        )

    json_ld = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": title,
        "url": canonical_url,
        "description": description,
        "about": {
            "@type": "AdministrativeArea",
            "name": district_name,
        },
    }
    if category_name:
        json_ld["mentions"] = {"@type": "Service", "name": category_name}

    return build_seo_payload(
        request,
        title=title,
        description=description,
        canonical_url=canonical_url,
        json_ld=json_ld,
    )


def _build_place_entries(request):
    categories = _public_categories()
    entries = []
    district_names = (
        _approved_saloons_queryset()
        .exclude(profile__district__name__isnull=True)
        .exclude(profile__district__name__exact="")
        .values_list("profile__district__name", flat=True)
        .distinct()
    )
    for district_name in district_names:
        district_slug = slugify(district_name)
        entries.append(
            build_absolute_url(
                request,
                reverse("public_place_district", kwargs={"district_slug": district_slug}),
            )
        )
        district_categories = _district_filter_categories(district_name, categories=categories)
        for category in district_categories:
            entries.append(
                build_absolute_url(
                    request,
                    reverse(
                        "public_place_district_category",
                        kwargs={"district_slug": district_slug, "category_slug": category["slug"]},
                    ),
                )
            )

    place_rows = (
        _approved_saloons_queryset()
        .exclude(profile__district__name__isnull=True)
        .exclude(profile__district__name__exact="")
        .exclude(profile__city__isnull=True)
        .exclude(profile__city__exact="")
        .values("profile__district__name", "profile__city")
        .distinct()
    )
    for row in place_rows:
        district_name = row["profile__district__name"]
        city_name = row["profile__city"]
        if not district_name or not city_name:
            continue
        district_slug = slugify(district_name)
        city_slug = slugify(city_name)
        entries.append(
            build_absolute_url(
                request,
                reverse("public_place_city", kwargs={"district_slug": district_slug, "city_slug": city_slug}),
            )
        )

        city_categories = _place_filter_categories(district_name, city_name, categories=categories)
        for category in city_categories:
            entries.append(
                build_absolute_url(
                    request,
                    reverse(
                        "public_place_city_category",
                        kwargs={
                            "district_slug": district_slug,
                            "city_slug": city_slug,
                            "category_slug": category["slug"],
                        },
                    ),
                )
            )

        locality_names = (
            _approved_saloons_queryset()
            .filter(
                profile__district__name__iexact=district_name,
                profile__city__iexact=city_name,
            )
            .exclude(profile__locality__isnull=True)
            .exclude(profile__locality__exact="")
            .values_list("profile__locality", flat=True)
            .distinct()
        )
        for locality_name in locality_names:
            locality_slug = slugify(locality_name)
            entries.append(
                build_absolute_url(
                    request,
                    reverse(
                        "public_place_locality",
                        kwargs={
                            "district_slug": district_slug,
                            "city_slug": city_slug,
                            "locality_slug": locality_slug,
                        },
                    ),
                )
            )
            locality_categories = _place_filter_categories(
                district_name,
                city_name,
                locality_name=locality_name,
                categories=categories,
            )
            for category in locality_categories:
                entries.append(
                    build_absolute_url(
                        request,
                        reverse(
                            "public_place_locality_category",
                            kwargs={
                                "district_slug": district_slug,
                                "city_slug": city_slug,
                                "locality_slug": locality_slug,
                                "category_slug": category["slug"],
                            },
                        ),
                    )
                )
    return entries


def _home_seo(request, categories):
    category_names = [category["name"] for category in categories[:4]]
    category_hint = ", ".join(category_names)
    description = (
        "Find nearby salons, spas, and beauty services on Elegentra. "
        f"Explore {category_hint} and more with reviews, maps, and direct contact options."
        if category_hint
        else "Find nearby salons, spas, and beauty services on Elegentra with reviews, maps, and direct contact options."
    )
    return build_seo_payload(
        request,
        title="Elegentra | Discover Nearby Salons, Spas, and Beauty Services",
        description=description,
        canonical_url=build_absolute_url(request, reverse("public_home")),
    )


def _search_seo(request, query, selected_category, categories):
    category_map = {category["slug"]: category["name"] for category in categories}
    category_name = category_map.get(selected_category, "")

    if query and category_name:
        title = f"{query} {category_name} Results | Elegentra"
        description = f"Search results for {query} in {category_name} on Elegentra."
    elif query:
        title = f"Search Results for {query} | Elegentra"
        description = f"Explore salons and beauty businesses matching {query} on Elegentra."
    elif category_name:
        title = f"{category_name} on Elegentra"
        description = f"Browse {category_name.lower()} providers on Elegentra."
    else:
        title = "Search Salons and Beauty Services | Elegentra"
        description = "Search salons, spas, and beauty services on Elegentra."

    return build_seo_payload(
        request,
        title=title,
        description=description,
        canonical_url=build_absolute_url(request, reverse("public_search")),
        robots="noindex,follow",
    )


def home(request):
    host_slug = extract_saloon_slug_from_host(request)
    if host_slug:
        from saloons.views import public_saloon as saloon_public_view
        return saloon_public_view(request, host_slug)

    is_vendor = _user_has_saloon(request.user)
    user_lat, user_lng = _request_coords(request)
    categories = _public_categories()
    favorite_slugs = set()
    if request.user.is_authenticated:
        favorite_slugs = set(
            FavoriteSaloon.objects.filter(user=request.user).values_list("saloon__slug", flat=True)
        )
    popular_saloons = _serialize_saloons(
        _platform_visible_saloons(Saloon.objects.filter(
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
        ))
        .annotate(avg_rating=Avg("reviews__rating", filter=Q(reviews__is_visible=True)))
        .annotate(review_count=Count("reviews", filter=Q(reviews__is_visible=True)))
        .select_related("profile")
        .prefetch_related("services")
        .order_by("-updated_at", "-id")[:12],
        user_lat=user_lat,
        user_lng=user_lng,
        favorite_slugs=favorite_slugs,
        request=request,
    )

    return render(
        request,
        "home.html",
        {
            "is_vendor": is_vendor,
            "public_has_saloon": is_vendor,
            "active_nav": "home",
            "categories": categories,
            "popular_saloons": popular_saloons,
            "nearby_saloons": popular_saloons[:8],
            "popular_places": _top_city_links(),
            "user_lat": user_lat,
            "user_lng": user_lng,
            "seo": _home_seo(request, categories),
        },
    )


def search(request):
    query = (request.GET.get("q") or "").strip()
    selected_category = (request.GET.get("category") or "").strip()
    user_lat, user_lng = _request_coords(request)
    categories = _public_categories()
    valid_category_slugs = {category["slug"] for category in categories}
    if selected_category and selected_category not in valid_category_slugs:
        selected_category = ""

    saloon_qs = _search_saloons(query, selected_category) if (query or selected_category) else Saloon.objects.none()
    paginator = Paginator(saloon_qs, 12)
    page_obj = paginator.get_page(request.GET.get("page"))
    favorite_slugs = set()
    if request.user.is_authenticated:
        favorite_slugs = set(
            FavoriteSaloon.objects.filter(user=request.user).values_list("saloon__slug", flat=True)
        )
    search_results = _serialize_saloons(
        page_obj.object_list,
        user_lat=user_lat,
        user_lng=user_lng,
        favorite_slugs=favorite_slugs,
        request=request,
    )

    return render(
        request,
        "search.html",
        {
            "active_nav": "search",
            "public_has_saloon": _user_has_saloon(request.user),
            "categories": categories,
            "query": query,
            "selected_category": selected_category,
            "search_results": search_results,
            "has_filters": bool(query or selected_category),
            "place_suggestions": _search_place_suggestions(query, selected_category, categories),
            "page_obj": page_obj,
            "user_lat": user_lat,
            "user_lng": user_lng,
            "seo": _search_seo(request, query, selected_category, categories),
        },
    )


def place_listing(request, district_slug, city_slug, locality_slug=None, category_slug=None):
    categories = _public_categories()
    district_name, city_name = _resolve_place_names(district_slug, city_slug)
    if not district_name or not city_name:
        raise Http404("Location not found.")

    locality_name = ""
    if locality_slug:
        locality_name = _resolve_locality_name(district_name, city_name, locality_slug)
        if not locality_name:
            raise Http404("Location not found.")

    category_name = ""
    if category_slug:
        category_name = _resolve_category_name(category_slug, categories)
        if not category_name:
            raise Http404("Category not found.")

    place_qs = _place_base_queryset(
        district_name,
        city_name,
        locality_name=locality_name,
        category_slug=category_slug or "",
    )
    if not place_qs.exists():
        raise Http404("No salons found for this place.")

    user_lat, user_lng = _request_coords(request)
    favorite_slugs = set()
    if request.user.is_authenticated:
        favorite_slugs = set(
            FavoriteSaloon.objects.filter(user=request.user).values_list("saloon__slug", flat=True)
        )

    paginator = Paginator(place_qs, 12)
    page_obj = paginator.get_page(request.GET.get("page"))
    listing_cards = _serialize_saloons(
        page_obj.object_list,
        user_lat=user_lat,
        user_lng=user_lng,
        favorite_slugs=favorite_slugs,
        request=request,
    )

    if locality_name and category_slug:
        canonical_url = build_absolute_url(
            request,
            reverse(
                "public_place_locality_category",
                kwargs={
                    "district_slug": district_slug,
                    "city_slug": city_slug,
                    "locality_slug": locality_slug,
                    "category_slug": category_slug,
                },
            ),
        )
    elif locality_name:
        canonical_url = build_absolute_url(
            request,
            reverse(
                "public_place_locality",
                kwargs={"district_slug": district_slug, "city_slug": city_slug, "locality_slug": locality_slug},
            ),
        )
    elif category_slug:
        canonical_url = build_absolute_url(
            request,
            reverse(
                "public_place_city_category",
                kwargs={"district_slug": district_slug, "city_slug": city_slug, "category_slug": category_slug},
            ),
        )
    else:
        canonical_url = build_absolute_url(
            request,
            reverse("public_place_city", kwargs={"district_slug": district_slug, "city_slug": city_slug}),
        )

    location_heading = ", ".join(part for part in [locality_name, city_name, district_name] if part)
    filter_categories = _place_filter_categories(
        district_name,
        city_name,
        locality_name=locality_name,
        categories=categories,
    )
    category_links = []
    for category in filter_categories:
        url = (
            _locality_category_place_url(district_name, city_name, locality_name, category["slug"])
            if locality_name
            else _city_category_place_url(district_name, city_name, category["slug"])
        )
        category_links.append({**category, "url": url})
    parent_links = _place_parent_links(
        district_name,
        city_name,
        locality_name=locality_name,
        category_slug=category_slug or "",
        categories=categories,
    )

    return render(
        request,
        "place_listing.html",
        {
            "active_nav": "search",
            "public_has_saloon": _user_has_saloon(request.user),
            "categories": category_links,
            "selected_category": category_slug or "",
            "listing_cards": listing_cards,
            "page_obj": page_obj,
            "district_slug": district_slug,
            "city_slug": city_slug,
            "locality_slug": locality_slug or "",
            "district_name": district_name,
            "city_name": city_name,
            "locality_name": locality_name,
            "category_name": category_name,
            "parent_links": parent_links,
            "location_heading": location_heading or ", ".join(part for part in [city_name, district_name] if part),
            "user_lat": user_lat,
            "user_lng": user_lng,
            "seo": _place_seo(
                request,
                district_name,
                city_name,
                locality_name=locality_name,
                category_name=category_name,
                canonical_url=canonical_url,
            ),
        },
    )


def district_place_listing(request, district_slug, category_slug=None):
    categories = _public_categories()
    district_name = _resolve_district_name(district_slug)
    if not district_name:
        return legacy_place_redirect(request, district_slug, category_slug=category_slug)

    category_name = ""
    if category_slug:
        category_name = _resolve_category_name(category_slug, categories)
        if not category_name:
            raise Http404("Category not found.")

    place_qs = _district_base_queryset(district_name, category_slug=category_slug or "")
    if not place_qs.exists():
        raise Http404("No salons found for this place.")

    user_lat, user_lng = _request_coords(request)
    favorite_slugs = set()
    if request.user.is_authenticated:
        favorite_slugs = set(
            FavoriteSaloon.objects.filter(user=request.user).values_list("saloon__slug", flat=True)
        )

    paginator = Paginator(place_qs, 12)
    page_obj = paginator.get_page(request.GET.get("page"))
    listing_cards = _serialize_saloons(
        page_obj.object_list,
        user_lat=user_lat,
        user_lng=user_lng,
        favorite_slugs=favorite_slugs,
        request=request,
    )

    if category_slug:
        canonical_url = build_absolute_url(
            request,
            reverse(
                "public_place_district_category",
                kwargs={"district_slug": district_slug, "category_slug": category_slug},
            ),
        )
    else:
        canonical_url = build_absolute_url(
            request,
            reverse("public_place_district", kwargs={"district_slug": district_slug}),
        )

    filter_categories = _district_filter_categories(district_name, categories=categories)
    category_links = [
        {**category, "url": _district_category_place_url(district_name, category["slug"])}
        for category in filter_categories
    ]
    parent_links = _district_parent_links(district_name, category_slug=category_slug or "", categories=categories)

    return render(
        request,
        "place_listing.html",
        {
            "active_nav": "search",
            "public_has_saloon": _user_has_saloon(request.user),
            "categories": category_links,
            "selected_category": category_slug or "",
            "listing_cards": listing_cards,
            "page_obj": page_obj,
            "district_slug": district_slug,
            "city_slug": "",
            "locality_slug": "",
            "district_name": district_name,
            "city_name": "",
            "locality_name": "",
            "category_name": category_name,
            "parent_links": parent_links,
            "location_heading": district_name,
            "user_lat": user_lat,
            "user_lng": user_lng,
            "seo": _district_seo(
                request,
                district_name,
                category_name=category_name,
                canonical_url=canonical_url,
            ),
        },
    )


def legacy_place_redirect(request, city_slug, locality_slug=None, category_slug=None):
    matches = _resolve_legacy_city_places(city_slug)
    if not matches:
        raise Http404("Location not found.")

    if len(matches) == 1:
        district_name, city_name = matches[0]
        if locality_slug and category_slug:
            return redirect(
                "public_place_locality_category",
                district_slug=slugify(district_name),
                city_slug=slugify(city_name),
                locality_slug=locality_slug,
                category_slug=category_slug,
            )
        if locality_slug:
            return redirect(
                "public_place_locality",
                district_slug=slugify(district_name),
                city_slug=slugify(city_name),
                locality_slug=locality_slug,
            )
        if category_slug:
            return redirect(
                "public_place_city_category",
                district_slug=slugify(district_name),
                city_slug=slugify(city_name),
                category_slug=category_slug,
            )
        return redirect(
            "public_place_city",
            district_slug=slugify(district_name),
            city_slug=slugify(city_name),
        )

    query = city_slug.replace("-", " ")
    redirect_url = reverse("public_search")
    if query:
        redirect_url += f"?q={query}"
    return redirect(redirect_url)


def favorites(request):
    favorite_cards = []
    if request.user.is_authenticated:
        favorites_qs = (
            FavoriteSaloon.objects.filter(
                user=request.user,
                saloon__in=_platform_visible_saloons(Saloon.objects.filter(
                    is_active=True,
                    approval_status=Saloon.APPROVAL_APPROVED,
                )),
            )
            .select_related("saloon__profile")
            .prefetch_related("saloon__services")
            .annotate(avg_rating=Avg("saloon__reviews__rating", filter=Q(saloon__reviews__is_visible=True)))
            .annotate(review_count=Count("saloon__reviews", filter=Q(saloon__reviews__is_visible=True)))
        )
        favorite_cards = [_favorite_card_payload(item, request=request) for item in favorites_qs]

    return render(
        request,
        "favorite.html",
        {
            "active_nav": "favorites",
            "public_has_saloon": _user_has_saloon(request.user),
            "favorite_cards": favorite_cards,
            "seo": build_seo_payload(
                request,
                title="Your Favourites | Elegentra",
                description="View your saved salons and beauty businesses on Elegentra.",
                canonical_url=build_absolute_url(request, reverse("public_favorites")),
                robots="noindex,follow",
            ),
        },
    )

@login_required
def notifications(request):
    notification_list = UserNotification.objects.filter(user=request.user)
    notification_list.filter(is_read=False).update(is_read=True)
    paginator = Paginator(notification_list, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "notifications.html",
        {
            "active_nav": "account",
            "public_has_saloon": _user_has_saloon(request.user),
            "page_obj": page_obj,
            "notifications": page_obj.object_list,
            "seo": build_seo_payload(
                request,
                title="Notifications | Elegentra",
                description="View your Elegentra account notifications.",
                canonical_url=build_absolute_url(request, reverse("public_notifications")),
                robots="noindex,follow",
            ),
        },
    )


@login_required
@require_POST
def notification_delete(request, notification_id):
    notification = get_object_or_404(UserNotification, id=notification_id, user=request.user)
    notification.delete()
    return redirect("public_notifications")


@login_required
@require_POST
def notifications_clear(request):
    UserNotification.objects.filter(user=request.user).delete()
    return redirect("public_notifications")

def account(request):
    has_saloon = False
    if request.user.is_authenticated:
        has_saloon = Saloon.objects.filter(owner=request.user).exists()

    return render(
        request,
        "account.html",
        {
            "active_nav": "account",
            "has_saloon": has_saloon,
            "public_has_saloon": has_saloon,
            "seo": build_seo_payload(
                request,
                title="Your Account | Elegentra",
                description="Manage your Elegentra account and connected salon profile.",
                canonical_url=build_absolute_url(request, reverse("public_account")),
                robots="noindex,follow",
            ),
        },
    )


def settings(request):
    return render(
        request,
        "settings.html",
        {
            "active_nav": "settings",
            "public_has_saloon": _user_has_saloon(request.user),
            "seo": build_seo_payload(
                request,
                title="Settings | Elegentra",
                description="Manage your Elegentra settings and preferences.",
                canonical_url=build_absolute_url(request, reverse("public_settings")),
                robots="noindex,follow",
            ),
        },
    )


def faq(request):
    faqs = [
        {
            "question": "How do I find salons near me on Elegentra?",
            "answer": "Use the search bar, choose a category, and allow location when prompted. Elegentra will show nearby salons, spas, and beauty businesses with reviews, maps, and contact options.",
        },
        {
            "question": "Do I need an account to browse salons?",
            "answer": "No. You can explore salons, services, and gallery posts without logging in. An account is only needed for actions like saving favourites or managing your own profile.",
        },
        {
            "question": "How do I contact a salon I like?",
            "answer": "Open the salon page and use the available actions like call, map, or booking-related options shown on that profile.",
        },
        {
            "question": "Can salon owners join Elegentra?",
            "answer": "Yes. Salon owners can create a partner account, add services, upload gallery posts, and manage their profile through the partner flow.",
        },
        {
            "question": "Why is my location used?",
            "answer": "Location helps Elegentra surface salons near you and improves search relevance. If you do not allow location, you can still browse manually.",
        },
        {
            "question": "What kind of businesses are listed here?",
            "answer": "Elegentra focuses on salons, spas, beauty studios, grooming businesses, and related beauty service providers.",
        },
    ]

    return render(
        request,
        "faq.html",
        {
            "public_has_saloon": _user_has_saloon(request.user),
            "faqs": faqs,
            "seo": build_seo_payload(
                request,
                title="FAQ | Elegentra",
                description="Answers to common questions about browsing salons, using search, location, accounts, and partner access on Elegentra.",
                canonical_url=build_absolute_url(request, reverse("public_faq")),
            ),
        },
    )



LEGAL_PAGES = {
    "privacy-policy": {
        "title": "Privacy Policy",
        "eyebrow": "Legal",
        "description": "How Elegentra collects, uses, stores, and protects information for customers, salon partners, and platform users.",
        "updated": "22 September 2026",
        "sections": [
            ("Overview", ["Elegentra is a beauty and wellness discovery platform that helps people discover salons, spas, barber shops, nail studios, wellness businesses, and beauty professionals. This Privacy Policy explains how we handle information when people browse Elegentra, create accounts, register a business, contact listed businesses, or use partner tools."]),
            ("Information we collect", ["We may collect account details such as name, email address, mobile number, login information, and preferences.", "For salon partners, we may collect business details such as salon name, owner details, location, services, gallery images, verification documents, and subscription status.", "We may collect technical information such as device type, browser, IP address, pages visited, approximate location permission status, and usage analytics to improve the platform."]),
            ("How we use information", ["We use information to operate Elegentra, show public business profiles, manage partner onboarding, verify salons, improve search and discovery, provide support, prevent abuse, and maintain platform safety.", "Customer-facing information such as salon name, services, gallery images, ratings, reviews, location, and contact options may be displayed publicly when a salon is approved."]),
            ("Payments", ["Customers do not pay salons through Elegentra. Salon partners may pay Elegentra subscription fees through our payment provider. Payment processing is handled by the payment provider, and Elegentra does not store full card, UPI mandate, or banking credentials."]),
            ("Private verification files", ["Documents uploaded for verification, such as identity or business proof, are intended for internal review and are not meant to be publicly displayed. Access should be limited to authorized Elegentra staff or administrators."]),
            ("Data sharing", ["We do not sell personal information. We may share limited information with service providers needed to operate the platform, such as hosting, storage, analytics, communication, and payment processing providers."]),
            ("Data retention", ["We keep information for as long as needed to provide the platform, comply with legal or accounting needs, resolve disputes, prevent misuse, and support business operations."]),
            ("Your choices", ["You may request updates or corrections to your account or business information. Salon partners can manage many profile details from their dashboard. For privacy requests, contact us using the support details on this site."]),
        ],
    },
    "terms-and-conditions": {
        "title": "Terms & Conditions",
        "eyebrow": "Legal",
        "description": "The basic terms for using Elegentra as a visitor, customer, salon partner, staff user, or administrator.",
        "updated": "22 September 2026",
        "sections": [
            ("Acceptance", ["By using Elegentra, creating an account, registering a salon, or accessing partner tools, you agree to these Terms & Conditions. If you do not agree, please do not use the platform."]),
            ("Platform role", ["Elegentra is a discovery platform for beauty and wellness businesses. We help users explore business profiles, services, galleries, locations, ratings, reviews, and contact options. Elegentra is not a marketplace for customer service payments."]),
            ("Customer use", ["Customers should verify service details, pricing, availability, timings, and booking terms directly with the salon or business. Elegentra may provide information and contact options, but the final service experience is between the customer and the business."]),
            ("Salon partner responsibilities", ["Salon partners are responsible for providing accurate business details, lawful images, correct service information, valid contact details, and any required documents for verification.", "Partners must not upload misleading, unsafe, unlawful, offensive, copied, or unauthorized content."]),
            ("Subscriptions", ["Approved salon partners may be required to maintain an active subscription after any applicable trial period. Subscription access, billing status, trial duration, grace periods, and founder benefits are controlled by Elegentra's platform rules."]),
            ("Reviews and content", ["Reviews, ratings, gallery images, service descriptions, and other public content should be honest, relevant, and respectful. Elegentra may moderate, hide, or remove content that appears abusive, fake, misleading, unlawful, or harmful."]),
            ("Account access", ["Users are responsible for keeping login access secure. Staff/admin access is restricted and may be suspended, blocked, or removed if misused."]),
            ("Limitation", ["Elegentra works to keep information useful and accurate, but we cannot guarantee that every listing, rating, service, opening time, or contact detail is always complete or current."]),
        ],
    },
    "cancellation-refund-policy": {
        "title": "Cancellation & Refund Policy",
        "eyebrow": "Billing",
        "description": "How subscription cancellation and refund handling works for Elegentra salon partners.",
        "updated": "22 September 2026",
        "sections": [
            ("Who this applies to", ["This policy applies to salon partners who subscribe to Elegentra. Customers browsing or contacting salons through Elegentra do not make service payments through Elegentra."]),
            ("Free trials", ["Eligible salon partners may receive a trial period before subscription billing begins. Trial duration and founder partner benefits are controlled by Elegentra and may depend on approval status and eligibility rules."]),
            ("Cancellation", ["Salon partners may request cancellation of their subscription. Cancellation affects future billing and may limit or remove partner features after the current paid period or applicable grace period."]),
            ("Refunds", ["Subscription payments are generally non-refundable once a billing period has started, unless required by law or approved by Elegentra after review. Duplicate payments, technical billing errors, or clearly accidental charges may be reviewed case by case."]),
            ("Failed payments and grace period", ["If a renewal payment fails, Elegentra may provide a short grace period before limiting subscription-based features. Access may be restored when payment succeeds."]),
            ("How to request help", ["For cancellation, payment, refund, or billing questions, contact Elegentra support with your salon name, registered email, payment reference if available, and a short explanation of the issue."]),
        ],
    },
    "contact-support": {
        "title": "Contact & Support",
        "eyebrow": "Support",
        "description": "Contact Elegentra for general questions, partner support, billing help, and platform assistance.",
        "updated": "22 September 2026",
        "sections": [
            ("General support", ["For general questions about Elegentra, partner onboarding, business profiles, or platform access, contact us at hello.elegentra@gmail.com."]),
            ("Billing support", ["For subscription or billing help, include your salon name, registered email address, and payment reference if available. This helps us find the correct account faster."]),
            ("Partner support", ["Salon partners can contact support for help with profile details, services, gallery uploads, subscription status, approval review, or account access."]),
            ("Response time", ["We try to respond as soon as possible. During launch and onboarding periods, response times may vary depending on support volume."]),
            ("Business address", ["Elegentra is preparing for launch in India. Formal business address and additional contact channels may be updated as operations expand."]),
        ],
    },
}


def legal_page(request, slug):
    page = LEGAL_PAGES.get(slug)
    if not page:
        raise Http404("Legal page not found.")
    return render(
        request,
        "legal_page.html",
        {
            "active_nav": "",
            "public_has_saloon": _user_has_saloon(request.user),
            "page": page,
            "seo": build_seo_payload(
                request,
                title=f"{page['title']} | Elegentra",
                description=page["description"],
                canonical_url=build_absolute_url(request, reverse(f"public_{slug.replace('-', '_')}")),
            ),
        },
    )

def robots_txt(request):
    sitemap_url = build_absolute_url(request, reverse("sitemap_xml"))
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /staff/",
        "Disallow: /accounts/",
        f"Sitemap: {sitemap_url}",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def sitemap_xml(request):
    entries = [
        (
            build_absolute_url(request, reverse("public_home")),
            None,
        ),
        (build_absolute_url(request, reverse("public_faq")), None),
        (build_absolute_url(request, reverse("public_privacy_policy")), None),
        (build_absolute_url(request, reverse("public_terms_and_conditions")), None),
        (build_absolute_url(request, reverse("public_cancellation_refund_policy")), None),
        (build_absolute_url(request, reverse("public_contact_support")), None),
    ]
    for place_url in _build_place_entries(request):
        entries.append((place_url, None))
    saloons = (
        _platform_visible_saloons(Saloon.objects.filter(
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
        ))
        .select_related("profile")
        .order_by("-updated_at")
    )
    for saloon in saloons:
        entries.append(
            (
                build_absolute_url(request, reverse("public_saloon", kwargs={"slug": saloon.slug})),
                saloon.updated_at.date().isoformat() if saloon.updated_at else None,
            )
        )

    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for location, lastmod in entries:
        xml_parts.append("<url>")
        xml_parts.append(f"<loc>{escape(location)}</loc>")
        if lastmod:
            xml_parts.append(f"<lastmod>{lastmod}</lastmod>")
        xml_parts.append("</url>")
    xml_parts.append("</urlset>")
    return HttpResponse("".join(xml_parts), content_type="application/xml")



def _static_image_path(filename):
    static_root = Path(django_settings.STATIC_ROOT) if django_settings.STATIC_ROOT else django_settings.BASE_DIR / "static"
    path = static_root / "images" / filename
    if not path.exists():
        path = django_settings.BASE_DIR / "static" / "images" / filename
    return path


def favicon_ico(request):
    icon_path = _static_image_path("favicon.ico")
    if not icon_path.is_file():
        raise Http404("Favicon not found.")
    return FileResponse(icon_path.open("rb"), content_type="image/x-icon")

def favicon_png(request):
    icon_path = _static_image_path("favicon.png")
    if not icon_path.is_file():
        raise Http404("Favicon not found.")
    return FileResponse(icon_path.open("rb"), content_type="image/png")

def site_webmanifest(request):
    manifest = {
        "name": "Elegentra",
        "short_name": "Elegentra",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f7f0ea",
        "theme_color": "#f7f0ea",
        "icons": [
            {
                "src": "/favicon.png",
                "sizes": "256x256",
                "type": "image/png",
            }
        ],
    }
    import json
    return HttpResponse(json.dumps(manifest), content_type="application/manifest+json")
