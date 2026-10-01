import random
import os
import json
import hmac
import base64
import hashlib
from datetime import timedelta
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.shortcuts import get_object_or_404, render, redirect
from services.utils import cleanup_expired_services
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.urls import reverse
from django.contrib import messages
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.conf import settings
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from public.models import UserNotification
from services.models import Service, ServiceInterest
from plans.models import SaloonSubscription, SubscriptionWebhookEvent
from plans.billing import (
    get_default_subscription_plan,
    get_or_create_saloon_subscription,
    sync_local_subscription_state,
    subscription_has_platform_access,
    subscription_access_until,
    subscription_lock_reason,
)
from .models import (
    Saloon,
    State,
    District,
    TrustedDevice,
    SaloonProfile,
    GalleryPost,
    SaloonReview,
    SaloonProfileView,
    SaloonMapClick,
)
from .utils import (
    build_saloon_public_url,
    format_whatsapp_number,
    is_reserved_saloon_slug,
    normalize_saloon_slug,
    saloon_domain_suffix,
    send_otp_whatsapp,
    sync_saloon_slug_from_name,
)


def _clean_coordinate(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _clean_time(value):
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError):
        return None



def _location_master_data():
    states = list(State.objects.filter(is_active=True).order_by("name").prefetch_related("districts"))
    districts_by_state = {
        str(state.id): [
            {"id": district.id, "name": district.name}
            for district in state.districts.filter(is_active=True).order_by("name")
        ]
        for state in states
    }
    return states, districts_by_state


def _build_slug_suggestions(base_slug, saloon_id=None, limit=3):
    base = normalize_saloon_slug(base_slug)
    if not base:
        return []
    candidates = [f"{base}01", f"{base}-studio", f"{base}-official", f"{base}123"]
    suggestions = []
    for candidate in candidates:
        clean = normalize_saloon_slug(candidate)
        if not clean or is_reserved_saloon_slug(clean):
            continue
        if Saloon.objects.filter(slug=clean).exclude(id=saloon_id).exists():
            continue
        suggestions.append(clean)
        if len(suggestions) >= limit:
            break
    return suggestions


def _sync_slug_from_saloon_name_if_default(saloon, force=False):
    sync_saloon_slug_from_name(saloon, force=force)
# ================================
# DASHBOARD ACCESS GUARD (SHARED)
# ================================
def _dashboard_guard(request, username):
    if request.user.username != username:
        return None, redirect("home")

    saloon = Saloon.objects.filter(owner=request.user).first()
    if not saloon:
        return None, redirect("partner_home")

    if not saloon.is_whatsapp_verified:
        return None, redirect("verify_whatsapp")

    device_token = request.COOKIES.get("salon_device")
    if not device_token:
        return None, redirect("verify_whatsapp")

    trusted = TrustedDevice.objects.filter(
        user=request.user,
        device_token=device_token
    ).first()

    if not trusted:
        return None, redirect("verify_whatsapp")

    trusted.last_used_at = timezone.now()
    trusted.save(update_fields=["last_used_at"])
    _sync_slug_from_saloon_name_if_default(saloon)

    return saloon, None


OTP_MAX_ATTEMPTS = 3
OTP_LOCK_SECONDS = 60


def _last_six_months():
    current = timezone.localdate().replace(day=1)
    months = []
    for offset in range(5, -1, -1):
        year = current.year
        month = current.month - offset
        while month <= 0:
            month += 12
            year -= 1
        months.append((year, month))
    return months


def _build_dashboard_metrics(saloon, analytics_until=None):
    services = (
        Service.objects
        .filter(saloon=saloon, is_active=True, is_deleted=False)
        .select_related("category", "category__main_category")
    )
    gallery_posts = GalleryPost.objects.filter(saloon=saloon)
    reviews = SaloonReview.objects.filter(saloon=saloon, is_visible=True)
    profile_views = SaloonProfileView.objects.filter(saloon=saloon)
    map_clicks = SaloonMapClick.objects.filter(saloon=saloon)
    interests = (
        ServiceInterest.objects
        .filter(service__saloon=saloon, service__is_active=True, service__is_deleted=False)
        .select_related("service")
    )

    if analytics_until:
        profile_views = profile_views.filter(created_at__lte=analytics_until)
        map_clicks = map_clicks.filter(created_at__lte=analytics_until)
        interests = interests.filter(created_at__lte=analytics_until)
        interest_count = Count("interest_events", filter=Q(interest_events__created_at__lte=analytics_until))
    else:
        interest_count = Count("interest_events")
    service_total = services.count()
    gallery_post_total = gallery_posts.count()
    gallery_views_total = gallery_posts.aggregate(total=Sum("views")).get("total") or 0
    review_total = reviews.count()
    profile_view_total = profile_views.count()
    map_click_total = map_clicks.count()
    interest_total = interests.count()

    interest_queryset = list(
        services.annotate(interest_total=interest_count).order_by("-interest_total", "name")
    )
    interest_labels = [service.name for service in interest_queryset if service.interest_total > 0]
    interest_values = [service.interest_total for service in interest_queryset if service.interest_total > 0]

    top_label = "0"
    top_share = 0
    top_interest_count = 0
    top_support = "interest"
    if interest_values:
        top_service = interest_queryset[0]
        top_label = top_service.name
        top_interest_count = top_service.interest_total
        top_share = round((top_service.interest_total / interest_total) * 100) if interest_total else 0
        interest_word = "person" if top_interest_count == 1 else "people"
        top_support = f"{top_interest_count} {interest_word} showed interest ? {top_share}% of total interest"

    month_labels = []
    monthly_view_counts = []
    monthly_map_counts = []
    for year, month in _last_six_months():
        month_labels.append(datetime(year, month, 1).strftime("%b"))
        monthly_view_counts.append(
            profile_views.filter(created_at__year=year, created_at__month=month).count()
        )
        monthly_map_counts.append(
            map_clicks.filter(created_at__year=year, created_at__month=month).count()
        )

    return {
        "service_total": service_total,
        "gallery_post_total": gallery_post_total,
        "gallery_views_total": gallery_views_total,
        "review_total": review_total,
        "profile_view_total": profile_view_total,
        "map_click_total": map_click_total,
        "interest_total": interest_total,
        "top_service_label": top_label,
        "top_service_share": top_share,
        "top_service_interest_count": top_interest_count,
        "top_service_support": top_support,
        "interest_chart": {
            "labels": interest_labels or ["No interest yet"],
            "values": interest_values or [1],
        },
        "activity_chart": {
            "labels": month_labels,
            "views": monthly_view_counts,
            "maps": monthly_map_counts,
        },
    }


def _get_default_subscription_plan():
    return get_default_subscription_plan()


def _sync_local_subscription_state(subscription):
    return sync_local_subscription_state(subscription)


def _get_or_create_saloon_subscription(saloon):
    return get_or_create_saloon_subscription(saloon)


def _subscription_access_context(subscription):
    subscription = _sync_local_subscription_state(subscription)
    has_access = subscription_has_platform_access(subscription)
    access_until = subscription_access_until(subscription)
    reason = subscription_lock_reason(subscription) if not has_access else {"title": "", "message": ""}
    return {
        "subscription_access_active": has_access,
        "subscription_access_until": access_until,
        "subscription_locked": not has_access,
        "subscription_lock_title": reason["title"],
        "subscription_lock_message": reason["message"],
    }


def _locked_dashboard_response(request, saloon, active_tab="dashboard"):
    subscription, plan = _get_or_create_saloon_subscription(saloon)
    access = _subscription_access_context(subscription)
    return render(
        request,
        "saloons/dashboard/locked.html",
        {
            "saloon": saloon,
            "subscription": subscription,
            "subscription_plan": plan,
            "active_tab": active_tab,
            **access,
        },
        status=403,
    )


def _subscription_locked_for_dashboard(saloon):
    subscription, _ = _get_or_create_saloon_subscription(saloon)
    access = _subscription_access_context(subscription)
    return access["subscription_locked"]


def _billing_state_context(subscription):
    is_trial_active = subscription.trial_ends_at > timezone.now()
    autopay_ready = bool(subscription.autopay_confirmed_at)
    next_charge_date = subscription.trial_ends_at if is_trial_active else subscription.current_period_end

    if is_trial_active and not autopay_ready:
        billing_state = "trial_pending"
    elif is_trial_active and autopay_ready:
        billing_state = "trial_ready"
    elif not is_trial_active and autopay_ready:
        billing_state = "ended_ready"
    else:
        billing_state = "ended_pending"

    return {
        "is_trial_active": is_trial_active,
        "trial_days_left": subscription.trial_days_left,
        "autopay_ready": autopay_ready,
        "next_charge_date": next_charge_date,
        "billing_state": billing_state,
    }


def _myfestivo_dashboard_url():
    connect_url = getattr(settings, "MYFESTIVO_CONNECT_URL", "http://localhost:8001/vendors/elegentra/connect/").strip()
    marker = "/vendors/elegentra/connect/"
    if marker in connect_url:
        return connect_url.split(marker, 1)[0].rstrip("/") + "/vendors/dashboard/"
    return connect_url

def _razorpay_api_request(method, path, payload=None):
    key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    if not key_id or not key_secret:
        raise RuntimeError("Razorpay keys are not configured.")

    url = f"https://api.razorpay.com{path}"
    request = Request(url, method=method.upper())
    auth_token = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("utf-8")
    request.add_header("Authorization", f"Basic {auth_token}")
    request.add_header("Content-Type", "application/json")

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    try:
        with urlopen(request, data=data, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            details = json.loads(body)
        except json.JSONDecodeError:
            details = {"error": {"description": body or "Unable to connect to Razorpay."}}
        raise RuntimeError(details.get("error", {}).get("description", "Unable to connect to Razorpay."))
    except URLError:
        raise RuntimeError("Unable to reach Razorpay right now. Please try again.")


def _ensure_remote_plan(plan):
    if plan.razorpay_plan_id:
        return plan.razorpay_plan_id

    payload = {
        "period": plan.billing_period,
        "interval": plan.interval,
        "item": {
            "name": plan.name,
            "description": plan.description,
            "amount": plan.amount_paise,
            "currency": plan.currency,
        },
        "notes": {
            "slug": plan.slug,
            "anchor_price": str(plan.strike_amount),
        },
    }
    response = _razorpay_api_request("POST", "/v1/plans", payload)
    plan.razorpay_plan_id = response.get("id", "")
    plan.save(update_fields=["razorpay_plan_id", "updated_at"])
    return plan.razorpay_plan_id


def _verify_subscription_signature(payment_id, subscription_id, signature):
    secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    if not secret:
        return False

    generated_signature = hmac.new(
        secret.encode("utf-8"),
        f"{payment_id}|{subscription_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(generated_signature, signature or "")


# ================================
# DASHBOARD OVERVIEW
# ================================
@login_required
def dashboard_overview(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    if saloon.registration_step < 5:
        if saloon.registration_step == 1:
            continue_url = "saloon_onboarding_step_one"
        elif saloon.registration_step == 2:
            continue_url = "saloon_onboarding_step_two"
        elif saloon.registration_step == 3:
            continue_url = "saloon_onboarding_step_three"
        else:
            continue_url = "saloon_onboarding_step_four"
        return render(request, "saloons/dashboard/submitting.html", {"saloon": saloon, "continue_url": continue_url})

    if saloon.approval_status == Saloon.APPROVAL_PENDING:
        return render(request, "saloons/dashboard/pending.html", {"saloon": saloon})

    if saloon.approval_status == Saloon.APPROVAL_REJECTED or not saloon.is_active:
        return render(request, "saloons/dashboard/rejected.html", {"saloon": saloon})

    subscription, plan = _get_or_create_saloon_subscription(saloon)
    access = _subscription_access_context(subscription)
    metrics = _build_dashboard_metrics(
        saloon,
        analytics_until=access["subscription_access_until"] if access["subscription_locked"] else None,
    )
    billing = _billing_state_context(subscription)
    return render(
        request,
        "saloons/dashboard/overview.html",
        {
            "saloon": saloon,
            "active_tab": "dashboard",
            "subscription": subscription,
            "subscription_plan": plan,
            "billing_state": billing["billing_state"],
            "trial_days_left": billing["trial_days_left"],
            **access,
            "saloon_public_url": build_saloon_public_url(saloon.slug, request=request),
            "myfestivo_dashboard_url": _myfestivo_dashboard_url(),
            **metrics,
        },
    )

@login_required
def dashboard_subscription(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    subscription, plan = _get_or_create_saloon_subscription(saloon)
    access = _subscription_access_context(subscription)
    billing = _billing_state_context(subscription)
    billing_state = billing["billing_state"]
    is_trial_active = billing["is_trial_active"]
    trial_days_left = billing["trial_days_left"]
    autopay_ready = billing["autopay_ready"]
    next_charge_date = billing["next_charge_date"]

    if billing_state == "trial_ready":
        status_title = "Trial active"
        status_note = f"Your {subscription.approved_trial_days}-day free trial is active and automatic billing is ready."
    elif billing_state == "ended_ready":
        status_title = "Subscription active"
        status_note = "Your free trial has ended, and your salon remains active with automatic billing enabled."
    elif billing_state == "ended_pending":
        status_title = "Your free trial has ended"
        status_note = "Your trial period is over. Activate your subscription to keep your salon active."
    else:
        status_title = "Trial active"
        status_note = "Your salon is live right now. Set up autopay before the trial ends to avoid interruptions."

    payment_enabled = bool(os.getenv("RAZORPAY_KEY_ID", "").strip() and os.getenv("RAZORPAY_KEY_SECRET", "").strip())

    return render(
        request,
        "saloons/dashboard/subscription.html",
        {
            "saloon": saloon,
            "active_tab": "subscription",
            "plan": plan,
            "subscription": subscription,
            "billing_state": billing_state,
            "status_title": status_title,
            "status_note": status_note,
            "trial_days_left": trial_days_left,
            "is_trial_active": is_trial_active,
            "autopay_ready": autopay_ready,
            "next_charge_date": next_charge_date,
            "payment_enabled": payment_enabled,
            "razorpay_key_id": os.getenv("RAZORPAY_KEY_ID", "").strip(),
            **access,
        },
    )

@login_required
@require_POST
def dashboard_subscription_create(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return JsonResponse({"ok": False, "message": "Please sign in again."}, status=403)

    subscription, plan = _get_or_create_saloon_subscription(saloon)
    amount = max(plan.amount_paise, 100)
    payload = {
        "amount": amount,
        "currency": plan.currency,
        "receipt": f"elegentra-{saloon.id}-{int(timezone.now().timestamp())}",
        "notes": {
            "saloon_slug": saloon.slug,
            "saloon_name": saloon.name,
            "vendor_username": saloon.owner.username,
            "plan": plan.slug,
        },
    }

    try:
        response = _razorpay_api_request("POST", "/v1/orders", payload)
    except RuntimeError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)

    return JsonResponse({
        "ok": True,
        "key": os.getenv("RAZORPAY_KEY_ID", "").strip(),
        "order_id": response.get("id", ""),
        "amount": response.get("amount", amount),
        "currency": response.get("currency", plan.currency),
        "name": "ELEGENTRA",
        "description": f"{plan.name} subscription setup",
        "vendor_name": saloon.name,
        "prefill": {
            "name": request.user.get_full_name() or request.user.username,
            "email": request.user.email,
            "contact": saloon.whatsapp_number,
        },
        "theme": {"color": "#C59A43"},
    })

@login_required
@require_POST
def dashboard_subscription_verify(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return JsonResponse({"ok": False, "message": "Please sign in again."}, status=403)

    subscription_id = request.POST.get("razorpay_subscription_id", "").strip()
    payment_id = request.POST.get("razorpay_payment_id", "").strip()
    signature = request.POST.get("razorpay_signature", "").strip()

    subscription = (
        saloon.subscriptions
        .filter(razorpay_subscription_id=subscription_id)
        .order_by("-created_at", "-id")
        .first()
    )
    if not subscription:
        return JsonResponse({"ok": False, "message": "Subscription record not found."}, status=404)

    if not _verify_subscription_signature(payment_id, subscription_id, signature):
        return JsonResponse({"ok": False, "message": "Payment verification failed."}, status=400)

    now = timezone.now()
    subscription.auth_payment_id = payment_id
    subscription.autopay_confirmed_at = now
    subscription.razorpay_status = "authenticated"

    if subscription.trial_ends_at > now:
        subscription.status = SaloonSubscription.STATUS_TRIALING
        subscription.current_period_start = subscription.trial_ends_at
        subscription.current_period_end = subscription.trial_ends_at + timedelta(days=30)
    else:
        subscription.status = SaloonSubscription.STATUS_ACTIVE
        subscription.current_period_start = now
        subscription.current_period_end = now + timedelta(days=30)

    subscription.save(
        update_fields=[
            "auth_payment_id",
            "autopay_confirmed_at",
            "razorpay_status",
            "status",
            "current_period_start",
            "current_period_end",
            "updated_at",
        ]
    )

    return JsonResponse(
        {
            "ok": True,
            "redirect_url": reverse("saloon_dashboard_subscription", kwargs={"username": saloon.owner.username}),
        }
    )


@csrf_exempt
@require_POST
def razorpay_subscription_webhook(request):
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
    body = request.body

    if webhook_secret:
        signature = request.headers.get("X-Razorpay-Signature", "")
        expected = hmac.new(webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return HttpResponse(status=403)

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    event_name = payload.get("event", "")
    subscription_entity = (
        payload.get("payload", {})
        .get("subscription", {})
        .get("entity", {})
    )
    subscription_id = subscription_entity.get("id", "")

    SubscriptionWebhookEvent.objects.create(
        event_name=event_name,
        razorpay_subscription_id=subscription_id,
        payload=payload,
    )

    if subscription_id:
        subscription = (
            SaloonSubscription.objects
            .filter(razorpay_subscription_id=subscription_id)
            .order_by("-created_at", "-id")
            .first()
        )
        if subscription:
            gateway_status = subscription_entity.get("status", "") or subscription.razorpay_status
            current_start = subscription_entity.get("current_start")
            current_end = subscription_entity.get("current_end")

            subscription.razorpay_status = gateway_status
            if current_start:
                subscription.current_period_start = datetime.fromtimestamp(current_start, tz=timezone.get_current_timezone())
            if current_end:
                subscription.current_period_end = datetime.fromtimestamp(current_end, tz=timezone.get_current_timezone())

            if gateway_status in {"active", "authenticated"}:
                if subscription.trial_ends_at > timezone.now():
                    subscription.status = SaloonSubscription.STATUS_TRIALING
                else:
                    subscription.status = SaloonSubscription.STATUS_ACTIVE
            elif gateway_status in {"halted", "pending"}:
                subscription.status = SaloonSubscription.STATUS_PAST_DUE
            elif gateway_status in {"cancelled", "completed", "expired"}:
                subscription.status = SaloonSubscription.STATUS_CANCELLED
                subscription.cancelled_at = timezone.now()

            subscription.save(
                update_fields=[
                    "razorpay_status",
                    "current_period_start",
                    "current_period_end",
                    "status",
                    "cancelled_at",
                    "updated_at",
                ]
            )

    return HttpResponse(status=200)


# ================================
# SERVICES PAGE
# ================================

@login_required
def dashboard_services(request, username):
    # automatic permanent cleanup
    cleanup_expired_services()

    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    services = Service.objects.filter(
        saloon=saloon,
        is_active=True,
        is_deleted=False
    )

    # 👇 UNDO SESSION HANDLING (ONE-TIME)
    undo_service_ids = request.session.pop("undo_service_ids", None)

    return render(
        request,
        "saloons/dashboard/services.html",
        {
            "saloon": saloon,
            "services": services,
            "active_tab": "services",
            "undo_service_ids": undo_service_ids,  # 👈 pass to template
        }
    )



# ================================
# MY SALOON PAGE
# ================================
@login_required
def dashboard_mysaloon(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    active_inner_tab = request.GET.get("tab", "services")

    if active_inner_tab not in ["services", "gallery", "about"]:
        active_inner_tab = "services"

    return render(
        request,
        "saloons/dashboard/mysaloon.html",
        {
            "saloon": saloon,
            "active_tab": "mysaloon",
            "active_inner_tab": active_inner_tab,
        }
    )



# ================================
# SETTINGS PAGE
# ================================
@login_required
def dashboard_settings(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    profile, _ = SaloonProfile.objects.get_or_create(saloon=saloon)

    if request.method == "POST":
        profile.whatsapp_prefill_template = request.POST.get("whatsapp_prefill_template", "").strip()
        profile.save()
        messages.success(request, "WhatsApp inquiry message updated.")
        return redirect("saloon_dashboard_settings", username=saloon.owner.username)

    return render(
        request,
        "saloons/dashboard/settings.html",
        {
            "saloon": saloon,
            "profile": profile,
            "active_tab": "settings",
        }
    )


# ================================
# EDIT BASIC DETAILS
# ================================
@login_required
def dashboard_edit_basic_detail(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    if request.method == "POST":
        saloon_name = request.POST.get("saloon_name", "").strip()
        city = request.POST.get("city", "").strip()
        contact_number = request.POST.get("contact_number", "").strip()
        locality = request.POST.get("locality", "").strip()
        latitude = _clean_coordinate(request.POST.get("latitude", "").strip())
        longitude = _clean_coordinate(request.POST.get("longitude", "").strip())
        opening_time = _clean_time(request.POST.get("opening_time", "").strip())
        closing_time = _clean_time(request.POST.get("closing_time", "").strip())
        operating_days = request.POST.getlist("operating_days")
        banner_image = request.FILES.get("banner_image")

        if saloon_name:
            saloon.name = saloon_name
        if banner_image:
            saloon.banner_image = banner_image
        saloon.save()

        profile, _ = SaloonProfile.objects.get_or_create(saloon=saloon)
        if saloon_name:
            profile.saloon_name = saloon_name
        if city:
            profile.city = city
        if contact_number:
            profile.contact_number = contact_number
        if locality:
            profile.locality = locality
        profile.latitude = latitude
        profile.longitude = longitude
        profile.google_map_link = request.POST.get("google_map_link", "").strip()
        profile.about = request.POST.get("about", "").strip()
        profile.opening_time = opening_time
        profile.closing_time = closing_time
        profile.operating_days = operating_days
        profile.save()

        messages.success(request, "Basic details updated successfully.")
        return redirect("saloon_dashboard_edit_basic_detail", username=saloon.owner.username)

    return render(
        request,
        "saloons/dashboard/edit_basic_detail.html",
        {
            "saloon": saloon,
            "active_tab": "mysaloon",
            "show_back_button": True,
            "back_url": reverse("saloon_dashboard_mysaloon", kwargs={"username": saloon.owner.username}),
            "back_parent_label": "My Saloon",
            "back_label": "Edit",
        }
    )


# ================================
# CHANGE WHATSAPP NUMBER
# ================================
@login_required
def dashboard_change_whatsapp(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    now = timezone.now()
    attempts = request.session.get("change_otp_attempts", 0)
    locked_until = request.session.get("change_otp_locked_until")

    if locked_until:
        unlock_time = timezone.datetime.fromisoformat(locked_until)
        if now < unlock_time:
            remaining = int((unlock_time - now).total_seconds())
            messages.error(request, f"Too many attempts. Try again in {remaining} seconds.")
            return render(
                request,
                "saloons/dashboard/change_whatsapp.html",
                _change_whatsapp_context(saloon, step="input"),
            )
        request.session.pop("change_otp_attempts", None)
        request.session.pop("change_otp_locked_until", None)

    if request.method == "POST" and "save_new_number" in request.POST:
        raw_phone = request.POST.get("new_whatsapp", "")
        new_phone = format_whatsapp_number(raw_phone)

        if not new_phone:
            messages.error(request, "Enter a valid WhatsApp number with country code.")
            return render(
                request,
                "saloons/dashboard/change_whatsapp.html",
                _change_whatsapp_context(saloon, step="input"),
            )

        if saloon.whatsapp_number == new_phone:
            messages.error(request, "This is already your current WhatsApp number.")
            return render(
                request,
                "saloons/dashboard/change_whatsapp.html",
                _change_whatsapp_context(saloon, step="input"),
            )

        if Saloon.objects.filter(
            whatsapp_number=new_phone,
            is_whatsapp_verified=True
        ).exclude(owner=request.user).exists():
            messages.error(request, "This WhatsApp number is already linked to another saloon.")
            return render(
                request,
                "saloons/dashboard/change_whatsapp.html",
                _change_whatsapp_context(saloon, step="input"),
            )

        request.session["change_new_whatsapp"] = new_phone
        return render(
            request,
            "saloons/dashboard/change_whatsapp.html",
            _change_whatsapp_context(
                saloon,
                step="choose",
                masked_email=_mask_email(request.user.email),
                masked_phone=_mask_phone(saloon.whatsapp_number),
            ),
        )

    if request.method == "POST" and "send_otp" in request.POST:
        channel = request.POST.get("channel")
        new_phone = request.session.get("change_new_whatsapp")

        if not new_phone:
            messages.error(request, "Please enter the new WhatsApp number again.")
            return redirect("saloon_dashboard_change_whatsapp", username=saloon.owner.username)

        attempts += 1
        if attempts > OTP_MAX_ATTEMPTS:
            lock_until = now + timedelta(seconds=OTP_LOCK_SECONDS)
            request.session["change_otp_locked_until"] = lock_until.isoformat()
            messages.error(request, "OTP locked. Try again later.")
            return redirect("saloon_dashboard_change_whatsapp", username=saloon.owner.username)

        new_otp = random.randint(100000, 999999)
        request.session.update({
            "change_otp": str(new_otp),
            "change_otp_channel": channel,
            "change_otp_attempts": attempts,
        })

        if channel == "email":
            try:
                send_mail(
                    subject="Your Saloon OTP",
                    message=f"Your OTP code is {new_otp}",
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
                    recipient_list=[request.user.email],
                    fail_silently=False,
                )
            except Exception:
                messages.error(request, "Failed to send OTP email. Try again.")
                return redirect("saloon_dashboard_change_whatsapp", username=saloon.owner.username)
        else:
            old_phone = format_whatsapp_number(saloon.whatsapp_number)
            if not old_phone:
                messages.error(request, "Your current WhatsApp number is invalid. Use email instead.")
                return redirect("saloon_dashboard_change_whatsapp", username=saloon.owner.username)
            try:
                send_otp_whatsapp(old_phone, new_otp)
            except Exception:
                messages.error(request, "We couldn't send the OTP right now. Please try again or use email instead.")
                return redirect("saloon_dashboard_change_whatsapp", username=saloon.owner.username)

        return render(
            request,
            "saloons/dashboard/change_whatsapp.html",
            _change_whatsapp_context(
                saloon,
                step="otp",
                masked_email=_mask_email(request.user.email),
                masked_phone=_mask_phone(saloon.whatsapp_number),
                otp_channel=request.session.get("change_otp_channel"),
            ),
        )

    if request.method == "POST" and "verify_otp" in request.POST:
        otp = request.session.get("change_otp")
        new_phone = request.session.get("change_new_whatsapp")

        if request.POST.get("otp") != otp:
            messages.error(request, "Invalid OTP")
            return render(
                request,
                "saloons/dashboard/change_whatsapp.html",
                _change_whatsapp_context(
                    saloon,
                    step="otp",
                    masked_email=_mask_email(request.user.email),
                    masked_phone=_mask_phone(saloon.whatsapp_number),
                    otp_channel=request.session.get("change_otp_channel"),
                ),
            )

        if not new_phone:
            messages.error(request, "Please start again.")
            return redirect("saloon_dashboard_change_whatsapp", username=saloon.owner.username)

        saloon.whatsapp_number = new_phone
        saloon.is_whatsapp_verified = True
        saloon.save(update_fields=["whatsapp_number", "is_whatsapp_verified"])

        for key in [
            "change_new_whatsapp",
            "change_otp",
            "change_otp_channel",
            "change_otp_attempts",
            "change_otp_locked_until",
        ]:
            request.session.pop(key, None)

        messages.success(request, "WhatsApp number updated.")
        return redirect("saloon_dashboard_edit_basic_detail", username=saloon.owner.username)

    return render(
        request,
        "saloons/dashboard/change_whatsapp.html",
        _change_whatsapp_context(saloon, step="input"),
    )


def _change_whatsapp_context(saloon, step, masked_email=None, masked_phone=None, otp_channel=None):
    return {
        "saloon": saloon,
        "step": step,
        "masked_email": masked_email,
        "masked_phone": masked_phone,
        "otp_channel": otp_channel,
        "active_tab": "mysaloon",
        "show_back_button": True,
        "back_url": reverse("saloon_dashboard_edit_basic_detail", kwargs={"username": saloon.owner.username}),
        "back_parent_label": "My Saloon",
        "back_label": "Change WhatsApp",
    }


def _mask_phone(phone):
    if not phone:
        return ""
    phone = phone.strip()
    return phone[-4:].rjust(len(phone), "*")


def _mask_email(email):
    if not email or "@" not in email:
        return ""
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        masked = name[0] + "*"
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return masked + "@" + domain
















# ================================
# CURRENT DASHBOARD OVERRIDES
# ================================
@login_required
def dashboard_connect_myfestivo(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    if _subscription_locked_for_dashboard(saloon):
        return _locked_dashboard_response(request, saloon, active_tab="dashboard")
    messages.info(request, "MyFestivo connection is not available right now.")
    return redirect("saloon_dashboard", username=saloon.owner.username)


@login_required
def dashboard_services(request, username):
    cleanup_expired_services()
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    if _subscription_locked_for_dashboard(saloon):
        return _locked_dashboard_response(request, saloon, active_tab="services")
    services = Service.objects.filter(saloon=saloon, is_active=True, is_deleted=False)
    undo_service_ids = request.session.pop("undo_service_ids", None)
    return render(
        request,
        "saloons/dashboard/services.html",
        {
            "saloon": saloon,
            "services": services,
            "active_tab": "services",
            "undo_service_ids": undo_service_ids,
            **_subscription_access_context(_get_or_create_saloon_subscription(saloon)[0]),
        },
    )


@login_required
def dashboard_mysaloon(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    _sync_slug_from_saloon_name_if_default(saloon)
    if _subscription_locked_for_dashboard(saloon):
        return _locked_dashboard_response(request, saloon, active_tab="mysaloon")
    active_inner_tab = request.GET.get("tab", "services")
    if active_inner_tab not in ["services", "gallery", "about"]:
        active_inner_tab = "services"
    gallery_posts = [
        post
        for post in saloon.gallery_posts.filter(is_hidden=False).select_related("service", "service__category").prefetch_related("media")
        if post.image_media
    ]
    subscription, _ = _get_or_create_saloon_subscription(saloon)
    return render(
        request,
        "saloons/dashboard/mysaloon.html",
        {
            "saloon": saloon,
            "gallery_posts": gallery_posts,
            "active_tab": "mysaloon",
            "active_inner_tab": active_inner_tab,
            "saloon_public_url": build_saloon_public_url(saloon.slug, request=request),
            "myfestivo_dashboard_url": _myfestivo_dashboard_url(),
            "saloon_domain_suffix": saloon_domain_suffix(),
            "saloon_slug_check_url": reverse("saloon_dashboard_slug_check", kwargs={"username": saloon.owner.username}),
            "saloon_slug_update_url": reverse("saloon_dashboard_slug_update", kwargs={"username": saloon.owner.username}),
            **_subscription_access_context(subscription),
        },
    )


@login_required
def dashboard_slug_check(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return JsonResponse({"ok": False, "message": "Please sign in again."}, status=403)
    if _subscription_locked_for_dashboard(saloon):
        return JsonResponse({"ok": False, "message": "Subscription required."}, status=403)
    raw_slug = request.GET.get("slug", "")
    normalized_slug = normalize_saloon_slug(raw_slug or saloon.name or saloon.owner.username)
    if not normalized_slug:
        return JsonResponse({"ok": True, "available": False, "slug": "", "message": "Enter a salon link name.", "suggestions": [], "public_url": ""})
    is_taken = is_reserved_saloon_slug(normalized_slug) or Saloon.objects.filter(slug=normalized_slug).exclude(id=saloon.id).exists()
    return JsonResponse({
        "ok": True,
        "available": not is_taken,
        "slug": normalized_slug,
        "message": "Available" if not is_taken else "This name is already taken",
        "suggestions": [] if not is_taken else _build_slug_suggestions(normalized_slug, saloon.id),
        "public_url": build_saloon_public_url(normalized_slug, request=request),
    })


@login_required
@require_POST
def dashboard_slug_update(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return JsonResponse({"ok": False, "message": "Please sign in again."}, status=403)
    if _subscription_locked_for_dashboard(saloon):
        return JsonResponse({"ok": False, "message": "Subscription required."}, status=403)
    normalized_slug = normalize_saloon_slug(request.POST.get("slug", "") or saloon.name or saloon.owner.username)
    if not normalized_slug:
        return JsonResponse({"ok": False, "message": "Enter a valid salon link name."}, status=400)
    if is_reserved_saloon_slug(normalized_slug) or Saloon.objects.filter(slug=normalized_slug).exclude(id=saloon.id).exists():
        return JsonResponse({"ok": False, "message": "This name is already taken.", "suggestions": _build_slug_suggestions(normalized_slug, saloon.id)}, status=400)
    saloon.slug = normalized_slug
    saloon.save(update_fields=["slug", "updated_at"])
    return JsonResponse({"ok": True, "slug": normalized_slug, "public_url": build_saloon_public_url(normalized_slug, request=request), "message": "Salon link updated"})


@login_required
def dashboard_settings(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    subscription, _ = _get_or_create_saloon_subscription(saloon)
    access = _subscription_access_context(subscription)
    profile, _ = SaloonProfile.objects.get_or_create(saloon=saloon)
    if request.method == "POST":
        profile.whatsapp_prefill_template = request.POST.get("whatsapp_prefill_template", "").strip()
        profile.save()
        messages.success(request, "WhatsApp inquiry message updated.")
        return redirect("saloon_dashboard_settings", username=saloon.owner.username)
    return render(request, "saloons/dashboard/settings.html", {"saloon": saloon, "profile": profile, "active_tab": "settings", **access})


@login_required
def dashboard_edit_basic_detail(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    if _subscription_locked_for_dashboard(saloon):
        return _locked_dashboard_response(request, saloon, active_tab="mysaloon")
    states, districts_by_state = _location_master_data()
    if request.method == "POST":
        saloon_name = request.POST.get("saloon_name", "").strip()
        state_id = request.POST.get("state_id", "").strip()
        district_id = request.POST.get("district_id", "").strip()
        city = request.POST.get("city", "").strip()
        contact_number = request.POST.get("contact_number", "").strip()
        locality = request.POST.get("locality", "").strip()
        latitude = _clean_coordinate(request.POST.get("latitude", "").strip())
        longitude = _clean_coordinate(request.POST.get("longitude", "").strip())
        opening_time = _clean_time(request.POST.get("opening_time", "").strip())
        closing_time = _clean_time(request.POST.get("closing_time", "").strip())
        operating_days = request.POST.getlist("operating_days")
        banner_image = request.FILES.get("banner_image")
        if saloon_name:
            saloon.name = saloon_name
        if banner_image:
            saloon.banner_image = banner_image
        saloon.save()
        _sync_slug_from_saloon_name_if_default(saloon, force=True)
        profile, _ = SaloonProfile.objects.get_or_create(saloon=saloon)
        if saloon_name:
            profile.saloon_name = saloon_name
        profile.state = State.objects.filter(id=state_id, is_active=True).first() if state_id else None
        selected_district = District.objects.filter(id=district_id, is_active=True).first() if district_id else None
        if selected_district and profile.state and selected_district.state_id != profile.state_id:
            selected_district = None
        profile.district = selected_district
        if city:
            profile.city = city
        if contact_number:
            profile.contact_number = contact_number
        if locality:
            profile.locality = locality
        profile.latitude = latitude
        profile.longitude = longitude
        profile.google_map_link = request.POST.get("google_map_link", "").strip()
        profile.about = request.POST.get("about", "").strip()
        profile.opening_time = opening_time
        profile.closing_time = closing_time
        profile.operating_days = operating_days
        profile.save()
        messages.success(request, "Basic details updated successfully.")
        return redirect("saloon_dashboard_edit_basic_detail", username=saloon.owner.username)
    subscription, _ = _get_or_create_saloon_subscription(saloon)
    return render(
        request,
        "saloons/dashboard/edit_basic_detail.html",
        {
            "saloon": saloon,
            "active_tab": "mysaloon",
            "show_back_button": True,
            "back_url": reverse("saloon_dashboard_mysaloon", kwargs={"username": saloon.owner.username}),
            "back_parent_label": "My Salon",
            "back_label": "Edit",
            "states": states,
            "districts_by_state": districts_by_state,
            **_subscription_access_context(subscription),
        },
    )


@login_required
def dashboard_search(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return JsonResponse({"results": []}, status=403)
    query = (request.GET.get("q") or "").strip().lower()
    items = [
        {"title": "Dashboard", "type": "Page", "url": reverse("saloon_dashboard", kwargs={"username": username}), "icon": "fa-chart-line"},
        {"title": "Analytics", "type": "Page", "url": reverse("saloon_dashboard_analytics", kwargs={"username": username}), "icon": "fa-chart-simple"},
        {"title": "Notifications", "type": "Page", "url": reverse("saloon_dashboard_notifications", kwargs={"username": username}), "icon": "fa-bell"},
        {"title": "Subscription", "type": "Page", "url": reverse("saloon_dashboard_subscription", kwargs={"username": username}), "icon": "fa-credit-card"},
        {"title": "Settings", "type": "Page", "url": reverse("saloon_dashboard_settings", kwargs={"username": username}), "icon": "fa-gear"},
    ]
    if not _subscription_locked_for_dashboard(saloon):
        items.extend([
            {"title": "My Salon", "type": "Page", "url": reverse("saloon_dashboard_mysaloon", kwargs={"username": username}), "icon": "fa-store"},
            {"title": "Services", "type": "Page", "url": reverse("saloon_dashboard_services", kwargs={"username": username}), "icon": "fa-scissors"},
        ])
        for service in Service.objects.filter(saloon=saloon, is_active=True, is_deleted=False).order_by("name")[:20]:
            items.append({"title": service.name, "type": "Service", "url": reverse("saloon_dashboard_services", kwargs={"username": username}), "icon": "fa-scissors"})
    if query:
        items = [item for item in items if query in item["title"].lower() or query in item["type"].lower()]
    return JsonResponse({"results": items[:8]})


@login_required
def dashboard_notifications(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    subscription, _ = _get_or_create_saloon_subscription(saloon)
    notification_list = UserNotification.objects.filter(user=request.user)
    notification_list.filter(is_read=False).update(is_read=True)
    paginator = Paginator(notification_list, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "saloons/dashboard/notifications.html",
        {
            "saloon": saloon,
            "active_tab": "dashboard",
            "notifications": page_obj.object_list,
            "page_obj": page_obj,
            **_subscription_access_context(subscription),
        },
    )


@login_required
@require_POST
def dashboard_notification_delete(request, username, notification_id):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    notification = get_object_or_404(UserNotification, id=notification_id, user=request.user)
    notification.delete()
    return redirect("saloon_dashboard_notifications", username=username)


@login_required
@require_POST
def dashboard_notifications_clear(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    UserNotification.objects.filter(user=request.user).delete()
    return redirect("saloon_dashboard_notifications", username=username)


@login_required
def dashboard_analytics(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    subscription, plan = _get_or_create_saloon_subscription(saloon)
    access = _subscription_access_context(subscription)
    analytics_until = access["subscription_access_until"] if access["subscription_locked"] else None
    metrics = _build_dashboard_metrics(saloon, analytics_until=analytics_until)
    return render(
        request,
        "saloons/dashboard/analytics.html",
        {
            "saloon": saloon,
            "subscription": subscription,
            "subscription_plan": plan,
            "active_tab": "dashboard",
            "show_back_button": True,
            "back_url": reverse("saloon_dashboard", kwargs={"username": username}),
            "back_parent_label": "Overview",
            "back_label": "Analytics",
            **access,
            **metrics,
        },
    )

# Razorpay recurring subscription route compatibility.
@login_required
@require_POST
def dashboard_subscription_recurring_create(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return JsonResponse({"ok": False, "message": "Please sign in again."}, status=403)

    subscription, plan = _get_or_create_saloon_subscription(saloon)

    if subscription.autopay_confirmed_at:
        return JsonResponse({
            "ok": True,
            "already_active": True,
            "subscription_id": subscription.razorpay_subscription_id,
            "message": "Autopay is already connected for this salon.",
            "redirect_url": reverse("saloon_dashboard_subscription", kwargs={"username": saloon.owner.username}),
        })
    if subscription.razorpay_subscription_id and subscription.status == SaloonSubscription.STATUS_PENDING_AUTH:
        return JsonResponse(
            {
                "ok": True,
                "key": os.getenv("RAZORPAY_KEY_ID", "").strip(),
                "subscription_id": subscription.razorpay_subscription_id,
                "name": "ELEGENTRA",
                "description": "First month free, then ₹699 every month via UPI Autopay.",
                "vendor_name": saloon.name,
                "prefill": {
                    "name": request.user.get_full_name() or request.user.username,
                    "email": request.user.email,
                    "contact": saloon.whatsapp_number,
                },
                "theme": {"color": "#C59A43"},
            }
        )

    try:
        remote_plan_id = _ensure_remote_plan(plan)
        now = timezone.now()
        start_at = subscription.trial_ends_at if subscription.trial_ends_at > now else now
        payload = {
            "plan_id": remote_plan_id,
            "total_count": 120,
            "quantity": 1,
            "customer_notify": 1,
            "notes": {
                "saloon_slug": saloon.slug,
                "saloon_name": saloon.name,
                "vendor_username": saloon.owner.username,
            },
        }
        if start_at > now:
            payload["start_at"] = int(start_at.timestamp())

        response = _razorpay_api_request("POST", "/v1/subscriptions", payload)
    except RuntimeError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)

    subscription.razorpay_plan_id = remote_plan_id
    subscription.razorpay_subscription_id = response.get("id", "")
    subscription.razorpay_status = response.get("status", "")
    subscription.status = SaloonSubscription.STATUS_PENDING_AUTH
    subscription.notes.update({"latest_create_response": response.get("short_url", "")})
    subscription.save(
        update_fields=[
            "razorpay_plan_id",
            "razorpay_subscription_id",
            "razorpay_status",
            "status",
            "notes",
            "updated_at",
        ]
    )

    return JsonResponse(
        {
            "ok": True,
            "key": os.getenv("RAZORPAY_KEY_ID", "").strip(),
            "subscription_id": subscription.razorpay_subscription_id,
            "name": "ELEGENTRA",
            "description": "First month free, then ₹699 every month via UPI Autopay.",
            "vendor_name": saloon.name,
            "prefill": {
                "name": request.user.get_full_name() or request.user.username,
                "email": request.user.email,
                "contact": saloon.whatsapp_number,
            },
            "theme": {"color": "#C59A43"},
        }
    )



def dashboard_subscription_recurring_verify(request, username):
    return dashboard_subscription_verify(request, username)

