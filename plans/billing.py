import hashlib
import os
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .models import ListingSubscriptionPlan, SaloonSubscription, SubscriptionPaymentHistory
from saloons.models import Saloon

FOUNDER_PARTNER_LIMIT = 25
FOUNDER_TRIAL_DAYS = 30
STANDARD_TRIAL_DAYS = 14
FOUNDER_MONTHLY_AMOUNT = Decimal("499.00")
STANDARD_MONTHLY_AMOUNT = Decimal("699.00")
GRACE_PERIOD_DAYS = 3
# Razorpay charges a renewal at the start of the new cycle and then sends the webhook.
# Keep a live autopay salon unlocked for this long past period end so that small delay
# never locks a paying salon.
RENEWAL_BUFFER = timedelta(hours=36)


def live_subscription_q(now=None):
    """
    Database version of `subscription_has_platform_access`, for listing queries
    (which salons are publicly visible). Keep the two in step.
    """
    from django.db.models import Q

    now = now or timezone.now()
    return (
        Q(trial_ends_at__gt=now)
        | Q(status=SaloonSubscription.STATUS_ACTIVE, current_period_end__isnull=True)
        | Q(status=SaloonSubscription.STATUS_ACTIVE, current_period_end__gt=now - RENEWAL_BUFFER)
        | Q(status=SaloonSubscription.STATUS_PAST_DUE, grace_period_ends_at__gt=now)
        | Q(status=SaloonSubscription.STATUS_CANCELLED, current_period_end__gt=now)
    )


def autopay_is_live(subscription):
    """Autopay is set up and has not been cancelled or paused by the owner."""
    return bool(subscription.autopay_confirmed_at) and subscription.status != SaloonSubscription.STATUS_CANCELLED


def get_default_subscription_plan():
    configured_razorpay_plan_id = os.getenv("RAZORPAY_SUBSCRIPTION_PLAN_ID", "").strip()
    plan, _ = ListingSubscriptionPlan.objects.get_or_create(
        slug="elegentra-monthly",
        defaults={
            "name": "Elegentra Listing",
            "description": "Keep your salon live on Elegentra with discovery placement, gallery, and premium support.",
            "amount": STANDARD_MONTHLY_AMOUNT,
            "strike_amount": Decimal("999.00"),
            "billing_period": "monthly",
            "interval": 1,
            "trial_days": STANDARD_TRIAL_DAYS,
            "currency": "INR",
        },
    )
    if configured_razorpay_plan_id and plan.razorpay_plan_id != configured_razorpay_plan_id:
        plan.razorpay_plan_id = configured_razorpay_plan_id
        plan.save(update_fields=["razorpay_plan_id", "updated_at"])
    return plan


def get_founder_subscription_plan():
    """₹499/month for life for the first FOUNDER_PARTNER_LIMIT approved salons."""
    configured_razorpay_plan_id = os.getenv("RAZORPAY_FOUNDER_PLAN_ID", "").strip()
    plan, _ = ListingSubscriptionPlan.objects.get_or_create(
        slug="elegentra-founder-monthly",
        defaults={
            "name": "Elegentra Founder Partner",
            "description": "Founder partner price, locked for life while the subscription stays active.",
            "amount": FOUNDER_MONTHLY_AMOUNT,
            "strike_amount": STANDARD_MONTHLY_AMOUNT,
            "billing_period": "monthly",
            "interval": 1,
            "trial_days": FOUNDER_TRIAL_DAYS,
            "currency": "INR",
        },
    )
    if configured_razorpay_plan_id and plan.razorpay_plan_id != configured_razorpay_plan_id:
        plan.razorpay_plan_id = configured_razorpay_plan_id
        plan.save(update_fields=["razorpay_plan_id", "updated_at"])
    return plan


def has_founder_price(subscription):
    """Founders keep ₹499 until a paid subscription lapses (see forfeit_founder_price)."""
    return bool(subscription.is_founder_partner) and not (subscription.notes or {}).get("founder_price_forfeited")


def plan_for_subscription(subscription):
    return get_founder_subscription_plan() if has_founder_price(subscription) else get_default_subscription_plan()


def forfeit_founder_price(subscription):
    """
    A founder whose paid subscription lapsed and who is starting again pays the
    standard price. The founder badge/number stays; only the price changes.
    """
    if not has_founder_price(subscription):
        return subscription
    notes = dict(subscription.notes or {})
    notes["founder_price_forfeited"] = True
    notes["founder_price_forfeited_at"] = timezone.now().isoformat()
    subscription.notes = notes
    subscription.plan = get_default_subscription_plan()
    subscription.save(update_fields=["notes", "plan", "updated_at"])
    return subscription


def _has_live_mandate(subscription):
    """An approved Razorpay mandate is tied to the plan it was created on; never swap it."""
    return bool(subscription.razorpay_subscription_id and subscription.autopay_confirmed_at)


def _approved_founder_count():
    return SaloonSubscription.objects.filter(is_founder_partner=True, saloon__is_test_saloon=False).count()


def founder_spots_left():
    return max(0, FOUNDER_PARTNER_LIMIT - _approved_founder_count())


@transaction.atomic
def assign_subscription_trial_on_approval(saloon):
    subscription = (
        SaloonSubscription.objects.select_for_update()
        .filter(saloon=saloon)
        .order_by("-created_at", "-id")
        .first()
    )

    if subscription and subscription.trial_assigned_at:
        if _has_live_mandate(subscription):
            return subscription, subscription.plan
        plan = plan_for_subscription(subscription)
        if subscription.plan_id != plan.id:
            subscription.plan = plan
            subscription.save(update_fields=["plan", "updated_at"])
        return subscription, plan

    approved_at = timezone.now()
    is_test_saloon = bool(getattr(saloon, "is_test_saloon", False))
    founder_number = None if is_test_saloon else _approved_founder_count() + 1
    is_founder = bool(founder_number and founder_number <= FOUNDER_PARTNER_LIMIT)
    trial_days = FOUNDER_TRIAL_DAYS if is_founder else STANDARD_TRIAL_DAYS
    plan = get_founder_subscription_plan() if is_founder else get_default_subscription_plan()
    notes = dict(getattr(subscription, "notes", {}) or {}) if subscription else {}
    notes.update({
        "origin": "approval_trial",
        "trial_rule": "test_saloon_14_day" if is_test_saloon else (f"founder_{FOUNDER_TRIAL_DAYS}_day" if is_founder else "standard_14_day"),
        "trial_assigned_from": "saloon_approval",
        "is_test_saloon": is_test_saloon,
    })

    defaults = {
        "plan": plan,
        "status": SaloonSubscription.STATUS_TRIALING,
        "trial_started_at": approved_at,
        "trial_ends_at": approved_at + timedelta(days=trial_days),
        "is_founder_partner": is_founder,
        "founder_partner_number": founder_number if is_founder else None,
        "approved_trial_days": trial_days,
        "trial_assigned_at": approved_at,
        "notes": notes,
    }

    if subscription:
        for field, value in defaults.items():
            setattr(subscription, field, value)
        subscription.save(update_fields=[*defaults.keys(), "updated_at"])
    else:
        subscription = SaloonSubscription.objects.create(saloon=saloon, **defaults)

    return subscription, plan


def get_or_create_saloon_subscription(saloon):
    subscription = saloon.subscriptions.order_by("-created_at", "-id").first()

    if not subscription and saloon.approval_status == Saloon.APPROVAL_APPROVED:
        return assign_subscription_trial_on_approval(saloon)

    if subscription:
        if not subscription.trial_assigned_at and saloon.approval_status == Saloon.APPROVAL_APPROVED:
            return assign_subscription_trial_on_approval(saloon)
        if _has_live_mandate(subscription):
            # Billing is fixed to the plan the mandate was approved on.
            plan = subscription.plan
        else:
            plan = plan_for_subscription(subscription)
            if subscription.plan_id != plan.id:
                subscription.plan = plan
                subscription.save(update_fields=["plan", "updated_at"])
        return sync_local_subscription_state(subscription), plan

    plan = get_default_subscription_plan()
    now = timezone.now()
    subscription = SaloonSubscription.objects.create(
        saloon=saloon,
        plan=plan,
        status=SaloonSubscription.STATUS_TRIALING,
        trial_started_at=now,
        trial_ends_at=now + timedelta(days=STANDARD_TRIAL_DAYS),
        approved_trial_days=STANDARD_TRIAL_DAYS,
        notes={"origin": "pre_approval_placeholder"},
    )
    return subscription, plan


def sync_local_subscription_state(subscription):
    now = timezone.now()

    if subscription.status == SaloonSubscription.STATUS_CANCELLED:
        return subscription

    if subscription.status == SaloonSubscription.STATUS_PAST_DUE:
        if subscription.grace_period_ends_at and subscription.grace_period_ends_at <= now:
            subscription.status = SaloonSubscription.STATUS_EXPIRED
            subscription.save(update_fields=["status", "updated_at"])
        return subscription

    if (
        subscription.autopay_confirmed_at
        and subscription.current_period_end
        and subscription.current_period_end + RENEWAL_BUFFER > now
    ):
        subscription.status = SaloonSubscription.STATUS_ACTIVE
    elif subscription.trial_ends_at > now:
        subscription.status = SaloonSubscription.STATUS_TRIALING
    elif subscription.current_period_end and subscription.current_period_end <= now:
        subscription.status = SaloonSubscription.STATUS_EXPIRED
    elif subscription.razorpay_subscription_id:
        subscription.status = SaloonSubscription.STATUS_PENDING_AUTH
    else:
        subscription.status = SaloonSubscription.STATUS_PAST_DUE

    subscription.save(update_fields=["status", "updated_at"])
    return subscription



def subscription_access_until(subscription):
    """The date the salon stays unlocked until (what the owner has already paid for)."""
    subscription = sync_local_subscription_state(subscription)
    candidates = [subscription.trial_ends_at, subscription.current_period_end]
    if subscription.status == SaloonSubscription.STATUS_PAST_DUE:
        candidates.append(subscription.grace_period_ends_at)
    candidates = [value for value in candidates if value]
    if not candidates:
        return None
    return max(candidates)


def subscription_has_platform_access(subscription):
    subscription = sync_local_subscription_state(subscription)
    now = timezone.now()

    if subscription.trial_ends_at and subscription.trial_ends_at > now:
        return True

    if subscription.status == SaloonSubscription.STATUS_ACTIVE:
        if not subscription.current_period_end:
            return True
        return subscription.current_period_end + RENEWAL_BUFFER > now

    if subscription.status == SaloonSubscription.STATUS_PAST_DUE:
        return bool(subscription.grace_period_ends_at and subscription.grace_period_ends_at > now)

    if subscription.status == SaloonSubscription.STATUS_CANCELLED:
        # Like Spotify/YouTube: cancelling autopay keeps access until the paid period ends.
        return bool(subscription.current_period_end and subscription.current_period_end > now)

    return False


def subscription_lock_reason(subscription):
    subscription = sync_local_subscription_state(subscription)
    if subscription.status == SaloonSubscription.STATUS_CANCELLED:
        return {
            "title": "Autopay cancelled",
            "message": "Your autopay was cancelled and your paid period has ended, so your listing is paused. Renew your subscription to make your salon public again.",
        }
    if subscription.status == SaloonSubscription.STATUS_EXPIRED:
        return {
            "title": "Renewal expired",
            "message": "Your subscription period has ended. Activate billing again to unlock your salon tools and public profile.",
        }
    if subscription.trial_ends_at and subscription.trial_ends_at <= timezone.now() and not subscription.autopay_confirmed_at:
        return {
            "title": "Trial ended",
            "message": "Your free trial is over. Subscribe to keep your salon visible and continue managing your profile.",
        }
    return {
        "title": "Subscription needed",
        "message": "Subscribe to continue using all salon tools and keep your public profile live.",
    }


def saloon_has_platform_access(saloon):
    subscription, _ = get_or_create_saloon_subscription(saloon)
    return subscription_has_platform_access(subscription)

def razorpay_subscription_status_to_local(gateway_status, subscription):
    normalized = (gateway_status or "").strip().upper().replace("_", " ")
    if normalized in {"ACTIVE", "BANK APPROVAL PENDING"}:
        if subscription.trial_ends_at > timezone.now() and not subscription.autopay_confirmed_at:
            return SaloonSubscription.STATUS_TRIALING
        return SaloonSubscription.STATUS_ACTIVE
    if normalized in {"INITIALIZED", "PENDING", "AUTHORIZATION PENDING", "APPROVAL PENDING"}:
        return SaloonSubscription.STATUS_PENDING_AUTH
    if normalized in {"ON HOLD", "FAILED", "PAYMENT FAILED", "CARD EXPIRED"}:
        return SaloonSubscription.STATUS_PAST_DUE
    if normalized in {"PAUSED", "CUSTOMER PAUSED", "COMPLETED", "CUSTOMER CANCELLED", "EXPIRED", "LINK EXPIRED", "CANCELLED"}:
        return SaloonSubscription.STATUS_CANCELLED if "CANCEL" in normalized or "PAUSED" in normalized else SaloonSubscription.STATUS_EXPIRED
    return subscription.status


def parse_razorpay_datetime(value):
    if not value:
        return None
    if isinstance(value, str):
        clean = value.replace("Z", "+00:00")
        try:
            parsed = timezone.datetime.fromisoformat(clean)
        except ValueError:
            return None
        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed)
        return parsed
    return None


def payment_method_label(details):
    auth_details = details.get("authorization_details") or details.get("authorisation_details") or {}
    method = auth_details.get("payment_method") or details.get("payment_method") or {}
    group = auth_details.get("payment_group") or details.get("payment_group") or ""
    if isinstance(method, dict):
        if "upi" in method:
            upi = method.get("upi") or {}
            upi_id = upi.get("upi_id") or upi.get("channel") or ""
            return f"UPI AutoPay {upi_id}".strip()
        if "card" in method:
            card = method.get("card") or {}
            last4 = card.get("card_number") or card.get("last4") or card.get("card_last4") or ""
            return f"Card ****{str(last4)[-4:]}" if last4 else "Card"
        if "enach" in method:
            return "eNACH"
    if isinstance(method, str) and method:
        return method
    return group or ""


def _decimal_from_payload(value):
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _payment_fingerprint(subscription, details):
    raw = "|".join([
        str(subscription.id),
        str(details.get("razorpay_payment_id") or details.get("payment_id") or ""),
        str(details.get("razorpay_payment_link_reference_id") or ""),
        str(details.get("payment_status") or ""),
        str(details.get("payment_schedule_date") or details.get("payment_time") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_subscription_payment(subscription, details, status):
    payment_id = str(details.get("razorpay_payment_id") or details.get("payment_id") or details.get("gateway_payment_id") or "")
    transaction_reference = str(details.get("razorpay_payment_link_reference_id") or details.get("bank_reference") or details.get("razorpay_order_id") or "")
    paid_at = parse_razorpay_datetime(
        details.get("payment_time") or details.get("payment_initiated_date") or details.get("payment_schedule_date")
    ) or timezone.now()
    fingerprint = _payment_fingerprint(subscription, details)
    history, created = SubscriptionPaymentHistory.objects.get_or_create(
        subscription=subscription,
        razorpay_payment_id=payment_id or fingerprint,
        defaults={
            "saloon": subscription.saloon,
            "plan": subscription.plan,
            "paid_at": paid_at,
            "amount": _decimal_from_payload(details.get("payment_amount") or details.get("authorization_amount")),
            "currency": details.get("payment_currency") or subscription.plan.currency,
            "payment_status": status,
            "payment_method": payment_method_label(details),
            "renewal_type": details.get("payment_type") or "renewal",
            "razorpay_subscription_id": details.get("subscription_id") or subscription.razorpay_subscription_id,
            "transaction_reference": transaction_reference,
            "raw_payload": details,
        },
    )
    if not created and history.payment_status != status:
        history.payment_status = status
        history.raw_payload = details
        history.save(update_fields=["payment_status", "raw_payload"])
    return history


def apply_razorpay_subscription_response(subscription, response):
    gateway_status = response.get("subscription_status", "") or response.get("status", "")
    subscription.razorpay_status = gateway_status or subscription.razorpay_status
    subscription.razorpay_subscription_id = response.get("subscription_id", "") or subscription.razorpay_subscription_id
    subscription.razorpay_customer_id = response.get("subscription_session_id", "") or subscription.razorpay_customer_id

    auth_details = response.get("authorisation_details") or response.get("authorization_details") or {}
    auth_status = (auth_details.get("authorization_status") or "").upper()
    payment_id = auth_details.get("payment_id") or auth_details.get("razorpay_payment_id") or ""
    if payment_id:
        subscription.auth_payment_id = str(payment_id)

    if gateway_status:
        subscription.status = razorpay_subscription_status_to_local(gateway_status, subscription)
    if auth_status in {"ACTIVE", "SUCCESS", "AUTHENTICATED"}:
        if not subscription.autopay_confirmed_at:
            subscription.autopay_confirmed_at = timezone.now()
        if subscription.trial_ends_at > timezone.now():
            subscription.current_period_start = subscription.trial_ends_at
            subscription.current_period_end = subscription.trial_ends_at + timedelta(days=30)
        elif not subscription.current_period_end or subscription.current_period_end <= timezone.now():
            subscription.current_period_start = timezone.now()
            subscription.current_period_end = timezone.now() + timedelta(days=30)
        subscription.status = SaloonSubscription.STATUS_TRIALING if subscription.trial_ends_at > timezone.now() else SaloonSubscription.STATUS_ACTIVE

    subscription.save(update_fields=[
        "razorpay_status",
        "razorpay_subscription_id",
        "razorpay_customer_id",
        "auth_payment_id",
        "autopay_confirmed_at",
        "status",
        "current_period_start",
        "current_period_end",
        "updated_at",
    ])
    return subscription


def apply_razorpay_webhook_event(subscription, event_name, details):
    event = (event_name or "").upper()
    now = timezone.now()
    update_fields = ["updated_at", "notes"]
    notes = dict(subscription.notes or {})
    notes["last_razorpay_event"] = event
    notes["last_razorpay_event_at"] = now.isoformat()
    subscription.notes = notes

    if event == "SUBSCRIPTION_AUTH_STATUS":
        apply_razorpay_subscription_response(subscription, details)
        return subscription

    if event == "SUBSCRIPTION_STATUS_CHANGED":
        gateway_status = details.get("subscription_status") or details.get("status")
        subscription.razorpay_status = gateway_status or subscription.razorpay_status
        subscription.status = razorpay_subscription_status_to_local(gateway_status, subscription)
        if subscription.status == SaloonSubscription.STATUS_CANCELLED and not subscription.cancelled_at:
            subscription.cancelled_at = now
            update_fields.append("cancelled_at")
        update_fields.extend(["razorpay_status", "status"])

    elif event == "SUBSCRIPTION_PAYMENT_SUCCESS":
        history = record_subscription_payment(subscription, details, SubscriptionPaymentHistory.STATUS_SUCCESS)
        subscription.last_payment_id = history.razorpay_payment_id
        subscription.last_payment_status = "SUCCESS"
        subscription.autopay_confirmed_at = subscription.autopay_confirmed_at or now
        subscription.status = SaloonSubscription.STATUS_ACTIVE
        subscription.grace_period_started_at = None
        subscription.grace_period_ends_at = None
        start_at = parse_razorpay_datetime(details.get("payment_schedule_date")) or now
        subscription.current_period_start = start_at
        subscription.current_period_end = start_at + timedelta(days=30 if subscription.plan.billing_period == "monthly" else 365)
        update_fields.extend([
            "last_payment_id",
            "last_payment_status",
            "autopay_confirmed_at",
            "status",
            "grace_period_started_at",
            "grace_period_ends_at",
            "current_period_start",
            "current_period_end",
        ])

    elif event == "SUBSCRIPTION_PAYMENT_FAILED":
        history = record_subscription_payment(subscription, details, SubscriptionPaymentHistory.STATUS_FAILED)
        subscription.last_payment_id = history.razorpay_payment_id
        subscription.last_payment_status = "FAILED"
        subscription.status = SaloonSubscription.STATUS_PAST_DUE
        if not subscription.grace_period_started_at:
            subscription.grace_period_started_at = now
            subscription.grace_period_ends_at = now + timedelta(days=GRACE_PERIOD_DAYS)
        notes["last_failure_reason"] = (details.get("failure_details") or {}).get("failure_reason") or details.get("failureReason") or ""
        notes["retry_attempts"] = details.get("retry_attempts")
        subscription.notes = notes
        update_fields.extend([
            "last_payment_id",
            "last_payment_status",
            "status",
            "grace_period_started_at",
            "grace_period_ends_at",
        ])

    elif event in {"SUBSCRIPTION_PAYMENT_CANCELLED", "SUBSCRIPTION_PAYMENT_NOTIFICATION_INITIATED"}:
        status = SubscriptionPaymentHistory.STATUS_CANCELLED if event.endswith("CANCELLED") else SubscriptionPaymentHistory.STATUS_PENDING
        record_subscription_payment(subscription, details, status)
        notes["retry_attempts"] = details.get("retry_attempts")
        subscription.notes = notes

    elif "CANCEL" in event:
        subscription.status = SaloonSubscription.STATUS_CANCELLED
        subscription.cancelled_at = subscription.cancelled_at or now
        update_fields.extend(["status", "cancelled_at"])

    subscription.save(update_fields=list(dict.fromkeys(update_fields)))
    return subscription





# ---------------------------------------------------------------------------
# Razorpay subscription webhooks (subscription.* events)
# ---------------------------------------------------------------------------

def _ts_to_datetime(value):
    if not value:
        return None
    try:
        return timezone.datetime.fromtimestamp(int(value), tz=timezone.get_current_timezone())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _record_razorpay_payment(subscription, payment_entity, status):
    payment_id = payment_entity.get("id") or ""
    if not payment_id:
        return None
    method = payment_entity.get("method") or ""
    if method == "upi" and payment_entity.get("vpa"):
        method_label = f"UPI AutoPay {payment_entity.get('vpa')}"
    elif method == "card":
        last4 = (payment_entity.get("card") or {}).get("last4") or ""
        method_label = f"Card ****{last4}" if last4 else "Card"
    else:
        method_label = method.upper() if method else ""
    amount_paise = payment_entity.get("amount") or 0
    history, created = SubscriptionPaymentHistory.objects.get_or_create(
        subscription=subscription,
        razorpay_payment_id=payment_id,
        defaults={
            "saloon": subscription.saloon,
            "plan": subscription.plan,
            "paid_at": _ts_to_datetime(payment_entity.get("created_at")) or timezone.now(),
            "amount": _decimal_from_payload(Decimal(amount_paise) / 100),
            "currency": payment_entity.get("currency") or subscription.plan.currency,
            "payment_status": status,
            "payment_method": method_label,
            "renewal_type": "renewal",
            "razorpay_subscription_id": subscription.razorpay_subscription_id,
            "transaction_reference": payment_entity.get("order_id") or "",
            "raw_payload": payment_entity,
        },
    )
    if not created and history.payment_status != status:
        history.payment_status = status
        history.raw_payload = payment_entity
        history.save(update_fields=["payment_status", "raw_payload"])
    return history


def _start_grace_period(subscription, now):
    if not subscription.grace_period_started_at:
        subscription.grace_period_started_at = now
        base = max(subscription.current_period_end or now, now)
        subscription.grace_period_ends_at = base + timedelta(days=GRACE_PERIOD_DAYS)


def apply_razorpay_subscription_event(subscription, event_name, payload):
    """
    Update a local subscription from a Razorpay `subscription.*` webhook.

    authenticated        -> autopay mandate set up
    activated / charged  -> paid; extend access to Razorpay's current cycle end
    pending / halted     -> renewal charge failed; grace period while Razorpay retries
    cancelled / paused / completed
                         -> owner stopped autopay (UPI app, bank or our dashboard); keep
                            access until the already-paid period ends, then lock
    resumed              -> owner resumed a paused mandate
    """
    now = timezone.now()
    event = (event_name or "").lower()
    entities = payload.get("payload", {}) or {}
    sub_entity = (entities.get("subscription") or {}).get("entity") or {}
    payment_entity = (entities.get("payment") or {}).get("entity") or {}

    notes = dict(subscription.notes or {})
    notes["last_razorpay_event"] = event
    notes["last_razorpay_event_at"] = now.isoformat()
    subscription.notes = notes

    gateway_status = sub_entity.get("status") or ""
    if gateway_status:
        subscription.razorpay_status = gateway_status

    current_start = _ts_to_datetime(sub_entity.get("current_start"))
    current_end = _ts_to_datetime(sub_entity.get("current_end"))

    if event == "subscription.authenticated":
        subscription.autopay_confirmed_at = subscription.autopay_confirmed_at or now
        subscription.cancelled_at = None
        subscription.cancel_at_cycle_end = False
        if subscription.status in {SaloonSubscription.STATUS_PENDING_AUTH, SaloonSubscription.STATUS_EXPIRED}:
            subscription.status = (
                SaloonSubscription.STATUS_TRIALING
                if subscription.trial_ends_at > now
                else SaloonSubscription.STATUS_ACTIVE
            )

    elif event in {"subscription.activated", "subscription.charged", "subscription.resumed"}:
        subscription.autopay_confirmed_at = subscription.autopay_confirmed_at or now
        subscription.cancelled_at = None
        subscription.cancel_at_cycle_end = False
        subscription.grace_period_started_at = None
        subscription.grace_period_ends_at = None
        if current_start:
            subscription.current_period_start = current_start
        if current_end:
            subscription.current_period_end = current_end
        subscription.status = (
            SaloonSubscription.STATUS_TRIALING
            if subscription.trial_ends_at > now
            else SaloonSubscription.STATUS_ACTIVE
        )
        if event == "subscription.charged" and payment_entity:
            history = _record_razorpay_payment(subscription, payment_entity, SubscriptionPaymentHistory.STATUS_SUCCESS)
            if history:
                subscription.last_payment_id = history.razorpay_payment_id
                subscription.last_payment_status = "SUCCESS"

    elif event in {"subscription.pending", "subscription.halted"}:
        subscription.status = SaloonSubscription.STATUS_PAST_DUE
        _start_grace_period(subscription, now)
        if payment_entity:
            history = _record_razorpay_payment(subscription, payment_entity, SubscriptionPaymentHistory.STATUS_FAILED)
            if history:
                subscription.last_payment_id = history.razorpay_payment_id
                subscription.last_payment_status = "FAILED"
        notes["last_failure_reason"] = payment_entity.get("error_description") or ""

    elif event in {"subscription.cancelled", "subscription.paused", "subscription.completed"}:
        subscription.status = SaloonSubscription.STATUS_CANCELLED
        subscription.cancelled_at = subscription.cancelled_at or now
        subscription.cancel_at_cycle_end = True
        notes["autopay_stopped_reason"] = event.split(".", 1)[1]
        # A failed renewal followed by cancel should not keep the grace period open.
        subscription.grace_period_started_at = None
        subscription.grace_period_ends_at = None

    else:
        # subscription.updated and anything new: keep the cycle dates in sync.
        if current_start:
            subscription.current_period_start = current_start
        if current_end:
            subscription.current_period_end = current_end

    subscription.save()
    return subscription
