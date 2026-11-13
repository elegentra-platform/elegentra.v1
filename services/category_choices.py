from django.db.models import Prefetch, Q

from .models import MainCategory, ServiceCategory


def category_choice_context(saloon):
    approved_subcategories = ServiceCategory.objects.filter(
        is_active=True,
        is_approved=True,
    ).order_by("name")
    main_categories = MainCategory.objects.filter(is_active=True).prefetch_related(
        Prefetch("service_categories", queryset=approved_subcategories, to_attr="approved_subcategories")
    ).order_by("display_order", "name")
    own_pending_categories = ServiceCategory.objects.filter(
        is_active=True,
        is_approved=False,
        created_by_saloon=saloon,
    ).order_by("name")
    return main_categories, own_pending_categories


def resolve_category_choice(value, saloon):
    value = (value or "").strip()
    if not value or ":" not in value:
        return None, None

    choice_type, object_id = value.split(":", 1)
    if not object_id.isdigit():
        return None, None

    if choice_type == "main":
        main = MainCategory.objects.filter(id=object_id, is_active=True).first()
        return None, main

    if choice_type == "category":
        category = ServiceCategory.objects.filter(id=object_id, is_active=True).filter(
            Q(is_approved=True, main_category__is_active=True) | Q(created_by_saloon=saloon)
        ).first()
        return category, None

    return None, None


def category_choice_value(service):
    if service.category_id:
        return f"category:{service.category_id}"
    if service.main_category_id:
        return f"main:{service.main_category_id}"
    return ""
