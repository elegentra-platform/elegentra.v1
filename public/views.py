import math
import mimetypes
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings as django_settings
from django.core.paginator import Paginator
from django.db.models import Q, Avg, Count
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse

from core.seo import build_absolute_url, build_seo_payload
from public.models import FavoriteSaloon
from saloons.models import Saloon
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


def _serialize_saloons(saloons, user_lat=None, user_lng=None, favorite_slugs=None):
    favorite_slugs = favorite_slugs or set()
    result = []
    for saloon in saloons:
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
        location = ", ".join([item for item in [locality, city] if item]) or "Nearby"
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
                "is_favorited": saloon.slug in favorite_slugs,
                "distance_value": _clean_float(distance_label.split()[0]) if "km away" in distance_label else None,
            }
        )

    if user_lat is not None and user_lng is not None:
        result.sort(key=lambda item: item["distance_value"] if item["distance_value"] is not None else 999999)

    return result


def _favorite_card_payload(favorite):
    saloon = favorite.saloon
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
    location = ", ".join([item for item in [locality, city] if item]) or "Nearby"
    avg_rating = getattr(favorite, "avg_rating", None)
    review_count = getattr(favorite, "review_count", 0) or 0

    return {
        "id": favorite.id,
        "name": _saloon_display_name(saloon),
        "slug": saloon.slug,
        "image": image,
        "location": location,
        "rating": round(float(avg_rating), 1) if avg_rating else None,
        "review_count": review_count,
    }


def _home_saloons(user_lat=None, user_lng=None):
    saloons = (
        Saloon.objects.filter(
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
        )
        .annotate(avg_rating=Avg("reviews__rating", filter=Q(reviews__is_visible=True)))
        .annotate(review_count=Count("reviews", filter=Q(reviews__is_visible=True)))
        .select_related("profile")
        .prefetch_related("services")
        .order_by("-updated_at", "-id")[:12]
    )
    return _serialize_saloons(saloons, user_lat=user_lat, user_lng=user_lng)


def _search_saloons(query, category_slug):
    saloons = (
        Saloon.objects.filter(
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
        )
        .annotate(avg_rating=Avg("reviews__rating", filter=Q(reviews__is_visible=True)))
        .annotate(review_count=Count("reviews", filter=Q(reviews__is_visible=True)))
        .select_related("profile")
        .prefetch_related("services")
    )

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
            | Q(profile__city__icontains=query)
            | Q(profile__locality__icontains=query)
            | Q(services__name__icontains=query)
            | Q(services__category__name__icontains=query)
        )

    return saloons.distinct().order_by("-updated_at", "-id")


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
    is_vendor = _user_has_saloon(request.user)
    user_lat, user_lng = _request_coords(request)
    categories = _public_categories()
    favorite_slugs = set()
    if request.user.is_authenticated:
        favorite_slugs = set(
            FavoriteSaloon.objects.filter(user=request.user).values_list("saloon__slug", flat=True)
        )
    popular_saloons = _serialize_saloons(
        Saloon.objects.filter(
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
        )
        .annotate(avg_rating=Avg("reviews__rating", filter=Q(reviews__is_visible=True)))
        .annotate(review_count=Count("reviews", filter=Q(reviews__is_visible=True)))
        .select_related("profile")
        .prefetch_related("services")
        .order_by("-updated_at", "-id")[:12],
        user_lat=user_lat,
        user_lng=user_lng,
        favorite_slugs=favorite_slugs,
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
            "page_obj": page_obj,
            "user_lat": user_lat,
            "user_lng": user_lng,
            "seo": _search_seo(request, query, selected_category, categories),
        },
    )


def favorites(request):
    favorite_cards = []
    if request.user.is_authenticated:
        favorites_qs = (
            FavoriteSaloon.objects.filter(
                user=request.user,
                saloon__is_active=True,
                saloon__approval_status=Saloon.APPROVAL_APPROVED,
            )
            .select_related("saloon__profile")
            .prefetch_related("saloon__services")
            .annotate(avg_rating=Avg("saloon__reviews__rating", filter=Q(saloon__reviews__is_visible=True)))
            .annotate(review_count=Count("saloon__reviews", filter=Q(saloon__reviews__is_visible=True)))
        )
        favorite_cards = [_favorite_card_payload(item) for item in favorites_qs]

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
        )
    ]
    saloons = (
        Saloon.objects.filter(
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
        )
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
