"""
Monthly WhatsApp performance summary for salon partners.

On report day (the 29th; the last day in shorter months) each eligible salon
gets one of three Utility templates (see whatsapp_report_templates.py):

  * first month  - the salon's first report, no comparison
  * up           - visits went up, stayed the same, or dropped by 15% or less
  * down         - visits dropped by more than 15% and last month had 20+ visits

Both months are compared over the same days (1st up to the day before the
report), so a 31-day month isn't compared with a 30-day one.
"""
import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.db.models import Avg, Count
from django.utils import timezone

from plans.billing import get_or_create_saloon_subscription, subscription_has_platform_access
from public.models import FavoriteSaloon
from services.models import ServiceInterest

from .models import (
    Saloon,
    SaloonContactClick,
    SaloonMapClick,
    SaloonMonthlyReport,
    SaloonProfileView,
    SaloonReview,
)
from .utils import format_whatsapp_number, send_whatsapp_template
from .whatsapp_report_templates import LANGUAGE, TEMPLATE_DOWN, TEMPLATE_FIRST_MONTH, TEMPLATE_UP

logger = logging.getLogger(__name__)

REPORT_DAY = 29
QUIETER_DROP = 0.15          # more than a 15% drop in visits → quieter-month template
QUIETER_MIN_PREVIOUS = 20    # ...but only if last month had at least this many visits
MIN_DAYS_LISTED = 7          # salons approved more recently than this wait for next month


# ---------------------------------------------------------------- dates

def report_day_for(year: int, month: int) -> int:
    return min(REPORT_DAY, calendar.monthrange(year, month)[1])


def is_report_day(today: date) -> bool:
    return today.day == report_day_for(today.year, today.month)


@dataclass
class ReportPeriod:
    label: str            # "December"
    key: str              # "2026-12"
    start: datetime
    end: datetime         # exclusive
    previous_label: str
    previous_start: datetime
    previous_end: datetime


def report_period(report_date: date) -> ReportPeriod:
    """Days 1..(report day - 1) of this month, and the same days of last month."""
    tz = timezone.get_current_timezone()
    days = max(report_date.day - 1, 1)

    start_day = report_date.replace(day=1)
    prev_month_last = start_day - timedelta(days=1)
    prev_start_day = prev_month_last.replace(day=1)
    prev_days = min(days, calendar.monthrange(prev_start_day.year, prev_start_day.month)[1])

    def at_midnight(day):
        return timezone.make_aware(datetime.combine(day, time.min), tz)

    return ReportPeriod(
        label=start_day.strftime("%B"),
        key=start_day.strftime("%Y-%m"),
        start=at_midnight(start_day),
        end=at_midnight(start_day + timedelta(days=days)),
        previous_label=prev_start_day.strftime("%B"),
        previous_start=at_midnight(prev_start_day),
        previous_end=at_midnight(prev_start_day + timedelta(days=prev_days)),
    )


# ---------------------------------------------------------------- numbers

@dataclass
class SalonStats:
    visits: int = 0
    location_checks: int = 0
    whatsapp: int = 0
    calls: int = 0
    saves: int = 0
    top_service: str = ""
    top_service_interest: int = 0
    new_reviews: int = 0
    average_rating: float = 0.0
    previous_visits: int = 0
    extra: dict = field(default_factory=dict)


def collect_stats(saloon, period: ReportPeriod) -> SalonStats:
    window = {"created_at__gte": period.start, "created_at__lt": period.end}
    stats = SalonStats()
    stats.visits = SaloonProfileView.objects.filter(saloon=saloon, **window).count()
    stats.previous_visits = SaloonProfileView.objects.filter(
        saloon=saloon, created_at__gte=period.previous_start, created_at__lt=period.previous_end
    ).count()
    stats.location_checks = SaloonMapClick.objects.filter(saloon=saloon, **window).count()
    clicks = SaloonContactClick.objects.filter(saloon=saloon, **window)
    stats.whatsapp = clicks.filter(kind=SaloonContactClick.WHATSAPP).count()
    stats.calls = clicks.filter(kind=SaloonContactClick.CALL).count()
    stats.saves = FavoriteSaloon.objects.filter(saloon=saloon, **window).count()

    top = (
        ServiceInterest.objects.filter(
            service__saloon=saloon, service__is_active=True, service__is_deleted=False, **window
        )
        .values("service__name")
        .annotate(total=Count("id"))
        .order_by("-total", "service__name")
        .first()
    )
    if top:
        stats.top_service = top["service__name"]
        stats.top_service_interest = top["total"]

    reviews = SaloonReview.objects.filter(saloon=saloon, is_visible=True, **window).aggregate(
        total=Count("id"), average=Avg("rating")
    )
    stats.new_reviews = reviews["total"] or 0
    stats.average_rating = float(reviews["average"] or 0)
    return stats


# ---------------------------------------------------------------- wording

def _count(n, one, many, none):
    if n <= 0:
        return none
    return f"1 {one}" if n == 1 else f"{n} {many}"


def visitors_text(n):
    return _count(n, "person", "people", "No one")


def enquiries_text(n):
    return _count(n, "WhatsApp enquiry", "WhatsApp enquiries", "No WhatsApp enquiries")


def calls_text(n):
    return _count(n, "call", "calls", "No calls")


def customers_text(n):
    return _count(n, "customer", "customers", "No customers")


def top_service_text(stats: SalonStats):
    if not stats.top_service or stats.top_service_interest <= 0:
        return "No service interest yet"
    who = "1 person" if stats.top_service_interest == 1 else f"{stats.top_service_interest} people"
    return f"{stats.top_service} · {who} interested"


def reviews_text(stats: SalonStats):
    if stats.new_reviews <= 0:
        return "No new reviews this month"
    rating = f"{stats.average_rating:.1f}".rstrip("0").rstrip(".")
    if stats.new_reviews == 1:
        return f"1 new review · {rating}★"
    return f"{stats.new_reviews} new reviews · {rating}★ average"


def comparison_text(previous: int, current: int):
    diff = current - previous
    if diff == 0:
        return f"no change ({previous} → {current})"
    word = "visit" if abs(diff) == 1 else "visits"
    sign = "+" if diff > 0 else "−"
    return f"{sign}{abs(diff)} {word} ({previous} → {current})"


# ---------------------------------------------------------------- choosing

def choose_template(saloon, stats: SalonStats) -> str:
    if not SaloonMonthlyReport.objects.filter(saloon=saloon, status=SaloonMonthlyReport.STATUS_SENT).exists():
        return TEMPLATE_FIRST_MONTH
    previous, current = stats.previous_visits, stats.visits
    if previous >= QUIETER_MIN_PREVIOUS and current < previous * (1 - QUIETER_DROP):
        return TEMPLATE_DOWN
    return TEMPLATE_UP


def _owner_first_name(saloon):
    profile = getattr(saloon, "profile", None)
    full = (getattr(profile, "owner_full_name", "") or saloon.owner.get_full_name() or "").strip()
    return full.split()[0] if full else (_salon_name(saloon))


def _salon_name(saloon):
    profile = getattr(saloon, "profile", None)
    return (getattr(profile, "saloon_name", "") or saloon.name or "your salon").strip()


def report_parameters(saloon, stats: SalonStats, period: ReportPeriod, template: str):
    """Body blanks {{1}}..{{12}} in the order the templates expect."""
    params = [
        _owner_first_name(saloon),
        _salon_name(saloon),
        period.label,
        visitors_text(stats.visits),
        visitors_text(stats.location_checks),
        enquiries_text(stats.whatsapp),
        calls_text(stats.calls),
        customers_text(stats.saves),
        top_service_text(stats),
        reviews_text(stats),
    ]
    if template != TEMPLATE_FIRST_MONTH:
        params += [period.previous_label, comparison_text(stats.previous_visits, stats.visits)]
    return params


def build_components(saloon, params):
    return [
        {"type": "body", "parameters": [{"type": "text", "text": value} for value in params]},
        {
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": saloon.owner.username}],
        },
    ]


# ---------------------------------------------------------------- who and send

def eligible_saloons(report_date: date):
    cutoff = timezone.make_aware(
        datetime.combine(report_date - timedelta(days=MIN_DAYS_LISTED), time.min),
        timezone.get_current_timezone(),
    )
    candidates = (
        Saloon.objects.filter(
            is_active=True,
            approval_status=Saloon.APPROVAL_APPROVED,
            is_whatsapp_verified=True,
            is_test_saloon=False,
        )
        .select_related("owner", "profile")
        .order_by("id")
    )
    for saloon in candidates:
        profile = getattr(saloon, "profile", None)
        if profile is not None and not profile.monthly_report_enabled:
            continue
        subscription, _ = get_or_create_saloon_subscription(saloon)
        if not subscription_has_platform_access(subscription):
            continue
        approved_at = subscription.trial_assigned_at
        if approved_at and approved_at > cutoff:
            continue
        if not format_whatsapp_number(saloon.whatsapp_number):
            continue
        yield saloon


def send_report(saloon, report_date: date, *, to_number=None, record=True):
    """
    Build and send one salon's report. `to_number` sends a preview to another
    number; previews (record=False) don't count as the salon's report.
    Returns (template, params, message_id).
    """
    period = report_period(report_date)
    stats = collect_stats(saloon, period)
    template = choose_template(saloon, stats)
    params = report_parameters(saloon, stats, period, template)
    destination = to_number or format_whatsapp_number(saloon.whatsapp_number)

    try:
        message_id = send_whatsapp_template(destination, template, LANGUAGE, build_components(saloon, params))
    except Exception as exc:
        if record:
            SaloonMonthlyReport.objects.update_or_create(
                saloon=saloon,
                period=period.key,
                defaults={"template_name": template, "status": SaloonMonthlyReport.STATUS_FAILED,
                          "error": str(exc)[:2000], "stats": {"params": params}},
            )
        raise

    if record:
        SaloonMonthlyReport.objects.update_or_create(
            saloon=saloon,
            period=period.key,
            defaults={"template_name": template, "status": SaloonMonthlyReport.STATUS_SENT,
                      "message_id": message_id, "error": "", "stats": {"params": params}},
        )
    return template, params, message_id


def already_sent(saloon, report_date: date) -> bool:
    return SaloonMonthlyReport.objects.filter(
        saloon=saloon, period=report_period(report_date).key, status=SaloonMonthlyReport.STATUS_SENT
    ).exists()
