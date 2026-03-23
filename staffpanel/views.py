from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.sessions.models import Session
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from saloons.models import Saloon, SaloonRejectionReason
from services.models import MainCategory, ServiceCategory


def _staff_check(user):
    return user.is_authenticated and user.is_staff


staff_required = [login_required, user_passes_test(_staff_check)]


def _online_staff_count():
    active_sessions = Session.objects.filter(expire_date__gte=timezone.now())
    user_ids = set()
    for session in active_sessions:
        data = session.get_decoded()
        user_id = data.get("_auth_user_id")
        if user_id:
            user_ids.add(user_id)

    if not user_ids:
        return 0

    User = get_user_model()
    return User.objects.filter(id__in=user_ids, is_staff=True, is_active=True).count()


@login_required
@user_passes_test(_staff_check)
def dashboard(request):
    context = {
        "active_tab": "dashboard",
        "pending_saloon_approvals": Saloon.objects.filter(
            approval_status=Saloon.APPROVAL_PENDING
        ).count(),
        "pending_service_category_approvals": ServiceCategory.objects.filter(
            is_approved=False,
            is_active=True,
        ).count(),
        "online_staff_count": _online_staff_count(),
        "saloon_status_breakdown": (
            Saloon.objects.values("approval_status")
            .annotate(total=Count("id"))
            .order_by("approval_status")
        ),
    }
    return render(request, "staffpanel/dashboard.html", context)


@login_required
@user_passes_test(_staff_check)
def saloon_queue(request):
    saloons = (
        Saloon.objects.filter(approval_status=Saloon.APPROVAL_PENDING)
        .select_related("profile", "owner")
        .order_by("-updated_at")
    )
    return render(
        request,
        "staffpanel/saloon_queue.html",
        {
            "active_tab": "saloons",
            "saloons": saloons,
        },
    )


@login_required
@user_passes_test(_staff_check)
def saloon_review(request, saloon_id):
    saloon = get_object_or_404(
        Saloon.objects.select_related("owner", "profile", "verification"),
        id=saloon_id,
    )

    verification = None
    try:
        verification = saloon.verification
    except Exception:
        verification = None

    return render(
        request,
        "staffpanel/saloon_review.html",
        {
            "active_tab": "saloons",
            "saloon": saloon,
            "verification": verification,
            "rejection_history": saloon.rejection_reasons.order_by("-created_at")[:10],
        },
    )


@require_POST
@login_required
@user_passes_test(_staff_check)
def saloon_approve(request, saloon_id):
    saloon = get_object_or_404(Saloon, id=saloon_id)
    saloon.approval_status = Saloon.APPROVAL_APPROVED
    saloon.is_active = True
    saloon.save(update_fields=["approval_status", "is_active", "updated_at"])
    messages.success(request, "Saloon approved.")
    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("staffpanel:saloon_queue")


@require_POST
@login_required
@user_passes_test(_staff_check)
def saloon_reject(request, saloon_id):
    saloon = get_object_or_404(Saloon, id=saloon_id)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "Rejection reason is required.")
        return redirect("staffpanel:saloon_queue")

    saloon.approval_status = Saloon.APPROVAL_REJECTED
    saloon.is_active = False
    saloon.save(update_fields=["approval_status", "is_active", "updated_at"])
    SaloonRejectionReason.objects.create(
        saloon=saloon,
        field_key="approval_status",
        message=reason,
    )
    messages.success(request, "Saloon rejected with reason.")
    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("staffpanel:saloon_queue")


@login_required
@user_passes_test(_staff_check)
def category_queue(request):
    pending_categories = (
        ServiceCategory.objects.filter(is_approved=False, is_active=True)
        .select_related("created_by_saloon", "main_category")
        .order_by("-created_at")
    )
    all_categories = (
        ServiceCategory.objects.filter(is_active=True)
        .select_related("created_by_saloon", "main_category")
        .order_by("name")
    )
    main_categories = MainCategory.objects.filter(is_active=True).order_by("display_order", "name")
    return render(
        request,
        "staffpanel/category_queue.html",
        {
            "active_tab": "categories",
            "categories": pending_categories,
            "all_categories": all_categories,
            "pending_count": pending_categories.count(),
            "main_categories": main_categories,
        },
    )


@require_POST
@login_required
@user_passes_test(_staff_check)
def category_map_main(request, category_id):
    category = get_object_or_404(ServiceCategory, id=category_id)
    main_category_id = request.POST.get("main_category_id")
    if not main_category_id:
        category.main_category = None
        category.save(update_fields=["main_category"])
        messages.success(request, "Category mapping cleared.")
        next_url = request.POST.get("next")
        if next_url:
            return redirect(next_url)
        return redirect("staffpanel:category_queue")

    main_category = get_object_or_404(MainCategory, id=main_category_id, is_active=True)
    category.main_category = main_category
    category.save(update_fields=["main_category"])
    messages.success(request, "Category mapped to main category.")
    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("staffpanel:category_queue")


@require_POST
@login_required
@user_passes_test(_staff_check)
def category_bulk_map_main(request):
    categories = ServiceCategory.objects.filter(is_active=True)
    main_categories = {
        str(main.id): main
        for main in MainCategory.objects.filter(is_active=True)
    }

    updated = 0
    for category in categories:
        key = f"map_{category.id}"
        selected = request.POST.get(key, "")
        new_main = main_categories.get(selected) if selected else None
        if category.main_category_id != (new_main.id if new_main else None):
            category.main_category = new_main
            category.save(update_fields=["main_category"])
            updated += 1

    messages.success(request, f"Saved mapping changes for {updated} category(s).")
    return redirect("staffpanel:category_queue")


@require_POST
@login_required
@user_passes_test(_staff_check)
def category_approve(request, category_id):
    category = get_object_or_404(ServiceCategory, id=category_id, is_active=True)
    if not category.main_category:
        messages.error(request, "Map this category to a main category before approving.")
        return redirect("staffpanel:category_queue")

    category.is_approved = True
    category.save(update_fields=["is_approved"])
    messages.success(request, "Service category approved.")
    return redirect("staffpanel:category_queue")


@require_POST
@login_required
@user_passes_test(_staff_check)
def category_reject(request, category_id):
    category = get_object_or_404(ServiceCategory, id=category_id)
    category.is_active = False
    category.save(update_fields=["is_active"])
    messages.success(request, "Service category rejected (disabled).")
    return redirect("staffpanel:category_queue")
