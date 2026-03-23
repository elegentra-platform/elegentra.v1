import math

from django.core.paginator import Paginator
from django.db.models import Q, Avg, Count
from django.shortcuts import render
from django.templatetags.static import static

from saloons.models import Saloon
from services.models import MainCategory, ServiceCategory


DEFAULT_SALOON_IMAGE = static("services/images/hero.webp")


def _user_has_saloon(user):
    return bool(user.is_authenticated and Saloon.objects.filter(owner=user).exists())


def _public_categories():
    main_categories = MainCategory.objects.filter(is_active=True).order_by("display_order", "name")
    if main_categories.exists():
        return [
            {
                "slug": category.slug,
                "name": category.name,
                "icon_url": category.icon_image.url if category.icon_image else "",
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
            "icon_url": (
                category.main_category.icon_image.url
                if category.main_category and category.main_category.icon_image
                else ""
            ),
        }
        for category in categories
    ]


def _clean_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _serialize_saloons(saloons, user_lat=None, user_lng=None):
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
                "name": saloon.name or getattr(profile, "saloon_name", "") or "Saloon",
                "distance": distance_label,
                "rating": rating_label,
                "image": image,
                "slug": saloon.slug,
                "distance_value": _clean_float(distance_label.split()[0]) if "km away" in distance_label else None,
            }
        )

    if user_lat is not None and user_lng is not None:
        result.sort(key=lambda item: item["distance_value"] if item["distance_value"] is not None else 999999)

    return result


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


def home(request):
    is_vendor = _user_has_saloon(request.user)
    user_lat, user_lng = _request_coords(request)
    popular_saloons = _home_saloons(user_lat=user_lat, user_lng=user_lng)

    return render(
        request,
        "home.html",
        {
            "is_vendor": is_vendor,
            "public_has_saloon": is_vendor,
            "active_nav": "home",
            "categories": _public_categories(),
            "popular_saloons": popular_saloons,
            "nearby_saloons": popular_saloons[:8],
            "user_lat": user_lat,
            "user_lng": user_lng,
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
    search_results = _serialize_saloons(page_obj.object_list, user_lat=user_lat, user_lng=user_lng)

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
        },
    )


def favorites(request):
    return render(
        request,
        "favorite.html",
        {
            "active_nav": "favorites",
            "public_has_saloon": _user_has_saloon(request.user),
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
        },
    )


def settings(request):
    return render(
        request,
        "settings.html",
        {
            "active_nav": "settings",
            "public_has_saloon": _user_has_saloon(request.user),
        },
    )
