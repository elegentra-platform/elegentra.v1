import random
import os
import json
import math
import hmac
import base64
import hashlib
import re
from io import BytesIO
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
from django.db import IntegrityError
from django.db.models import Avg, Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError
import qrcode

from public.models import UserNotification
from core.image_processing import optimize_public_image_upload
from services.models import Service, ServiceInterest
from plans.models import SaloonSubscription, SubscriptionWebhookEvent
from plans.billing import (
    apply_razorpay_subscription_event,
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
    SaloonContactClick,
    GalleryView,
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
        return None, redirect("public_home")

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
        .select_related("category", "category__main_category", "main_category")
    )
    gallery_posts = GalleryPost.objects.filter(saloon=saloon)
    reviews = SaloonReview.objects.filter(saloon=saloon, is_visible=True)
    profile_views = SaloonProfileView.objects.filter(saloon=saloon)
    map_clicks = SaloonMapClick.objects.filter(saloon=saloon)
    whatsapp_clicks = SaloonContactClick.objects.filter(saloon=saloon, kind=SaloonContactClick.WHATSAPP)
    phone_clicks = SaloonContactClick.objects.filter(saloon=saloon, kind=SaloonContactClick.CALL)
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
    gallery_views_total = GalleryView.objects.filter(saloon=saloon).count() + (gallery_posts.aggregate(total=Sum("views")).get("total") or 0)
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
        "whatsapp_click_total": whatsapp_clicks.count(),
        "phone_click_total": phone_clicks.count(),
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
    subscription = _sync_local_subscription_state(subscription)
    is_trial_active = subscription.trial_ends_at > timezone.now()
    has_access = subscription_has_platform_access(subscription)
    # Autopay only counts as "ready" while it is actually keeping the salon unlocked.
    # An old confirmation from a lapsed subscription must not show as active.
    is_cancelled = subscription.status == SaloonSubscription.STATUS_CANCELLED
    autopay_ready = bool(subscription.autopay_confirmed_at) and has_access and not is_cancelled
    next_charge_date = subscription.trial_ends_at if is_trial_active else subscription.current_period_end

    if is_cancelled and has_access:
        # Autopay stopped (UPI app, bank or dashboard) but the paid period is still running.
        billing_state = "cancelled_active"
    elif is_cancelled:
        billing_state = "cancelled_ended"
    elif is_trial_active and not autopay_ready:
        billing_state = "trial_pending"
    elif is_trial_active and autopay_ready:
        billing_state = "trial_ready"
    elif subscription.status == SaloonSubscription.STATUS_PAST_DUE and has_access:
        billing_state = "past_due"
    elif autopay_ready:
        billing_state = "ended_ready"
    elif subscription.autopay_confirmed_at:
        # Was paying before, but the period ended without a successful renewal.
        billing_state = "expired"
    else:
        billing_state = "ended_pending"

    return {
        "is_trial_active": is_trial_active,
        "trial_days_left": subscription.trial_days_left,
        "autopay_ready": autopay_ready,
        "next_charge_date": next_charge_date,
        "billing_state": billing_state,
        "access_until": subscription_access_until(subscription),
    }


def _checkout_description(subscription, plan):
    amount = f"₹{plan.amount:.0f}"
    now = timezone.now()
    if subscription.trial_ends_at > now:
        return f"Free until your trial ends, then {amount} every month via UPI Autopay."
    if (
        subscription.status == SaloonSubscription.STATUS_CANCELLED
        and subscription.current_period_end
        and subscription.current_period_end > now
    ):
        return f"Nothing to pay today. {amount} every month from {subscription.current_period_end:%d %b %Y} via UPI Autopay."
    return f"{amount} every month via UPI Autopay."


def _reconcile_lapsed_razorpay_subscription(subscription):
    """
    Look up a lapsed salon's previous Razorpay subscription before starting a new one.

    Returns:
      "recovered" - Razorpay says it is active and paid into the future (we missed the
                    renewal webhook); local period is updated and the salon is unlocked.
      "cleared"   - the old subscription is finished, or was still live and is now
                    cancelled, so a new subscription can be created safely.
      "error"     - Razorpay could not be reached or the cancel failed; do not create a
                    new subscription (it could charge the owner twice).
    """
    old_id = subscription.razorpay_subscription_id
    try:
        remote = _razorpay_api_request("GET", f"/v1/subscriptions/{old_id}")
    except RuntimeError:
        return "error"

    remote_status = (remote.get("status") or "").lower()
    current_start = remote.get("current_start")
    current_end = remote.get("current_end")
    now = timezone.now()

    if remote_status == "active" and current_end:
        period_end = datetime.fromtimestamp(current_end, tz=timezone.get_current_timezone())
        if period_end > now:
            if current_start:
                subscription.current_period_start = datetime.fromtimestamp(
                    current_start, tz=timezone.get_current_timezone()
                )
            subscription.current_period_end = period_end
            subscription.razorpay_status = remote_status
            subscription.status = SaloonSubscription.STATUS_ACTIVE
            subscription.save(
                update_fields=[
                    "current_period_start",
                    "current_period_end",
                    "razorpay_status",
                    "status",
                    "updated_at",
                ]
            )
            return "recovered"

    if remote_status in {"created", "authenticated", "active", "pending", "halted"}:
        try:
            _razorpay_api_request("POST", f"/v1/subscriptions/{old_id}/cancel", {"cancel_at_cycle_end": 0})
        except RuntimeError:
            return "error"
        subscription.razorpay_status = "cancelled"
        subscription.notes = {**(subscription.notes or {}), "replaced_razorpay_subscription_id": old_id}
        subscription.save(update_fields=["razorpay_status", "notes", "updated_at"])

    return "cleared"


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
    elif billing_state == "past_due":
        status_title = "Payment due"
        status_note = "Your last renewal payment did not go through. Your salon stays active during the grace period."
    elif billing_state == "expired":
        status_title = "Subscription expired"
        status_note = "We did not receive your renewal payment, so your salon tools and public profile are paused. Renew to unlock them."
    elif billing_state == "cancelled_active":
        status_title = "Autopay cancelled"
        status_note = "Autopay is off. Your salon stays active until the end of the period you already paid for."
    elif billing_state == "cancelled_ended":
        status_title = "Subscription ended"
        status_note = "Autopay was cancelled and your paid period has ended. Renew to unlock your salon again."
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
            "access_until": billing["access_until"],
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
    subscription.cancelled_at = None
    subscription.cancel_at_cycle_end = False
    subscription.grace_period_started_at = None
    subscription.grace_period_ends_at = None

    if subscription.trial_ends_at > now:
        # Nothing is paid yet: access runs to the trial end. The first charge happens at
        # trial end and its `subscription.charged` webhook extends the period.
        subscription.status = SaloonSubscription.STATUS_TRIALING
        subscription.current_period_start = subscription.trial_started_at
        subscription.current_period_end = subscription.trial_ends_at
    elif subscription.current_period_end and subscription.current_period_end > now:
        # Autopay turned back on before the paid month ran out. The new mandate starts
        # charging at that date, so keep the already-paid period as it is.
        subscription.status = SaloonSubscription.STATUS_ACTIVE
    else:
        # Mandate with an immediate first charge.
        subscription.status = SaloonSubscription.STATUS_ACTIVE
        subscription.current_period_start = now
        subscription.current_period_end = now + timedelta(days=30)

    subscription.save(
        update_fields=[
            "auth_payment_id",
            "autopay_confirmed_at",
            "razorpay_status",
            "cancelled_at",
            "cancel_at_cycle_end",
            "grace_period_started_at",
            "grace_period_ends_at",
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
    # Razorpay may deliver the same event more than once; its event id makes it idempotent.
    event_id = request.headers.get("X-Razorpay-Event-Id", "").strip() or None

    if event_id and SubscriptionWebhookEvent.objects.filter(idempotency_key=event_id).exists():
        return HttpResponse(status=200)

    try:
        event_log = SubscriptionWebhookEvent.objects.create(
            event_name=event_name,
            razorpay_subscription_id=subscription_id,
            idempotency_key=event_id,
            webhook_signature=request.headers.get("X-Razorpay-Signature", "")[:512],
            verification_status="verified" if webhook_secret else "unverified",
            payload=payload,
        )
    except IntegrityError:
        # A parallel delivery of the same event got stored first.
        return HttpResponse(status=200)

    subscription = None
    if subscription_id and event_name.startswith("subscription."):
        subscription = (
            SaloonSubscription.objects
            .filter(razorpay_subscription_id=subscription_id)
            .order_by("-created_at", "-id")
            .first()
        )

    if not subscription:
        # Unknown, or an old mandate we already replaced: log only.
        event_log.processing_status = SubscriptionWebhookEvent.STATUS_PROCESSED
        event_log.error_message = "No matching active subscription."
        event_log.save(update_fields=["processing_status", "error_message"])
        return HttpResponse(status=200)

    try:
        apply_razorpay_subscription_event(subscription, event_name, payload)
    except Exception as exc:  # keep the log so it can be replayed
        event_log.processing_status = SubscriptionWebhookEvent.STATUS_FAILED
        event_log.error_message = str(exc)[:2000]
        event_log.save(update_fields=["processing_status", "error_message"])
        raise

    event_log.processing_status = SubscriptionWebhookEvent.STATUS_PROCESSED
    event_log.save(update_fields=["processing_status"])
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

    # ÃƒÂ°Ã…Â¸Ã¢â‚¬ËœÃ¢â‚¬Â¡ UNDO SESSION HANDLING (ONE-TIME)
    undo_service_ids = request.session.pop("undo_service_ids", None)

    return render(
        request,
        "saloons/dashboard/services.html",
        {
            "saloon": saloon,
            "services": services,
            "active_tab": "services",
            "undo_service_ids": undo_service_ids,  # ÃƒÂ°Ã…Â¸Ã¢â‚¬ËœÃ‹â€  pass to template
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
            saloon.banner_image = optimize_public_image_upload(banner_image)
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


POSTER_WIDTH = 1080
POSTER_HEIGHT = 1920
POSTER_EXPORT_SIZE = (900, 1600)
POSTER_BG = "#f7f0e6"
POSTER_INK = "#2f261f"
POSTER_MUTED = "#7d6a55"
POSTER_GOLD = "#c79a43"


def _poster_font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()



def _poster_poppins_font(size, bold=True):
    candidates = [
        "static/fonts/Poppins-Bold.ttf" if bold else "static/fonts/Poppins-Regular.ttf",
        "staticfiles/fonts/Poppins-Bold.ttf" if bold else "staticfiles/fonts/Poppins-Regular.ttf",
        "C:/Windows/Fonts/Poppins-Bold.ttf" if bold else "C:/Windows/Fonts/Poppins-Regular.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    return _poster_font(size, bold=bold)

def _poster_serif_font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/cambriab.ttf" if bold else "C:/Windows/Fonts/cambria.ttc",
        "C:/Windows/Fonts/georgiab.ttf" if bold else "C:/Windows/Fonts/georgia.ttf",
        "C:/Windows/Fonts/timesbd.ttf" if bold else "C:/Windows/Fonts/times.ttf",
    ]
    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    return _poster_font(size, bold=bold)



def _poster_brand_font(size):
    try:
        return ImageFont.truetype("static/fonts/bauserif.ttf", size=size)
    except OSError:
        return _poster_serif_font(size, bold=False)


def _poster_favicon(size):
    for icon_path in ["static/images/favicon.png", "staticfiles/images/favicon.png"]:
        try:
            icon = Image.open(icon_path).convert("RGBA")
            return ImageOps.contain(icon, (size, size), method=Image.Resampling.LANCZOS)
        except (OSError, ValueError, UnidentifiedImageError):
            continue
    fallback = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fallback_draw = ImageDraw.Draw(fallback)
    fallback_draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=max(8, size // 5), fill="#fff7e8", outline="#b88a46", width=max(2, size // 28))
    fallback_draw.text((size * 0.32, size * 0.18), "E", font=_poster_brand_font(max(18, size // 2)), fill="#b88a46")
    return fallback


def _poster_draw_brand(draw, poster, xy, icon_size=54, text_size=48, fill="#5c5144"):
    icon = _poster_favicon(icon_size)
    poster.paste(icon, xy, icon)
    draw.text((xy[0] + icon_size + 12, xy[1] + icon_size * 0.06), "Elegentra", font=_poster_brand_font(text_size), fill=fill)



def _poster_icon_font(size):
    candidates = [
        "staticfiles/rest_framework/fonts/fontawesome-webfont.ttf",
        "venv/Lib/site-packages/rest_framework/static/rest_framework/fonts/fontawesome-webfont.ttf",
    ]
    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    return None


def _poster_draw_centered_icon(draw, center, codepoint, font, fill):
    icon = chr(codepoint)
    bbox = draw.textbbox((0, 0), icon, font=font)
    x = center[0] - (bbox[2] - bbox[0]) / 2 - bbox[0]
    y = center[1] - (bbox[3] - bbox[1]) / 2 - bbox[1]
    draw.text((x, y), icon, font=font, fill=fill)


def _poster_draw_whatsapp_mark(poster, center):
    icon = _poster_whatsapp_icon(72)
    x = int(center[0] - icon.width / 2)
    y = int(center[1] - icon.height / 2)
    poster.paste(icon, (x, y), icon)


def _poster_cubic_points(p0, p1, p2, p3, steps=72):
    points = []
    for index in range(steps + 1):
        t = index / steps
        mt = 1 - t
        x = mt ** 3 * p0[0] + 3 * mt ** 2 * t * p1[0] + 3 * mt * t ** 2 * p2[0] + t ** 3 * p3[0]
        y = mt ** 3 * p0[1] + 3 * mt ** 2 * t * p1[1] + 3 * mt * t ** 2 * p2[1] + t ** 3 * p3[1]
        points.append((int(round(x)), int(round(y))))
    return points


def _poster_scaled(points, scale):
    return [(int(x * scale), int(y * scale)) for x, y in points]


def _poster_bezier_path(segments, steps=64):
    points = []
    for index, (p0, p1, p2, p3) in enumerate(segments):
        segment_points = _poster_cubic_points(p0, p1, p2, p3, steps=steps)
        points.extend(segment_points if index == 0 else segment_points[1:])
    return points


def _poster_draw_contour(draw, segments, color, width=2, steps=64):
    draw.line(_poster_bezier_path(segments, steps=steps), fill=color, width=width, joint="curve")

WHATSAPP_SVG_PATH = "M476.9 161.1C435 119.1 379.2 96 319.9 96C197.5 96 97.9 195.6 97.9 318C97.9 357.1 108.1 395.3 127.5 429L96 544L213.7 513.1C246.1 530.8 282.6 540.1 319.8 540.1L319.9 540.1C442.2 540.1 544 440.5 544 318.1C544 258.8 518.8 203.1 476.9 161.1zM319.9 502.7C286.7 502.7 254.2 493.8 225.9 477L219.2 473L149.4 491.3L168 423.2L163.6 416.2C145.1 386.8 135.4 352.9 135.4 318C135.4 216.3 218.2 133.5 320 133.5C369.3 133.5 415.6 152.7 450.4 187.6C485.2 222.5 506.6 268.8 506.5 318.1C506.5 419.9 421.6 502.7 319.9 502.7zM421.1 364.5C415.6 361.7 388.3 348.3 383.2 346.5C378.1 344.6 374.4 343.7 370.7 349.3C367 354.9 356.4 367.3 353.1 371.1C349.9 374.8 346.6 375.3 341.1 372.5C308.5 356.2 287.1 343.4 265.6 306.5C259.9 296.7 271.3 297.4 281.9 276.2C283.7 272.5 282.8 269.3 281.4 266.5C280 263.7 268.9 236.4 264.3 225.3C259.8 214.5 255.2 216 251.8 215.8C248.6 215.6 244.9 215.6 241.2 215.6C237.5 215.6 231.5 217 226.4 222.5C221.3 228.1 207 241.5 207 268.8C207 296.1 226.9 322.5 229.6 326.2C232.4 329.9 268.7 385.9 324.4 410C359.6 425.2 373.4 426.5 391 423.9C401.7 422.3 423.8 410.5 428.4 397.5C433 384.5 433 373.4 431.6 371.1C430.3 368.6 426.6 367.2 421.1 364.5z"


def _poster_svg_path_polygons(path_data, steps=16):
    tokens = re.findall(r"[MmLlCcZz]|-?\d+(?:\.\d+)?", path_data)
    polygons = []
    polygon = []
    index = 0
    command = None
    current = (0.0, 0.0)
    start = (0.0, 0.0)

    def is_command(value):
        return bool(re.fullmatch(r"[MmLlCcZz]", value))

    def number():
        nonlocal index
        value = float(tokens[index])
        index += 1
        return value

    while index < len(tokens):
        if is_command(tokens[index]):
            command = tokens[index]
            index += 1
        if command in ("M", "m"):
            while index < len(tokens) and not is_command(tokens[index]):
                x, y = number(), number()
                if command == "m":
                    x += current[0]
                    y += current[1]
                current = (x, y)
                start = current
                if polygon:
                    polygons.append(polygon)
                polygon = [current]
                command = "L" if command == "M" else "l"
        elif command in ("L", "l"):
            while index < len(tokens) and not is_command(tokens[index]):
                x, y = number(), number()
                if command == "l":
                    x += current[0]
                    y += current[1]
                current = (x, y)
                polygon.append(current)
        elif command in ("C", "c"):
            while index < len(tokens) and not is_command(tokens[index]):
                p0 = current
                p1 = (number(), number())
                p2 = (number(), number())
                p3 = (number(), number())
                if command == "c":
                    p1 = (p1[0] + current[0], p1[1] + current[1])
                    p2 = (p2[0] + current[0], p2[1] + current[1])
                    p3 = (p3[0] + current[0], p3[1] + current[1])
                for point in _poster_cubic_points(p0, p1, p2, p3, steps=steps)[1:]:
                    polygon.append(point)
                current = p3
        elif command in ("Z", "z"):
            if polygon:
                polygon.append(start)
                polygons.append(polygon)
                polygon = []
            current = start
            command = None
        else:
            break
    if polygon:
        polygons.append(polygon)
    return polygons


def _poster_whatsapp_icon(size=74):
    render_size = size * 4
    scale = render_size / 640
    mask = Image.new("L", (render_size, render_size), 0)
    for polygon in _poster_svg_path_polygons(WHATSAPP_SVG_PATH, steps=24):
        points = [(int(x * scale), int(y * scale)) for x, y in polygon]
        poly_mask = Image.new("L", (render_size, render_size), 0)
        ImageDraw.Draw(poly_mask).polygon(points, fill=255)
        mask = ImageChops.difference(mask, poly_mask)

    icon = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    gold = Image.new("RGBA", (render_size, render_size), (177, 138, 85, 255))
    icon.paste(gold, (0, 0), mask)
    return icon.resize((size, size), Image.Resampling.LANCZOS)


def _poster_draw_service_icon(draw, center, kind, color="#5A4938"):
    icon_font = _poster_icon_font(58)
    kind = (kind or "").upper()
    codepoint = 0xf005
    if "COLOR" in kind or "COLOUR" in kind:
        codepoint = 0xf0d0  # magic wand
    elif "HAIR" in kind or "CUT" in kind:
        codepoint = 0xf0c4  # scissors
    elif "NAIL" in kind:
        codepoint = 0xf0c3  # flask / bottle-like glyph
    elif "BRID" in kind or "MAKE" in kind:
        codepoint = 0xf182  # female
    elif "SKIN" in kind or "BEAUT" in kind:
        codepoint = 0xf06c  # leaf
    if icon_font:
        _poster_draw_centered_icon(draw, center, codepoint, icon_font, color)
        return

    x, y = center
    initial = kind[:1] or "E"
    font = _poster_serif_font(46, bold=True)
    bbox = draw.textbbox((0, 0), initial, font=font)
    draw.text((x - (bbox[2] - bbox[0]) / 2, y - (bbox[3] - bbox[1]) / 2 - 5), initial, font=font, fill=color)


def _poster_draw_info_icon(draw, center, kind, color="#5A4938", poster=None):
    icon_font = _poster_icon_font(58)
    if kind == "location" and icon_font:
        _poster_draw_centered_icon(draw, center, 0xf041, icon_font, color)
        return
    if kind == "star" and icon_font:
        _poster_draw_centered_icon(draw, center, 0xf006, icon_font, color)
        return
    if kind == "whatsapp":
        if poster is not None:
            _poster_draw_whatsapp_mark(poster, center)
        return

    x, y = center
    draw.ellipse((x - 28, y - 28, x + 28, y + 28), outline=color, width=4)


def _poster_text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]



def _poster_fit_single_line(draw, text, font, max_width):
    text = str(text or "").strip()
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "..."
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1].rstrip()
    return (text + ellipsis) if text else ellipsis

def _poster_fit_font(draw, text, max_width, start_size, min_size=12, bold=False):
    text = str(text or "").strip()
    for size in range(start_size, min_size - 1, -1):
        font = _poster_font(size, bold=bold)
        if draw.textlength(text, font=font) <= max_width:
            return font
    return _poster_font(min_size, bold=bold)


def _poster_wrap_lines_no_ellipsis(draw, text, font, max_width, max_lines=2):
    words = str(text or "").strip().split()
    if not words:
        return []
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines[:max_lines]


def _poster_wrapped_font_no_ellipsis(draw, text, max_width, start_size=18, min_size=11, max_lines=2):
    for size in range(start_size, min_size - 1, -1):
        font = _poster_font(size)
        lines = _poster_wrap_lines_no_ellipsis(draw, text, font, max_width, max_lines + 1)
        if len(lines) <= max_lines:
            return font, lines
    font = _poster_font(min_size)
    return font, _poster_wrap_lines_no_ellipsis(draw, text, font, max_width, max_lines)
def _poster_tracking_width(draw, text, font, tracking=0):
    chars = list(str(text or ""))
    if not chars:
        return 0
    return sum(draw.textlength(char, font=font) for char in chars) + tracking * (len(chars) - 1)


def _poster_draw_tracked_text(draw, xy, text, font, fill, tracking=0):
    x, y = xy
    for char in str(text or ""):
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + tracking


def _poster_wrapped_lines(draw, text, font, max_width, max_lines=2):
    words = str(text or "").split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        if _poster_text_size(draw, test, font)[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if words and len(lines) == max_lines:
        full = " ".join(words)
        if " ".join(lines) != full:
            last = lines[-1]
            while last and _poster_text_size(draw, last + "...", font)[0] > max_width:
                last = last[:-1].rstrip()
            lines[-1] = (last + "...") if last else "..."
    return lines


def _poster_rounded_rect(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _poster_cover_image(image, size):
    image = image.convert("RGB")
    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def _poster_saloon_banner(saloon):
    if saloon.banner_image:
        try:
            with saloon.banner_image.open("rb") as banner_file:
                return Image.open(banner_file).convert("RGB")
        except (OSError, ValueError, UnidentifiedImageError):
            pass
    fallback = Image.new("RGB", (POSTER_WIDTH, 820), "#dfd2bf")
    draw = ImageDraw.Draw(fallback)
    for y in range(fallback.height):
        ratio = y / max(fallback.height - 1, 1)
        r = int(232 - ratio * 40)
        g = int(222 - ratio * 44)
        b = int(204 - ratio * 52)
        draw.line([(0, y), (fallback.width, y)], fill=(r, g, b))
    return fallback



def _poster_qr_image(url):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, border=2, box_size=12)
    qr.add_data(url)
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color="#2f261f", back_color="white").convert("RGBA")
    icon_size = max(44, qr_image.size[0] // 5)
    icon = _poster_favicon(icon_size)
    pad = 12
    badge_size = icon_size + pad * 2
    badge = Image.new("RGBA", (badge_size, badge_size), (255, 250, 241, 255))
    badge_draw = ImageDraw.Draw(badge)
    badge_draw.rounded_rectangle((0, 0, badge_size - 1, badge_size - 1), radius=18, outline="#d2aa5c", width=3)
    badge.paste(icon, (pad, pad), icon)
    qr_image.paste(badge, ((qr_image.size[0] - badge_size) // 2, (qr_image.size[1] - badge_size) // 2), badge)
    return qr_image.convert("RGB")

def _generate_saloon_share_poster(request, saloon):
    profile = getattr(saloon, "profile", None)
    public_url = build_saloon_public_url(saloon.slug, request=request)
    salon_name = (getattr(profile, "saloon_name", "") or saloon.name or "Elegentra Salon").strip()
    locality = getattr(profile, "locality", "") or ""
    city = getattr(profile, "city", "") or ""
    location = ", ".join([part for part in [locality, city] if part]) or "Your Area, City"
    contact = getattr(profile, "contact_number", "") or saloon.whatsapp_number or ""
    reviews = SaloonReview.objects.filter(saloon=saloon, is_visible=True)
    review_count = reviews.count()
    rating_average = reviews.aggregate(avg=Avg("rating")).get("avg")

    services = list(
        Service.objects.filter(saloon=saloon, is_visible=True, is_deleted=False)
        .order_by("name")
        .values_list("name", flat=True)[:5]
    )
    service_labels = services or ["Hair", "Beauty", "Skin Care", "Nail Art", "Bridal"]

    warm_white = "#FAF9F6"
    soft_cream = "#F3EEE6"
    light_beige = "#E8DED0"
    warm_taupe = "#B9945E"
    muted_gold = "#C5964E"
    charcoal = "#242321"
    deep_brown = "#4A392B"

    poster = Image.new("RGB", (POSTER_WIDTH, POSTER_HEIGHT), warm_white)
    draw = ImageDraw.Draw(poster)

    # Top banner image, neutral enough to work with any salon colors.
    banner_height = 790
    banner = _poster_cover_image(_poster_saloon_banner(saloon), (POSTER_WIDTH, banner_height))
    banner = ImageEnhance.Brightness(banner).enhance(1.06)
    banner = ImageEnhance.Contrast(banner).enhance(1.04)
    poster.paste(banner, (0, 0))



    # Organic cream wave over the lower image area, drawn as a Bezier mask.
    wave = Image.new("RGBA", (POSTER_WIDTH, POSTER_HEIGHT), (0, 0, 0, 0))
    wave_scale = 3
    wave_mask = Image.new("L", (POSTER_WIDTH * wave_scale, POSTER_HEIGHT * wave_scale), 0)
    wave_mask_draw = ImageDraw.Draw(wave_mask)
    curve = []
    curve += _poster_cubic_points((-90, 438), (42, 520), (178, 600), (340, 628), steps=56)
    curve += _poster_cubic_points((340, 628), (516, 656), (706, 666), (872, 696), steps=46)[1:]
    curve += _poster_cubic_points((872, 696), (974, 716), (1062, 744), (POSTER_WIDTH + 90, 772), steps=44)[1:]
    wave_points = curve + [(POSTER_WIDTH + 90, POSTER_HEIGHT), (-90, POSTER_HEIGHT)]
    wave_mask_draw.polygon(_poster_scaled(wave_points, wave_scale), fill=255)
    wave_mask = wave_mask.resize((POSTER_WIDTH, POSTER_HEIGHT), Image.Resampling.LANCZOS)

    wave_shadow = Image.new("RGBA", (POSTER_WIDTH, POSTER_HEIGHT), (0, 0, 0, 0))
    wave_shadow_draw = ImageDraw.Draw(wave_shadow)
    wave_shadow_draw.line(curve, fill=(78, 58, 38, 36), width=16, joint="curve")
    wave_shadow = wave_shadow.filter(ImageFilter.GaussianBlur(12))
    poster = Image.alpha_composite(poster.convert("RGBA"), wave_shadow).convert("RGB")

    wave_fill = Image.new("RGBA", (POSTER_WIDTH, POSTER_HEIGHT), (250, 249, 246, 255))
    wave.paste(wave_fill, (0, 0), wave_mask)

    abstract = Image.new("RGBA", (POSTER_WIDTH, POSTER_HEIGHT), (0, 0, 0, 0))
    abstract_draw = ImageDraw.Draw(abstract)
    abstract_draw.ellipse((-330, 492, 78, 918), fill=(243, 238, 230, 118))

    wave = Image.alpha_composite(abstract, wave)
    poster = Image.alpha_composite(poster.convert("RGBA"), wave).convert("RGB")
    draw = ImageDraw.Draw(poster)

    # Editorial contour paths that follow the organic image transition.
    contour = Image.new("RGBA", (POSTER_WIDTH, POSTER_HEIGHT), (0, 0, 0, 0))
    contour_draw = ImageDraw.Draw(contour)
    hero_paths = [
        ([((-70, 538), (72, 592), (218, 630), (382, 652)), ((382, 652), (560, 675), (742, 682), (900, 704)), ((900, 704), (1010, 720), (1086, 744), (POSTER_WIDTH + 76, 770))], (177, 138, 85, 178), 3),
        ([((-86, 558), (92, 612), (250, 646), (422, 668)), ((422, 668), (596, 690), (764, 696), (920, 720)), ((920, 720), (1020, 736), (1092, 758), (POSTER_WIDTH + 88, 786))], (90, 73, 56, 120), 2),
        ([((-38, 516), (116, 578), (286, 622), (444, 642)), ((444, 642), (608, 664), (760, 670), (888, 690)), ((888, 690), (994, 706), (1070, 724), (POSTER_WIDTH + 62, 748))], (216, 199, 173, 144), 2),
    ]
    for segments, color, width in hero_paths:
        _poster_draw_contour(contour_draw, segments, color, width=width, steps=58)
    poster = Image.alpha_composite(poster.convert("RGBA"), contour).convert("RGB")
    draw = ImageDraw.Draw(poster)

    # Decorative organic bottom-right contours, cropped naturally by the poster edges.
    deco = Image.new("RGBA", (POSTER_WIDTH, POSTER_HEIGHT), (0, 0, 0, 0))
    deco_draw = ImageDraw.Draw(deco)
    bottom_blob_segments = [
        ((POSTER_WIDTH + 140, 1508), (1032, 1528), (914, 1618), (862, 1738)),
        ((862, 1738), (812, 1854), (840, 1948), (POSTER_WIDTH + 94, POSTER_HEIGHT + 82)),
    ]
    bottom_blob = _poster_bezier_path(bottom_blob_segments, steps=78)
    bottom_blob += [(POSTER_WIDTH + 150, POSTER_HEIGHT + 90), (POSTER_WIDTH + 150, 1504)]
    deco_draw.polygon(bottom_blob, fill=(243, 238, 230, 116))
    bottom_paths = [
        ([((POSTER_WIDTH + 42, 1566), (984, 1592), (858, 1702), (774, 1878)), ((774, 1878), (748, 1942), (742, 1992), (746, POSTER_HEIGHT + 42))], (177, 138, 85, 188), 4),
        ([((POSTER_WIDTH + 58, 1608), (1012, 1628), (902, 1730), (824, 1894)), ((824, 1894), (798, 1952), (790, 2004), (796, POSTER_HEIGHT + 54))], (138, 115, 90, 132), 3),
        ([((POSTER_WIDTH + 82, 1660), (1030, 1682), (936, 1778), (876, 1916)), ((876, 1916), (854, 1972), (852, 2020), (862, POSTER_HEIGHT + 66))], (216, 199, 173, 132), 2),
        ([((POSTER_WIDTH + 108, 1714), (1050, 1738), (972, 1824), (930, 1944)), ((930, 1944), (914, 1992), (916, 2032), (930, POSTER_HEIGHT + 72))], (232, 222, 208, 112), 2),
    ]
    for segments, color, width in bottom_paths:
        _poster_draw_contour(deco_draw, segments, color, width=width, steps=80)
    poster = Image.alpha_composite(poster.convert("RGBA"), deco).convert("RGB")
    draw = ImageDraw.Draw(poster)
    # Salon name, centered and editorial. Elegentra stays secondary.
    name_font = _poster_poppins_font(120, bold=True)
    name_gold_font = _poster_poppins_font(120, bold=True)
    words = salon_name.split()
    if len(words) > 1:
        first_part = " ".join(words[:-1])
        last_part = words[-1]
    else:
        first_part = salon_name
        last_part = ""
    first_w, _ = _poster_text_size(draw, first_part, name_font)
    last_w, _ = _poster_text_size(draw, last_part, name_gold_font) if last_part else (0, 0)
    gap = 14 if last_part else 0
    total_w = first_w + gap + last_w
    title_y = 740
    if total_w <= POSTER_WIDTH - 116:
        text_x = (POSTER_WIDTH - total_w) // 2
        draw.text((text_x, title_y), first_part, font=name_font, fill=deep_brown)
        if last_part:
            draw.text((text_x + first_w + gap, title_y + 2), last_part, font=name_gold_font, fill=muted_gold)
    else:
        for line in _poster_wrapped_lines(draw, salon_name, name_font, POSTER_WIDTH - 142, max_lines=2):
            lw, _ = _poster_text_size(draw, line, name_font)
            draw.text(((POSTER_WIDTH - lw) // 2, title_y), line, font=name_font, fill=charcoal)
            title_y += 110
    draw.line((486, 892, 594, 892), fill=muted_gold, width=3)

    # Services card.
    service_card = (42, 958, POSTER_WIDTH - 42, 1164)
    service_shadow = Image.new("RGBA", (POSTER_WIDTH, POSTER_HEIGHT), (0, 0, 0, 0))
    service_shadow_draw = ImageDraw.Draw(service_shadow)
    service_shadow_draw.rounded_rectangle((service_card[0], service_card[1] + 12, service_card[2], service_card[3] + 12), radius=20, fill=(72, 52, 32, 18))
    service_shadow = service_shadow.filter(ImageFilter.GaussianBlur(15))
    poster = Image.alpha_composite(poster.convert("RGBA"), service_shadow).convert("RGB")
    draw = ImageDraw.Draw(poster)
    draw.rounded_rectangle(service_card, radius=20, fill="#fffdf7", outline=(232, 222, 208), width=1)

    services_heading = "A FEW OF OUR SERVICES"
    heading_font = _poster_font(16, bold=True)
    heading_w = draw.textlength(services_heading, font=heading_font)
    heading_y = service_card[1] + 32
    heading_x = (POSTER_WIDTH - heading_w) / 2
    draw.line((service_card[0] + 168, heading_y + 8, heading_x - 22, heading_y + 8), fill=light_beige, width=2)
    draw.line((heading_x + heading_w + 22, heading_y + 8, service_card[2] - 168, heading_y + 8), fill=light_beige, width=2)
    draw.text((heading_x, heading_y), services_heading, font=heading_font, fill=warm_taupe)

    display_services = service_labels[:3]
    service_count = max(len(display_services), 1)
    content_top = service_card[1] + 80
    content_bottom = service_card[3] - 54
    col_w = (service_card[2] - service_card[0] - 92) / service_count
    start_x = service_card[0] + 46
    for index, service in enumerate(display_services):
        col_left = start_x + index * col_w
        col_right = col_left + col_w
        cx = (col_left + col_right) / 2
        if index:
            draw.line((col_left - 1, content_top + 4, col_left - 1, content_bottom - 6), fill=light_beige, width=2)
        clean = str(service).strip() or "Service"
        label_size = 28
        label_font = _poster_serif_font(label_size, bold=True)
        max_label_width = col_w - 34
        while label_size > 18 and _poster_text_size(draw, clean, label_font)[0] > max_label_width:
            label_size -= 1
            label_font = _poster_serif_font(label_size, bold=True)
        label_lines = _poster_wrapped_lines(draw, clean, label_font, max_label_width, max_lines=2)
        total_label_height = len(label_lines) * (label_size + 5)
        line_y = content_top + 10 - max(0, total_label_height - label_size - 5) / 2
        for line in label_lines:
            line_w = draw.textlength(line, font=label_font)
            draw.text((cx - line_w / 2, line_y), line, font=label_font, fill=charcoal)
            line_y += label_size + 5


    scan_text = "EXPLORE ALL SERVICES ON OUR PROFILE"
    scan_font = _poster_font(15, bold=True)
    scan_w = _poster_tracking_width(draw, scan_text, scan_font, tracking=0.5)
    scan_y = service_card[3] - 34
    _poster_draw_tracked_text(draw, ((POSTER_WIDTH - scan_w) / 2, scan_y), scan_text, scan_font, warm_taupe, tracking=0.5)
    # Info strip.
    strip = (56, 1266, POSTER_WIDTH - 56, 1400)
    strip_shadow = Image.new("RGBA", (POSTER_WIDTH, POSTER_HEIGHT), (0, 0, 0, 0))
    strip_shadow_draw = ImageDraw.Draw(strip_shadow)
    strip_shadow_draw.rounded_rectangle((strip[0], strip[1] + 8, strip[2], strip[3] + 8), radius=22, fill=(72, 52, 32, 24))
    strip_shadow = strip_shadow.filter(ImageFilter.GaussianBlur(14))
    poster = Image.alpha_composite(poster.convert("RGBA"), strip_shadow).convert("RGB")
    draw = ImageDraw.Draw(poster)
    draw.rounded_rectangle(strip, radius=18, fill=soft_cream)
    item_w = (strip[2] - strip[0]) // 3
    if review_count and rating_average is not None:
        rating_title = f"{float(rating_average):.1f}"
        rating_sub = f"{review_count} Review{'s' if review_count != 1 else ''}"
    else:
        rating_title = "New"
        rating_sub = "Customer Rating"
    info_items = [
        ("location", "Our Location", location),
        ("star", rating_title, rating_sub),
        ("whatsapp", "Book Now", "via WhatsApp" if contact else "via Elegentra"),
    ]
    for index, (icon_kind, title, sub) in enumerate(info_items):
        x = strip[0] + index * item_w
        if index:
            draw.line((x, strip[1] + 24, x, strip[3] - 24), fill="#d8cfc3", width=2)
        _poster_draw_info_icon(draw, (x + 58, strip[1] + 58), icon_kind, muted_gold, poster)
        text_x = x + 116
        max_text_width = item_w - 132
        title_font = _poster_fit_font(draw, title, max_text_width, 24, min_size=18, bold=True)
        sub_font, sub_lines = _poster_wrapped_font_no_ellipsis(draw, sub, max_text_width, start_size=18, min_size=11, max_lines=2)
        draw.text((text_x, strip[1] + 30), title, font=title_font, fill=charcoal)
        for line_index, line in enumerate(sub_lines):
            draw.text((text_x, strip[1] + 64 + line_index * 20), line, font=sub_font, fill=deep_brown)

    # QR + brand footer.
    qr_size = 218
    qr = _poster_qr_image(public_url).resize((qr_size, qr_size), Image.Resampling.NEAREST)
    qr_y = 1478
    cta_font = _poster_font(16, bold=True)
    detail_font = _poster_font(14)
    find_font = _poster_font(23)
    cta_text = "SCAN TO VIEW OUR PROFILE"
    detail_text = "Reviews \u00b7 Services \u00b7 Photos \u00b7 Details"
    brand_icon_size = 76
    brand_text_size = 62
    brand_width = brand_icon_size + 18 + int(draw.textlength("Elegentra", font=_poster_brand_font(brand_text_size)))
    left_col_width = max(
        qr_size + 28,
        int(_poster_tracking_width(draw, cta_text, cta_font, tracking=1)),
        int(draw.textlength(detail_text, font=detail_font)),
    )
    col_gap = 38
    footer_width = left_col_width + col_gap + brand_width
    footer_x = (POSTER_WIDTH - footer_width) // 2
    qr_x = footer_x + (left_col_width - qr_size) // 2
    brand_x = footer_x + left_col_width + col_gap
    qr_shadow = Image.new("RGBA", (POSTER_WIDTH, POSTER_HEIGHT), (0, 0, 0, 0))
    qr_shadow_draw = ImageDraw.Draw(qr_shadow)
    qr_shadow_draw.rounded_rectangle((qr_x - 10, qr_y - 6, qr_x + qr_size + 18, qr_y + qr_size + 22), radius=12, fill=(72, 52, 32, 24))
    qr_shadow = qr_shadow.filter(ImageFilter.GaussianBlur(14))
    poster = Image.alpha_composite(poster.convert("RGBA"), qr_shadow).convert("RGB")
    draw = ImageDraw.Draw(poster)
    draw.rounded_rectangle((qr_x - 14, qr_y - 14, qr_x + qr_size + 14, qr_y + qr_size + 14), radius=8, fill="#fffdf7", outline=muted_gold, width=3)
    poster.paste(qr, (qr_x, qr_y))

    brand_group_height = 23 + 18 + brand_icon_size
    brand_y = qr_y + (qr_size - brand_group_height) // 2 - 10
    draw.text((brand_x, brand_y), "Find Us On", font=find_font, fill=deep_brown)
    _poster_draw_brand(draw, poster, (brand_x, brand_y + 41), icon_size=brand_icon_size, text_size=brand_text_size, fill=charcoal)

    qr_center_x = qr_x + qr_size / 2
    cta_x = qr_center_x - _poster_tracking_width(draw, cta_text, cta_font, tracking=1) / 2
    detail_x = qr_center_x - draw.textlength(detail_text, font=detail_font) / 2
    _poster_draw_tracked_text(draw, (cta_x, qr_y + qr_size + 40), cta_text, cta_font, warm_taupe, tracking=1)
    draw.text((detail_x, qr_y + qr_size + 68), detail_text, font=detail_font, fill="#8A735A")
    if poster.size != POSTER_EXPORT_SIZE:
        poster = poster.resize(POSTER_EXPORT_SIZE, Image.Resampling.LANCZOS)
    return poster
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
def dashboard_share_poster_preview(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    _sync_slug_from_saloon_name_if_default(saloon)

    return render(
        request,
        "saloons/dashboard/share_poster_preview.html",
        {
            "saloon": saloon,
            "poster_url": reverse("saloon_dashboard_share_poster", kwargs={"username": saloon.owner.username}),
            "back_url": reverse("saloon_dashboard_mysaloon", kwargs={"username": saloon.owner.username}),
            "public_url": build_saloon_public_url(saloon.slug, request=request),
        },
    )


@login_required
def dashboard_share_poster(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    _sync_slug_from_saloon_name_if_default(saloon)

    poster = _generate_saloon_share_poster(request, saloon)
    buffer = BytesIO()
    poster.save(buffer, format="PNG", optimize=True)
    filename = normalize_saloon_slug(saloon.name or saloon.owner.username) or "elegentra-salon"
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Content-Disposition"] = f'inline; filename="{filename}-share-poster.png"'
    response["Cache-Control"] = "no-store, max-age=0"
    return response


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
            saloon.banner_image = optimize_public_image_upload(banner_image)
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
    subscription = _sync_local_subscription_state(subscription)
    has_access = subscription_has_platform_access(subscription)

    if subscription.autopay_confirmed_at and not has_access and subscription.razorpay_subscription_id:
        # Lapsed subscription being renewed. Check the old Razorpay subscription first so we
        # never leave two live mandates (double charging) or charge for a period already paid.
        outcome = _reconcile_lapsed_razorpay_subscription(subscription)
        if outcome == "recovered":
            return JsonResponse({
                "ok": True,
                "already_active": True,
                "subscription_id": subscription.razorpay_subscription_id,
                "message": "Your renewal payment was found. Your salon is active again.",
                "redirect_url": reverse("saloon_dashboard_subscription", kwargs={"username": saloon.owner.username}),
            })
        if outcome == "error":
            return JsonResponse(
                {
                    "ok": False,
                    "message": "We could not check your previous autopay. Please try again in a minute or contact support.",
                },
                status=400,
            )
        # outcome == "cleared": the old mandate is closed, fall through and create a new one.
    elif (
        subscription.autopay_confirmed_at
        and has_access
        and subscription.status != SaloonSubscription.STATUS_CANCELLED
    ):
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
                "description": _checkout_description(subscription, plan),
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
        # First charge only after everything already granted: the trial, or the month the
        # owner already paid for before cancelling autopay. Never charge twice for a period.
        start_at = max(
            [value for value in (subscription.trial_ends_at, subscription.current_period_end, now) if value]
            if subscription.status == SaloonSubscription.STATUS_CANCELLED
            else [subscription.trial_ends_at, now]
        )
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
    if subscription.status != SaloonSubscription.STATUS_CANCELLED:
        # A cancelled salon keeps its "cancelled, active until …" state until the new
        # mandate is actually approved in verify.
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
            "description": _checkout_description(subscription, plan),
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


@login_required
@require_POST
def dashboard_subscription_cancel(request, username):
    """Owner stops autopay from Elegentra. Access continues until the paid period ends."""
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return JsonResponse({"ok": False, "message": "Please sign in again."}, status=403)

    subscription, _ = _get_or_create_saloon_subscription(saloon)
    subscription = _sync_local_subscription_state(subscription)

    if subscription.status == SaloonSubscription.STATUS_CANCELLED:
        return JsonResponse({"ok": True, "message": "Autopay is already cancelled."})
    if not subscription.razorpay_subscription_id or not subscription.autopay_confirmed_at:
        return JsonResponse({"ok": False, "message": "There is no active autopay to cancel."}, status=400)

    try:
        # Stop the mandate now so no further debits happen; our own access rules keep
        # the salon open until the period that is already paid for runs out.
        _razorpay_api_request(
            "POST",
            f"/v1/subscriptions/{subscription.razorpay_subscription_id}/cancel",
            {"cancel_at_cycle_end": 0},
        )
    except RuntimeError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)

    now = timezone.now()
    subscription.status = SaloonSubscription.STATUS_CANCELLED
    subscription.razorpay_status = "cancelled"
    subscription.cancelled_at = now
    subscription.cancel_at_cycle_end = True
    subscription.grace_period_started_at = None
    subscription.grace_period_ends_at = None
    subscription.notes = {**(subscription.notes or {}), "autopay_stopped_reason": "cancelled_from_dashboard"}
    subscription.save()

    return JsonResponse(
        {
            "ok": True,
            "redirect_url": reverse("saloon_dashboard_subscription", kwargs={"username": saloon.owner.username}),
        }
    )




















