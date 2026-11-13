from functools import wraps
from datetime import datetime
from pathlib import Path

from django.contrib import messages
from django.core.files.storage import default_storage
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.contrib.sessions.models import Session
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from PIL import Image, UnidentifiedImageError

from saloons.models import GalleryPost, Saloon, SaloonRejectionReason
from services.models import MainCategory, Service, ServiceCategory
from plans.billing import assign_subscription_trial_on_approval
from plans.models import SaloonSubscription, SubscriptionPaymentHistory, SubscriptionWebhookEvent
from .models import StaffPanelSetting, StaffProfile

TEST_SALOON_MODE_KEY = "test_saloon_mode"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _format_file_size(size_bytes):
    if size_bytes is None:
        return "-"
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return "-"
    units = ["B", "KB", "MB", "GB"]
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.2f} {units[unit_index]}"


def _media_file_row(file_field, label, source=""):
    if not file_field or not getattr(file_field, "name", ""):
        return None

    file_name = file_field.name
    display_name = Path(file_name).name
    suffix = Path(display_name).suffix.lower()
    file_format = suffix.lstrip(".").upper() or "-"
    is_image = suffix in IMAGE_EXTENSIONS
    size_bytes = None
    dimensions = "-"
    url = ""

    try:
        size_bytes = default_storage.size(file_name)
    except Exception:
        size_bytes = None

    try:
        url = file_field.url
    except Exception:
        url = ""

    if is_image:
        try:
            with default_storage.open(file_name, "rb") as image_file:
                image = Image.open(image_file)
                width, height = image.size
                dimensions = f"{width} x {height}"
                if image.format:
                    file_format = image.format.upper()
        except (OSError, UnidentifiedImageError, ValueError):
            dimensions = "-"

    return {
        "label": label,
        "source": source,
        "name": display_name,
        "path": file_name,
        "format": file_format,
        "size_bytes": size_bytes,
        "size": _format_file_size(size_bytes),
        "dimensions": dimensions,
        "url": url,
        "is_image": is_image,
    }


def _saloon_media_rows(saloon):
    rows = []

    def add(file_field, label, source=""):
        row = _media_file_row(file_field, label, source)
        if row:
            rows.append(row)

    add(saloon.banner_image, "Banner image", "Salon branding")

    for service in Service.objects.filter(saloon=saloon).only("name", "image"):
        add(service.image, f"Service image - {service.name}", "Service")

    for post in saloon.gallery_posts.prefetch_related("media").all():
        post_title = post.title or f"Gallery post #{post.id}"
        for media in post.media.all():
            add(media.file, post_title, f"Gallery {media.get_media_type_display()}")

    return rows

def _saloon_verification_rows(verification):
    if not verification:
        return []

    rows = []

    def add(file_field, label):
        row = _media_file_row(file_field, label, "Verification file")
        if row:
            rows.append(row)

    add(verification.owner_id_proof, "Owner ID proof")
    add(verification.inside_image_1, "Inside image 1")
    add(verification.inside_image_2, "Inside image 2")
    add(verification.inside_image_3, "Inside image 3")
    add(verification.outside_image_1, "Outside image 1")
    add(verification.outside_image_2, "Outside image 2")
    add(verification.outside_image_3, "Outside image 3")
    return rows

def _is_test_saloon_mode_enabled():
    return StaffPanelSetting.get_bool(TEST_SALOON_MODE_KEY, default=False)


def _staff_role(user):
    if not user.is_authenticated or not user.is_staff:
        return ""
    if user.is_superuser:
        return StaffProfile.ROLE_SUPER_ADMIN
    profile = getattr(user, "staff_profile", None)
    if profile:
        return profile.role
    return StaffProfile.ROLE_STAFF


def _is_admin_level(user):
    return _staff_role(user) in {StaffProfile.ROLE_ADMIN, StaffProfile.ROLE_SUPER_ADMIN}


def _is_super_admin_level(user):
    return _staff_role(user) == StaffProfile.ROLE_SUPER_ADMIN


def _staff_context(request, extra=None):
    role = _staff_role(request.user)
    context = {
        "staff_role": role,
        "is_staff_admin": role in {StaffProfile.ROLE_ADMIN, StaffProfile.ROLE_SUPER_ADMIN},
        "is_staff_super_admin": role == StaffProfile.ROLE_SUPER_ADMIN,
    }
    if extra:
        context.update(extra)
    return context


def staff_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("staffpanel:staff_login")
        if not request.user.is_staff:
            messages.error(request, "You do not have staff access.")
            return redirect("staffpanel:staff_login")
        if not request.user.is_superuser:
            profile = getattr(request.user, "staff_profile", None)
            if not profile:
                messages.error(request, "Staff profile is missing.")
                return redirect("staffpanel:staff_login")
            if profile.status == StaffProfile.STATUS_PENDING:
                messages.error(request, "Enter your staff invite code to activate access.")
                return redirect(f"/staff/login/?email={request.user.email}")
            if profile.status in {StaffProfile.STATUS_SUSPENDED, StaffProfile.STATUS_BLOCKED, StaffProfile.STATUS_REMOVED}:
                logout(request)
                messages.error(request, "Your staff access is not active. Contact an admin.")
                return redirect("staffpanel:staff_login")
        return view_func(request, *args, **kwargs)

    return _wrapped


def admin_required(view_func):
    @staff_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not _is_admin_level(request.user):
            messages.error(request, "Admin access is required.")
            return redirect("staffpanel:dashboard")
        return view_func(request, *args, **kwargs)

    return _wrapped


def super_admin_required(view_func):
    @staff_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not _is_super_admin_level(request.user):
            messages.error(request, "Super admin access is required.")
            return redirect("staffpanel:dashboard")
        return view_func(request, *args, **kwargs)

    return _wrapped


def staff_login(request):
    if request.user.is_authenticated and request.user.is_staff:
        profile = getattr(request.user, "staff_profile", None)
        if request.user.is_superuser or (profile and profile.status == StaffProfile.STATUS_ACTIVE):
            return redirect("staffpanel:dashboard")
        if profile and profile.status in {StaffProfile.STATUS_SUSPENDED, StaffProfile.STATUS_BLOCKED, StaffProfile.STATUS_REMOVED}:
            logout(request)
            messages.error(request, "Your staff access is not active. Contact an admin.")
            return redirect("staffpanel:staff_login")

    email = request.POST.get("email", "").strip().lower() if request.method == "POST" else request.GET.get("email", "").strip().lower()
    step = "code" if email else "email"
    staff_user = None
    if email:
        User = get_user_model()
        staff_user = User.objects.filter(email__iexact=email, is_staff=True, is_active=True).first()
        if request.method == "POST" and "continue" in request.POST:
            if not staff_user:
                messages.error(request, "No active staff account found for this email.")
                step = "email"
            elif _staff_role(staff_user) != StaffProfile.ROLE_STAFF:
                messages.error(request, "Admin accounts use password login.")
                return redirect("staffpanel:admin_login")
            else:
                step = "code"
        elif request.method == "POST" and "login" in request.POST:
            code = request.POST.get("code", "").strip().upper()
            profile = getattr(staff_user, "staff_profile", None) if staff_user else None
            if not staff_user or not profile or profile.role != StaffProfile.ROLE_STAFF:
                messages.error(request, "Use a valid staff email.")
                step = "email"
            elif profile.status in {StaffProfile.STATUS_SUSPENDED, StaffProfile.STATUS_BLOCKED, StaffProfile.STATUS_REMOVED}:
                messages.error(request, "This staff account is not active.")
                step = "email"
            elif profile.check_invite_code(code):
                profile.last_code_login_at = timezone.now()
                profile.status = StaffProfile.STATUS_ACTIVE
                profile.save(update_fields=["last_code_login_at", "status", "updated_at"])
                login(request, staff_user, backend="django.contrib.auth.backends.ModelBackend")
                return redirect("staffpanel:dashboard")
            else:
                messages.error(request, "Invalid staff code.")
                step = "code"

    return render(request, "staffpanel/staff_login.html", {"email": email, "step": step, "staff_user": staff_user})


def admin_login(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("staffpanel:dashboard")

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        User = get_user_model()
        user = User.objects.filter(email__iexact=email, is_staff=True, is_active=True).first()
        if user:
            user = authenticate(request, username=user.get_username(), password=password)
        if user and user.is_staff and _staff_role(user) in {StaffProfile.ROLE_ADMIN, StaffProfile.ROLE_SUPER_ADMIN}:
            login(request, user)
            return redirect("staffpanel:dashboard")
        messages.error(request, "Invalid admin email or password.")

    return render(request, "staffpanel/admin_login.html")


def staff_logout(request):
    logout(request)
    return redirect("staffpanel:staff_login")


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


@staff_required
def dashboard(request):
    context = _staff_context(request, {
        "active_tab": "dashboard",
        "pending_saloon_approvals": Saloon.objects.filter(
            approval_status=Saloon.APPROVAL_PENDING
        ).count(),
        "pending_service_category_approvals": ServiceCategory.objects.filter(
            is_approved=False,
            is_active=True,
        ).count(),
        "total_saloons": Saloon.objects.count(),
        "online_staff_count": _online_staff_count(),
        "saloon_status_breakdown": (
            Saloon.objects.values("approval_status")
            .annotate(total=Count("id"))
            .order_by("approval_status")
        ),
        "subscription_total": SaloonSubscription.objects.count(),
        "payment_total": SubscriptionPaymentHistory.objects.count(),
        "recent_saloons": Saloon.objects.select_related("owner").order_by("-updated_at")[:6],
        "recent_payments": SubscriptionPaymentHistory.objects.select_related("saloon").order_by("-created_at")[:4],
    })
    return render(request, "staffpanel/dashboard.html", context)


@staff_required
def saloon_queue(request):
    saloons = (
        Saloon.objects.filter(approval_status=Saloon.APPROVAL_PENDING)
        .select_related("profile", "owner")
        .order_by("-updated_at")
    )
    return render(
        request,
        "staffpanel/saloon_queue.html",
        _staff_context(request, {
            "active_tab": "saloons",
            "saloons": saloons,
        }),
    )


@staff_required
def saloon_list(request):
    status = request.GET.get("status", "").strip()
    month = request.GET.get("month", "").strip()
    sort = request.GET.get("sort", "newest").strip()
    query = request.GET.get("q", "").strip()

    saloons = Saloon.objects.select_related("profile", "owner").all()

    valid_statuses = {
        Saloon.APPROVAL_SUBMITTING,
        Saloon.APPROVAL_PENDING,
        Saloon.APPROVAL_APPROVED,
        Saloon.APPROVAL_REJECTED,
    }
    if status in valid_statuses:
        saloons = saloons.filter(approval_status=status)

    selected_month = None
    if month:
        try:
            selected_month = datetime.strptime(month, "%Y-%m")
            saloons = saloons.filter(
                created_at__year=selected_month.year,
                created_at__month=selected_month.month,
            )
        except ValueError:
            selected_month = None

    if query:
        saloons = saloons.filter(
            Q(name__icontains=query)
            | Q(owner__email__icontains=query)
            | Q(whatsapp_number__icontains=query)
            | Q(profile__saloon_name__icontains=query)
            | Q(profile__owner_full_name__icontains=query)
        )

    if sort == "oldest":
        saloons = saloons.order_by("created_at", "id")
    else:
        sort = "newest"
        saloons = saloons.order_by("-created_at", "-id")

    paginator = Paginator(saloons, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "staffpanel/saloon_list.html",
        _staff_context(request, {
            "active_tab": "all_saloons",
            "page_obj": page_obj,
            "saloons": page_obj.object_list,
            "status": status,
            "month": month if selected_month else "",
            "sort": sort,
            "query": query,
            "status_choices": Saloon.APPROVAL_STATUS_CHOICES,
            "test_saloon_mode_enabled": _is_test_saloon_mode_enabled(),
        }),
    )


@staff_required
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
        _staff_context(request, {
            "active_tab": "saloons",
            "saloon": saloon,
            "verification": verification,
            "services": Service.objects.filter(saloon=saloon).select_related("category").order_by("-created_at"),
            "gallery_posts": saloon.gallery_posts.prefetch_related("media").all()[:12],
            "rejection_history": saloon.rejection_reasons.order_by("-created_at")[:10],
            "test_saloon_mode_enabled": _is_test_saloon_mode_enabled(),
        }),
    )


@staff_required
def saloon_media_sizes(request, saloon_id):
    saloon = get_object_or_404(
        Saloon.objects.select_related("owner", "profile", "verification"),
        id=saloon_id,
    )
    try:
        verification = saloon.verification
    except Exception:
        verification = None

    media_rows = _saloon_media_rows(saloon)
    verification_rows = _saloon_verification_rows(verification)
    total_bytes = sum(row["size_bytes"] or 0 for row in media_rows + verification_rows)

    return render(
        request,
        "staffpanel/saloon_media_sizes.html",
        _staff_context(request, {
            "active_tab": "saloons",
            "saloon": saloon,
            "media_rows": media_rows,
            "verification_rows": verification_rows,
            "total_size": _format_file_size(total_bytes),
            "total_files": len(media_rows) + len(verification_rows),
        }),
    )
@staff_required
def saloon_gallery_detail(request, saloon_id, post_id):
    saloon = get_object_or_404(
        Saloon.objects.select_related("owner", "profile"),
        id=saloon_id,
    )
    profile = getattr(saloon, "profile", None)
    display_name = profile.saloon_name if profile and profile.saloon_name else saloon.name or "Salon"

    post = get_object_or_404(
        GalleryPost.objects.select_related("service", "service__category").prefetch_related("media"),
        id=post_id,
        saloon=saloon,
    )
    if not post.image_media:
        messages.error(request, "This gallery post does not have an image preview.")
        return redirect("staffpanel:saloon_review", saloon_id=saloon.id)

    posts = [
        item
        for item in saloon.gallery_posts.select_related("service", "service__category").prefetch_related("media").all()
        if item.image_media
    ]
    post_ids = [item.id for item in posts]
    try:
        index = post_ids.index(post.id)
    except ValueError:
        index = 0

    prev_post = posts[index - 1] if index > 0 else None
    next_post = posts[index + 1] if index + 1 < len(posts) else None

    return render(
        request,
        "staffpanel/saloon_gallery_detail.html",
        _staff_context(request, {
            "active_tab": "saloons",
            "saloon": saloon,
            "profile": profile,
            "display_name": display_name,
            "post": post,
            "ordered_posts": posts,
            "selected_post_id": post.id,
            "prev_post": prev_post,
            "next_post": next_post,
            "back_url": request.GET.get("next") or reverse("staffpanel:saloon_review", kwargs={"saloon_id": saloon.id}) + "#gallery",
        }),
    )
@require_POST
@staff_required
def gallery_post_moderate(request, saloon_id, post_id, action):
    saloon = get_object_or_404(Saloon, id=saloon_id)
    post = get_object_or_404(GalleryPost, id=post_id, saloon=saloon)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "Moderation reason is required.")
        return redirect("staffpanel:saloon_review", saloon_id=saloon.id)

    if action == "hide":
        post.is_hidden = True
        post.moderation_reason = reason
        post.moderated_by = request.user
        post.moderated_at = timezone.now()
        post.save(update_fields=["is_hidden", "moderation_reason", "moderated_by", "moderated_at"])
        SaloonRejectionReason.objects.create(
            saloon=saloon,
            field_key=f"gallery_post:{post.id}:hide",
            message=reason,
        )
        messages.success(request, "Gallery post hidden with reason.")
    elif action == "delete":
        SaloonRejectionReason.objects.create(
            saloon=saloon,
            field_key=f"gallery_post:{post.id}:delete",
            message=reason,
        )
        post.delete()
        messages.success(request, "Gallery post deleted with reason.")
    else:
        messages.error(request, "Unknown gallery moderation action.")
    return redirect("staffpanel:saloon_review", saloon_id=saloon.id)


@require_POST
@staff_required
def saloon_approve(request, saloon_id):
    saloon = get_object_or_404(Saloon, id=saloon_id)
    was_approved = saloon.approval_status == Saloon.APPROVAL_APPROVED
    update_fields = ["approval_status", "is_active", "updated_at"]

    if not was_approved and _is_test_saloon_mode_enabled() and request.POST.get("is_test_saloon") == "1":
        saloon.is_test_saloon = True
        saloon.test_saloon_marked_at = timezone.now()
        saloon.test_saloon_marked_by = request.user
        update_fields.extend(["is_test_saloon", "test_saloon_marked_at", "test_saloon_marked_by"])

    saloon.approval_status = Saloon.APPROVAL_APPROVED
    saloon.is_active = True
    saloon.save(update_fields=update_fields)
    if not was_approved:
        assign_subscription_trial_on_approval(saloon)
    messages.success(request, "Test saloon approved." if saloon.is_test_saloon else "Saloon approved.")
    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("staffpanel:saloon_queue")


@require_POST
@staff_required
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


@staff_required
def category_queue(request):
    pending_categories = (
        ServiceCategory.objects.filter(is_approved=False, is_active=True)
        .select_related("created_by_saloon", "main_category")
        .order_by("-created_at")
    )
    all_categories = (
        ServiceCategory.objects.all()
        .select_related("created_by_saloon", "main_category")
        .order_by("name")
    )
    approved_subcategories = ServiceCategory.objects.filter(
        is_active=True,
        is_approved=True,
    ).order_by("name")
    main_categories = MainCategory.objects.filter(is_active=True).prefetch_related(
        Prefetch("service_categories", queryset=approved_subcategories, to_attr="approved_subcategories")
    ).order_by("display_order", "name")
    services = (
        Service.objects.filter(is_deleted=False)
        .select_related("saloon", "category", "main_category")
        .order_by("-created_at")[:100]
    )
    return render(
        request,
        "staffpanel/category_queue.html",
        _staff_context(request, {
            "active_tab": "categories",
            "categories": pending_categories,
            "all_categories": all_categories,
            "pending_count": pending_categories.count(),
            "main_categories": main_categories,
            "services": services,
        }),
    )


@require_POST
@staff_required
def category_map_main(request, category_id):
    category = get_object_or_404(ServiceCategory, id=category_id)
    category_name = (request.POST.get("category_name") or "").strip()
    main_category_id = request.POST.get("main_category_id")
    if not category_name:
        messages.error(request, "Category name cannot be empty.")
        return redirect("staffpanel:category_queue")
    if len(category_name) > ServiceCategory._meta.get_field("name").max_length:
        messages.error(request, "Category name is too long.")
        return redirect("staffpanel:category_queue")
    if ServiceCategory.objects.filter(name__iexact=category_name).exclude(id=category.id).exists():
        messages.error(request, "A service category with that name already exists.")
        return redirect("staffpanel:category_queue")

    name_changed = category.name != category_name
    if name_changed:
        category.name = category_name
        category.slug = ""

    if not main_category_id:
        category.main_category = None
        category.save()
        messages.success(request, "Category name saved and mapping cleared.")
        next_url = request.POST.get("next")
        if next_url:
            return redirect(next_url)
        return redirect("staffpanel:category_queue")

    main_category = get_object_or_404(MainCategory, id=main_category_id, is_active=True)
    category.main_category = main_category
    category.save()
    messages.success(request, "Category name and main-category mapping saved.")
    next_url = request.POST.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("staffpanel:category_queue")


@require_POST
@staff_required
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
@staff_required
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
@staff_required
def category_reject(request, category_id):
    category = get_object_or_404(ServiceCategory, id=category_id)
    category.is_active = False
    category.save(update_fields=["is_active"])
    messages.success(request, "Service category rejected (disabled).")
    return redirect("staffpanel:category_queue")


@require_POST
@staff_required
def category_update(request, category_id):
    category = get_object_or_404(ServiceCategory, id=category_id)
    category_name = (request.POST.get("category_name") or "").strip()
    main_category_id = (request.POST.get("main_category_id") or "").strip()
    is_approved = request.POST.get("is_approved") == "on"
    is_active = request.POST.get("is_active") == "on"

    if not category_name:
        messages.error(request, "Category name cannot be empty.")
        return redirect("staffpanel:category_queue")
    if len(category_name) > ServiceCategory._meta.get_field("name").max_length:
        messages.error(request, "Category name is too long.")
        return redirect("staffpanel:category_queue")
    if ServiceCategory.objects.filter(name__iexact=category_name).exclude(id=category.id).exists():
        messages.error(request, "A service category with that name already exists.")
        return redirect("staffpanel:category_queue")

    main_category = None
    if main_category_id:
        main_category = get_object_or_404(MainCategory, id=main_category_id, is_active=True)
    if is_approved and not main_category:
        messages.error(request, "An approved subcategory must be mapped to a main category.")
        return redirect("staffpanel:category_queue")

    if category.name != category_name:
        category.name = category_name
        category.slug = ""
    category.main_category = main_category
    category.is_approved = is_approved
    category.is_active = is_active
    category.save()
    messages.success(request, "Service category updated.")
    return redirect("staffpanel:category_queue")


@require_POST
@staff_required
def category_delete(request, category_id):
    category = get_object_or_404(ServiceCategory, id=category_id)
    if category.services.exists():
        category.is_active = False
        category.is_approved = False
        category.save(update_fields=["is_active", "is_approved"])
        messages.warning(request, "This category is used by services, so it was disabled instead of permanently deleted.")
    else:
        category.delete()
        messages.success(request, "Service category deleted.")
    return redirect("staffpanel:category_queue")


@require_POST
@staff_required
def service_review_update(request, service_id):
    service = get_object_or_404(Service.objects.select_related("saloon"), id=service_id, is_deleted=False)
    service_name = (request.POST.get("service_name") or "").strip()
    category_choice = (request.POST.get("category_choice") or "").strip()

    if not service_name:
        messages.error(request, "Service name cannot be empty.")
        return redirect("staffpanel:category_queue")

    category = None
    main_category = None
    if category_choice.startswith("main:") and category_choice[5:].isdigit():
        main_category = MainCategory.objects.filter(id=category_choice[5:], is_active=True).first()
    elif category_choice.startswith("category:") and category_choice[9:].isdigit():
        category = ServiceCategory.objects.filter(
            id=category_choice[9:],
            is_active=True,
            is_approved=True,
            main_category__is_active=True,
        ).first()
    if not category and not main_category:
        messages.error(request, "Choose an active main category or approved subcategory.")
        return redirect("staffpanel:category_queue")

    service.name = service_name
    service.category = category
    service.main_category = main_category
    service.is_active = request.POST.get("is_active") == "on"
    service.is_visible = request.POST.get("is_visible") == "on"
    service.save()
    messages.success(request, "Service reviewed and updated.")
    return redirect("staffpanel:category_queue")


@require_POST
@staff_required
def service_review_delete(request, service_id):
    service = get_object_or_404(Service, id=service_id, is_deleted=False)
    service.is_deleted = True
    service.deleted_at = timezone.now()
    service.is_active = False
    service.save(update_fields=["is_deleted", "deleted_at", "is_active", "updated_at"])
    messages.success(request, "Service removed. It can still be recovered from the database if needed.")
    return redirect("staffpanel:category_queue")


@admin_required
def staff_management(request):
    User = get_user_model()
    if request.method == "POST":
        full_name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        role = request.POST.get("role", StaffProfile.ROLE_STAFF).strip()
        allowed_roles = {StaffProfile.ROLE_STAFF, StaffProfile.ROLE_ADMIN}
        if _is_super_admin_level(request.user):
            allowed_roles.add(StaffProfile.ROLE_SUPER_ADMIN)
        if role not in allowed_roles:
            messages.error(request, "You cannot create that role.")
            return redirect("staffpanel:staff_management")
        if not email:
            messages.error(request, "Email is required.")
            return redirect("staffpanel:staff_management")

        username_base = email.split("@", 1)[0] or "staff"
        username = username_base
        counter = 1
        while User.objects.filter(username=username).exclude(email__iexact=email).exists():
            counter += 1
            username = f"{username_base}{counter}"
        user, created = User.objects.get_or_create(
            email=email,
            defaults={"username": username, "is_staff": True, "is_active": True},
        )
        if full_name:
            parts = full_name.split(" ", 1)
            user.first_name = parts[0]
            user.last_name = parts[1] if len(parts) > 1 else ""
        user.is_staff = True
        user.is_active = True
        if created:
            user.set_unusable_password()
        user.save()

        profile, _ = StaffProfile.objects.get_or_create(user=user, defaults={"created_by": request.user})
        profile.role = role
        profile.status = StaffProfile.STATUS_PENDING if role == StaffProfile.ROLE_STAFF else StaffProfile.STATUS_ACTIVE
        if role == StaffProfile.ROLE_STAFF or not profile.invite_code_display:
            profile.set_invite_code(StaffProfile.generate_invite_code())
        profile.created_by = profile.created_by or request.user
        profile.save()
        messages.success(request, "Staff profile created. Share the invite code from the details page.")
        return redirect("staffpanel:staff_detail", profile_id=profile.id)

    staff_profiles = StaffProfile.objects.select_related("user").exclude(status=StaffProfile.STATUS_REMOVED).order_by("role", "user__first_name", "user__email")
    stats = {
        "total": staff_profiles.count(),
        "active": staff_profiles.filter(status=StaffProfile.STATUS_ACTIVE).count(),
        "pending": staff_profiles.filter(status=StaffProfile.STATUS_PENDING).count(),
        "suspended": staff_profiles.filter(status=StaffProfile.STATUS_SUSPENDED).count(),
        "blocked": staff_profiles.filter(status=StaffProfile.STATUS_BLOCKED).count(),
    }
    return render(
        request,
        "staffpanel/staff_management.html",
        _staff_context(request, {
            "active_tab": "staff_management",
            "staff_profiles": staff_profiles,
            "stats": stats,
            "role_choices": StaffProfile.ROLE_CHOICES,
        }),
    )


@admin_required
def staff_detail(request, profile_id):
    profile = get_object_or_404(StaffProfile.objects.select_related("user", "created_by"), id=profile_id)
    if profile.role == StaffProfile.ROLE_SUPER_ADMIN and not _is_super_admin_level(request.user):
        messages.error(request, "Super admin profiles can only be viewed by a super admin.")
        return redirect("staffpanel:staff_management")
    return render(
        request,
        "staffpanel/staff_detail.html",
        _staff_context(request, {
            "active_tab": "staff_management",
            "profile": profile,
        }),
    )


@require_POST
@admin_required
def staff_action(request, profile_id, action):
    profile = get_object_or_404(StaffProfile.objects.select_related("user"), id=profile_id)
    if profile.user_id == request.user.id and action in {"suspend", "block", "remove"}:
        messages.error(request, "You cannot disable your own staff access.")
        return redirect("staffpanel:staff_detail", profile_id=profile.id)
    if profile.role == StaffProfile.ROLE_SUPER_ADMIN and not _is_super_admin_level(request.user):
        messages.error(request, "Only a super admin can change this profile.")
        return redirect("staffpanel:staff_management")

    if action == "reset-code":
        profile.set_invite_code(StaffProfile.generate_invite_code())
        profile.status = StaffProfile.STATUS_PENDING if profile.role == StaffProfile.ROLE_STAFF else profile.status
        profile.save(update_fields=["invite_code_hash", "invite_code_display", "invite_code_created_at", "status", "updated_at"])
        messages.success(request, "Invite code reset.")
    elif action == "activate":
        profile.status = StaffProfile.STATUS_ACTIVE
        profile.user.is_staff = True
        profile.user.is_active = True
        profile.user.save(update_fields=["is_staff", "is_active"])
        profile.save(update_fields=["status", "updated_at"])
        messages.success(request, "Staff access activated.")
    elif action == "suspend":
        profile.status = StaffProfile.STATUS_SUSPENDED
        profile.save(update_fields=["status", "updated_at"])
        messages.success(request, "Staff access suspended.")
    elif action == "block":
        profile.status = StaffProfile.STATUS_BLOCKED
        profile.save(update_fields=["status", "updated_at"])
        messages.success(request, "Staff access blocked.")
    elif action == "remove":
        profile.status = StaffProfile.STATUS_REMOVED
        profile.user.is_staff = False
        profile.user.save(update_fields=["is_staff"])
        profile.save(update_fields=["status", "updated_at"])
        messages.success(request, "Staff access removed. The user account was not deleted.")
        return redirect("staffpanel:staff_management")
    else:
        messages.error(request, "Unknown staff action.")
    return redirect("staffpanel:staff_detail", profile_id=profile.id)


@admin_required
def subscription_management(request):
    subscriptions = SaloonSubscription.objects.select_related("saloon", "plan").order_by("-updated_at")[:60]
    return render(request, "staffpanel/subscription_management.html", _staff_context(request, {
        "active_tab": "subscription_management",
        "subscriptions": subscriptions,
    }))


@admin_required
def reports(request):
    return render(request, "staffpanel/simple_page.html", _staff_context(request, {
        "active_tab": "reports",
        "page_title": "Reports",
        "page_note": "Operational reports will live here.",
    }))


@admin_required
def notifications(request):
    return render(request, "staffpanel/simple_page.html", _staff_context(request, {
        "active_tab": "notifications",
        "page_title": "Notifications",
        "page_note": "Announcements and salon-owner messages will live here.",
    }))


@super_admin_required
def admin_management(request):
    admins = StaffProfile.objects.select_related("user").filter(role__in=[StaffProfile.ROLE_ADMIN, StaffProfile.ROLE_SUPER_ADMIN])
    return render(request, "staffpanel/simple_table.html", _staff_context(request, {
        "active_tab": "admin_management",
        "page_title": "Admin Management",
        "items": admins,
    }))


@super_admin_required
def founder_program(request):
    founders = SaloonSubscription.objects.select_related("saloon", "plan").filter(is_founder_partner=True).order_by("founder_partner_number")
    return render(request, "staffpanel/founder_program.html", _staff_context(request, {
        "active_tab": "founder_program",
        "founders": founders,
    }))


@super_admin_required
def plans(request):
    return render(request, "staffpanel/simple_page.html", _staff_context(request, {
        "active_tab": "plans",
        "page_title": "Plans",
        "page_note": "Subscription plan controls will live here.",
    }))


@super_admin_required
def analytics(request):
    return render(request, "staffpanel/simple_page.html", _staff_context(request, {
        "active_tab": "analytics",
        "page_title": "Analytics",
        "page_note": "Platform-wide analytics will live here.",
    }))


@super_admin_required
def audit_logs(request):
    events = SubscriptionWebhookEvent.objects.order_by("-created_at")[:80]
    return render(request, "staffpanel/audit_logs.html", _staff_context(request, {
        "active_tab": "audit_logs",
        "events": events,
    }))


@admin_required
def system_settings(request):
    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        saloon_id = request.POST.get("saloon_id")

        if action == "toggle_test_mode":
            enabled = request.POST.get("enabled") == "1"
            StaffPanelSetting.set_bool(TEST_SALOON_MODE_KEY, enabled, request.user)
            messages.success(request, "Test salon mode turned on." if enabled else "Test salon mode turned off.")
        elif action in {"hide_test", "delete_test", "convert_test"} and saloon_id:
            saloon = get_object_or_404(Saloon, id=saloon_id, is_test_saloon=True)
            if action == "hide_test":
                saloon.is_active = False
                saloon.test_saloon_hidden_at = timezone.now()
                saloon.save(update_fields=["is_active", "test_saloon_hidden_at", "updated_at"])
                messages.success(request, f"{saloon.name or 'Test saloon'} hidden from public pages.")
            elif action == "delete_test":
                name = saloon.name or "Test saloon"
                saloon.delete()
                messages.success(request, f"{name} deleted.")
            elif action == "convert_test":
                saloon.is_test_saloon = False
                saloon.test_saloon_converted_at = timezone.now()
                saloon.save(update_fields=["is_test_saloon", "test_saloon_converted_at", "updated_at"])
                messages.success(request, f"{saloon.name or 'Saloon'} converted to a real saloon.")
        elif action == "hide_all_tests":
            count = Saloon.objects.filter(is_test_saloon=True, is_active=True).update(
                is_active=False,
                test_saloon_hidden_at=timezone.now(),
                updated_at=timezone.now(),
            )
            messages.success(request, f"{count} test saloon(s) hidden.")
        elif action == "delete_all_tests":
            qs = Saloon.objects.filter(is_test_saloon=True)
            count = qs.count()
            qs.delete()
            messages.success(request, f"{count} test saloon(s) deleted.")
        else:
            messages.error(request, "Unknown test salon action.")
        return redirect("staffpanel:system_settings")

    test_saloons = Saloon.objects.filter(is_test_saloon=True).select_related("owner", "profile").order_by("-updated_at", "-id")
    return render(request, "staffpanel/system_settings.html", _staff_context(request, {
        "active_tab": "system_settings",
        "test_saloon_mode_enabled": _is_test_saloon_mode_enabled(),
        "test_saloons": test_saloons,
        "test_saloon_count": test_saloons.count(),
        "active_test_saloon_count": test_saloons.filter(is_active=True).count(),
    }))


@super_admin_required
def razorpay(request):
    events = SubscriptionWebhookEvent.objects.order_by("-created_at")[:30]
    return render(request, "staffpanel/audit_logs.html", _staff_context(request, {
        "active_tab": "razorpay",
        "events": events,
        "page_title": "Razorpay Event Log",
    }))


@super_admin_required
def feature_flags(request):
    return render(request, "staffpanel/simple_page.html", _staff_context(request, {
        "active_tab": "feature_flags",
        "page_title": "Feature Flags",
        "page_note": "Launch switches and experimental features will live here.",
    }))




