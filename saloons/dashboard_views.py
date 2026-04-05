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

from django.shortcuts import render, redirect
from services.utils import cleanup_expired_services
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.urls import reverse
from django.contrib import messages
from django.core.mail import send_mail
from django.conf import settings
from django.db.models import Count, Sum
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from services.models import Service, ServiceInterest
from plans.models import ListingSubscriptionPlan, SaloonSubscription, SubscriptionWebhookEvent
from .models import (
    Saloon,
    TrustedDevice,
    SaloonProfile,
    GalleryPost,
    SaloonReview,
    SaloonProfileView,
    SaloonMapClick,
)
from .utils import format_whatsapp_number, send_otp_whatsapp


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


def _build_dashboard_metrics(saloon):
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

    service_total = services.count()
    gallery_post_total = gallery_posts.count()
    gallery_views_total = gallery_posts.aggregate(total=Sum("views")).get("total") or 0
    review_total = reviews.count()
    profile_view_total = profile_views.count()
    map_click_total = map_clicks.count()
    interest_total = interests.count()

    interest_queryset = list(
        services.annotate(interest_total=Count("interest_events")).order_by("-interest_total", "name")
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
    plan, _ = ListingSubscriptionPlan.objects.get_or_create(
        slug="elegentra-monthly",
        defaults={
            "name": "Elegentra Listing",
            "description": "Keep your salon live on Elegentra with discovery placement, gallery, and premium support.",
            "amount": Decimal("499.00"),
            "strike_amount": Decimal("999.00"),
            "billing_period": "monthly",
            "interval": 1,
            "trial_days": 30,
            "currency": "INR",
        },
    )
    return plan


def _sync_local_subscription_state(subscription):
    now = timezone.now()

    if subscription.status == SaloonSubscription.STATUS_CANCELLED:
        return subscription

    if subscription.current_period_end and subscription.current_period_end <= now:
        subscription.status = SaloonSubscription.STATUS_EXPIRED
    elif subscription.autopay_confirmed_at and subscription.current_period_end and subscription.current_period_end > now:
        subscription.status = SaloonSubscription.STATUS_ACTIVE
    elif subscription.trial_ends_at > now:
        subscription.status = SaloonSubscription.STATUS_TRIALING
    elif subscription.razorpay_subscription_id:
        subscription.status = SaloonSubscription.STATUS_PENDING_AUTH
    else:
        subscription.status = SaloonSubscription.STATUS_PAST_DUE

    subscription.save(update_fields=["status", "updated_at"])
    return subscription


def _get_or_create_saloon_subscription(saloon):
    plan = _get_default_subscription_plan()
    subscription = saloon.subscriptions.order_by("-created_at", "-id").first()
    if subscription:
        if subscription.plan_id != plan.id:
            subscription.plan = plan
            subscription.save(update_fields=["plan", "updated_at"])
        return _sync_local_subscription_state(subscription), plan

    trial_started_at = saloon.created_at or timezone.now()
    subscription = SaloonSubscription.objects.create(
        saloon=saloon,
        plan=plan,
        status=SaloonSubscription.STATUS_TRIALING,
        trial_started_at=trial_started_at,
        trial_ends_at=trial_started_at + timedelta(days=plan.trial_days),
        notes={"origin": "auto_trial"},
    )
    return subscription, plan


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

    # 🟡 ONBOARDING INCOMPLETE
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

    # 🟢 APPROVED → OVERVIEW
    metrics = _build_dashboard_metrics(saloon)
    subscription, plan = _get_or_create_saloon_subscription(saloon)
    return render(
        request,
        "saloons/dashboard/overview.html",
        {
            "saloon": saloon,
            "active_tab": "dashboard",
            "subscription": subscription,
            "subscription_plan": plan,
            **metrics,
        }
    )


@login_required
def dashboard_subscription(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    subscription, plan = _get_or_create_saloon_subscription(saloon)
    is_trial_active = subscription.trial_ends_at > timezone.now()
    trial_days_left = subscription.trial_days_left
    autopay_ready = bool(subscription.autopay_confirmed_at)
    next_charge_date = subscription.trial_ends_at if is_trial_active else subscription.current_period_end

    if subscription.status == SaloonSubscription.STATUS_ACTIVE:
        status_title = "Autopay is active"
        status_note = "Your listing will renew automatically every month."
    elif subscription.status == SaloonSubscription.STATUS_PENDING_AUTH:
        status_title = "Autopay setup pending"
        status_note = "Complete the mandate once and we will charge after your free month ends."
    elif is_trial_active:
        status_title = "Free month running"
        status_note = "You are live right now. Set up UPI Autopay before the trial ends to avoid interruptions."
    else:
        status_title = "Renewal needed"
        status_note = "Your free month is over. Start autopay to keep your salon visible."

    payment_enabled = bool(os.getenv("RAZORPAY_KEY_ID", "").strip() and os.getenv("RAZORPAY_KEY_SECRET", "").strip())

    return render(
        request,
        "saloons/dashboard/subscription.html",
        {
            "saloon": saloon,
            "active_tab": "subscription",
            "plan": plan,
            "subscription": subscription,
            "status_title": status_title,
            "status_note": status_note,
            "trial_days_left": trial_days_left,
            "is_trial_active": is_trial_active,
            "autopay_ready": autopay_ready,
            "next_charge_date": next_charge_date,
            "payment_enabled": payment_enabled,
            "razorpay_key_id": os.getenv("RAZORPAY_KEY_ID", "").strip(),
        },
    )


@login_required
@require_POST
def dashboard_subscription_create(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return JsonResponse({"ok": False, "message": "Please sign in again."}, status=403)

    subscription, plan = _get_or_create_saloon_subscription(saloon)

    if subscription.autopay_confirmed_at and subscription.status in {
        SaloonSubscription.STATUS_TRIALING,
        SaloonSubscription.STATUS_ACTIVE,
    }:
        return JsonResponse(
            {"ok": False, "message": "Autopay is already connected for this salon."},
            status=400,
        )

    if subscription.razorpay_subscription_id and subscription.status == SaloonSubscription.STATUS_PENDING_AUTH:
        return JsonResponse(
            {
                "ok": True,
                "key": os.getenv("RAZORPAY_KEY_ID", "").strip(),
                "subscription_id": subscription.razorpay_subscription_id,
                "name": "ELEGENTRA",
                "description": "First month free, then INR 499 every month via UPI Autopay.",
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
            "description": "First month free, then INR 499 every month via UPI Autopay.",
            "vendor_name": saloon.name,
            "prefill": {
                "name": request.user.get_full_name() or request.user.username,
                "email": request.user.email,
                "contact": saloon.whatsapp_number,
            },
            "theme": {"color": "#C59A43"},
        }
    )


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
