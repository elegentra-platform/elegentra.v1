from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils.text import slugify
from django.urls import reverse
from django.db.models import Q
from saloons.models import Saloon
from .models import Service, ServiceCategory


@login_required
def service_list(request):
    saloon = get_object_or_404(Saloon, owner=request.user)

    if saloon.approval_status != Saloon.APPROVAL_APPROVED:
        messages.error(request, "Services can be managed only after approval.")
        return redirect("saloon_dashboard", username=request.user.username)

    services = Service.objects.filter(
    saloon=saloon,
    is_deleted=False
)

    return render(request, "saloons/dashboard/services.html", {
        "saloon": saloon,
        "services": services,
    })


@login_required
def service_create(request):
    saloon = get_object_or_404(Saloon, owner=request.user)
    return_to = request.POST.get("return_to") or request.GET.get("return_to") or "services"
    from_gallery = return_to == "gallery_create"

    if saloon.approval_status != Saloon.APPROVAL_APPROVED:
        messages.error(request, "You cannot add services before approval.")
        return redirect("saloon_dashboard", username=request.user.username)

    categories = ServiceCategory.objects.filter(
        is_active=True
    ).filter(
        Q(is_approved=True) | Q(created_by_saloon=saloon)
    )
    gallery_create_url = reverse("saloon_gallery_create", kwargs={"username": request.user.username})
    service_create_url = reverse("service_create")

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        price = request.POST.get("price")
        category_id = request.POST.get("category")
        new_category = request.POST.get("new_category", "").strip()
        description = request.POST.get("description", "")
        offer_price = request.POST.get("offer_price") or None
        is_visible = request.POST.get("is_visible") == "on"

        if not name or not price:
            messages.error(request, "Name and price are required.")
            redirect_url = service_create_url + "?return_to=gallery_create" if from_gallery else service_create_url
            return redirect(redirect_url)

        # CATEGORY LOGIC
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
        elif category_id:
            category = get_object_or_404(ServiceCategory, id=category_id)
        else:
            messages.error(request, "Please select or add a category.")
            redirect_url = service_create_url + "?return_to=gallery_create" if from_gallery else service_create_url
            return redirect(redirect_url)

        service = Service.objects.create(
            saloon=saloon,
            name=name,
            price=price,
            offer_price=offer_price,
            category=category,
            description=description,
            is_visible=is_visible,
            is_active=True,
            added_during_onboarding=False,
        )

        messages.success(request, "Service added successfully.")
        if from_gallery:
            return redirect(f"{gallery_create_url}?selected_service={service.id}")
        return redirect(
            "saloon_dashboard_services",
            username=request.user.username
        )

    back_url = gallery_create_url if from_gallery else reverse("saloon_dashboard_services", kwargs={"username": request.user.username})

    return render(request, "services/service_form.html", {
        "saloon": saloon,
        "categories": categories,
        "active_tab": "mysaloon" if from_gallery else "services",
        "show_back_button": True,
        "back_url": back_url,
        "back_parent_label": "Gallery" if from_gallery else "Services",
        "back_label": "Add Service",
        "return_to": return_to,
    })


@login_required
def service_edit(request, service_id):
    saloon = get_object_or_404(Saloon, owner=request.user)

    if saloon.approval_status != Saloon.APPROVAL_APPROVED:
        messages.error(request, "Services can be managed only after approval.")
        return redirect("saloon_dashboard", username=request.user.username)

    service = get_object_or_404(Service, id=service_id, saloon=saloon)
    categories = ServiceCategory.objects.filter(
        is_active=True
    ).filter(
        Q(is_approved=True) | Q(created_by_saloon=saloon)
    )

    if request.method == "POST":
        service.name = request.POST.get("name", service.name)
        service.price = request.POST.get("price", service.price)
        service.offer_price = request.POST.get("offer_price") or None
        service.description = request.POST.get("description", service.description)
        service.is_visible = request.POST.get("is_visible") == "on"

        category_id = request.POST.get("category")
        if category_id:
            service.category = get_object_or_404(ServiceCategory, id=category_id)

        service.save()
        messages.success(request, "Service updated.")
        return redirect(
            "saloon_dashboard_services",
            username=request.user.username
        )


    return render(request, "services/service_form.html", {
        "saloon": saloon,
        "service": service,
        "categories": categories,
        "active_tab": "services",
        "show_back_button": True,
        "back_url": reverse("saloon_dashboard_services", kwargs={"username": request.user.username}),
        "back_parent_label": "Services",
        "back_label": "Edit",
    })



@login_required
@require_POST
def service_delete(request, service_id):
    saloon = get_object_or_404(Saloon, owner=request.user)

    service = Service.objects.filter(
        id=service_id,
        saloon=saloon
    ).first()

    if not service:
        messages.error(request, "Service not found.")
        return redirect("saloon_dashboard_services", username=request.user.username)

    if not service.is_deleted:
        service.is_deleted = True
        service.deleted_at = timezone.now()
        service.save()

    # 🔑 STORE FOR UNDO
    request.session["undo_service_ids"] = [service.id]

    messages.success(request, "Service deleted.")
    return redirect("saloon_dashboard_services", username=request.user.username)


@login_required
@require_POST
def service_bulk_delete(request):
    saloon = get_object_or_404(Saloon, owner=request.user)

    service_ids = request.POST.getlist("service_ids")

    if not service_ids:
        messages.error(request, "No services selected.")
        return redirect("saloon_dashboard_services", username=request.user.username)

    Service.objects.filter(
        id__in=service_ids,
        saloon=saloon,
        is_deleted=False
    ).update(
        is_deleted=True,
        deleted_at=timezone.now()
    )

    # 🔑 STORE FOR UNDO
    request.session["undo_service_ids"] = service_ids

    messages.success(request, f"{len(service_ids)} services deleted.")
    return redirect("saloon_dashboard_services", username=request.user.username)


@login_required
@require_POST
def service_undo_delete(request):
    saloon = get_object_or_404(Saloon, owner=request.user)

    service_ids = request.POST.getlist("service_ids")

    Service.objects.filter(
        id__in=service_ids,
        saloon=saloon,
        is_deleted=True
    ).update(
        is_deleted=False,
        deleted_at=None
    )

    messages.success(request, "Services restored.")
    return redirect("saloon_dashboard_services", username=request.user.username)
