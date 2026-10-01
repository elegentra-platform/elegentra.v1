import os
import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from twilio.rest import Client


def build_service_slots(services, rejection_map=None):
    slots = []

    for i in range(3):
        if i < len(services):
            slot = services[i]
        else:
            slot = {
                "name": "",
                "price": "",
                "category_id": None,
            }

        if rejection_map:
            slot["rejection"] = (
                rejection_map.get(f"service_{i+1}")
                or rejection_map.get(f"service_image_{i+1}")
            )

        slots.append(slot)

    return slots


def format_whatsapp_number(raw_phone: str) -> str:
    """
    Ensures phone number is valid E.164 format.
    Frontend is responsible for country selection.
    Backend is defensive.
    """

    if not raw_phone:
        return ""

    phone = raw_phone.strip().replace(" ", "").replace("-", "")

    if not phone.startswith("+"):
        return ""

    digits = phone[1:]

    if not digits.isdigit():
        return ""

    if len(digits) < 8 or len(digits) > 15:
        return ""

    return phone


def send_otp_whatsapp(phone_number: str, otp: int):
    """
    Sends OTP via Twilio WhatsApp.
    Expects phone_number in E.164 (+XXXXXXXX).
    """

    if settings.DEBUG:
        print(f"[DEV OTP] {phone_number} -> {otp}")

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")

    if not account_sid or not auth_token or not from_number:
        raise ImproperlyConfigured("Twilio WhatsApp credentials are not configured.")

    client = Client(account_sid, auth_token)

    to_number = phone_number
    if not to_number.startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"

    client.messages.create(
        from_=from_number,
        to=to_number,
        body=f"Your Saloon verification code is {otp}",
    )


RESERVED_SALOON_SLUGS = {
    "www",
    "admin",
    "api",
    "app",
    "help",
    "support",
    "blog",
    "mail",
    "static",
    "media",
    "account",
    "accounts",
    "dashboard",
    "partner",
    "search",
    "settings",
}


def normalize_saloon_slug(raw_value: str, max_length: int = 50) -> str:
    value = (raw_value or "").strip().lower()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-z0-9_-]", "", value)
    value = re.sub(r"-{2,}", "-", value)
    value = re.sub(r"_{2,}", "_", value)
    value = value.strip("-_")
    return value[:max_length]


def is_reserved_saloon_slug(slug: str) -> bool:
    return (slug or "").strip().lower() in RESERVED_SALOON_SLUGS


def saloon_domain_suffix() -> str:
    return getattr(settings, "ELEGENTRA_ROOT_DOMAIN", "elegentra.com").strip()


def build_saloon_public_url(slug: str, request=None) -> str:
    clean_slug = normalize_saloon_slug(slug)
    if not clean_slug:
        return ""

    if request is not None:
        request_host = request.get_host() or ""
        host = (request.get_host() or "").split(":")[0].lower()
        scheme = "https" if request.is_secure() else "http"
        port = ""
        try:
            request_port = request.get_port()
            if request_port and request_port not in {"80", "443"}:
                port = f":{request_port}"
        except Exception:
            port = ""

        root = saloon_domain_suffix().lower()
        if host == root or host == f"www.{root}" or host.endswith(f".{root}"):
            return f"https://{clean_slug}.{root}"

        # localhost, 127.0.0.1, and temporary/public tunnel hosts all use
        # path-style URLs. "localhost" has no dot, so it can't carry a
        # cookie-sharing Domain=.localhost attribute (browsers/HTTP clients
        # reject it as an invalid cookie domain per RFC 6265) — a real
        # *.localhost subdomain would silently lose the session cookie.
        # Tunnel hosts need this too since they don't have wildcard DNS.
        return f"{scheme}://{request_host}/saloons/{clean_slug}/"

    return f"https://{clean_slug}.{saloon_domain_suffix()}"


def extract_saloon_slug_from_host(request) -> str:
    host = (request.get_host() or "").split(":")[0].lower()
    if host in {"localhost", "127.0.0.1"}:
        return ""

    if host.endswith(".localhost"):
        candidate = host[: -len(".localhost")]
        candidate = normalize_saloon_slug(candidate)
        if candidate and not is_reserved_saloon_slug(candidate):
            return candidate
        return ""

    root = saloon_domain_suffix().lower()
    if host == root or host == f"www.{root}" or not host.endswith(f".{root}"):
        return ""

    candidate = host[: -(len(root) + 1)]
    candidate = normalize_saloon_slug(candidate)
    if candidate and not is_reserved_saloon_slug(candidate):
        return candidate
    return ""


def build_public_root_url(request) -> str:
    scheme = "https" if request.is_secure() else "http"
    request_host = request.get_host() or ""
    host = (request.get_host() or "").split(":")[0].lower()
    port = ""
    try:
        request_port = request.get_port()
        if request_port and request_port not in {"80", "443"}:
            port = f":{request_port}"
    except Exception:
        port = ""

    if host.endswith(".localhost") or host in {"localhost", "127.0.0.1"}:
        return f"{scheme}://localhost{port}"

    root = saloon_domain_suffix().lower()
    if host == root or host == f"www.{root}" or host.endswith(f".{root}"):
        return f"{scheme}://{root}"

    # Temporary/public tunnel hosts should keep navigation on the current host.
    return f"{scheme}://{request_host}"


def sync_saloon_slug_from_name(saloon, force=False):
    if not saloon:
        return saloon

    profile = getattr(saloon, "profile", None)
    current_slug = normalize_saloon_slug(saloon.slug)
    owner_slug = normalize_saloon_slug(getattr(saloon.owner, "username", ""))
    preferred_name = (saloon.name or "").strip() or ((profile.saloon_name or "").strip() if profile else "")
    preferred_slug = normalize_saloon_slug(preferred_name)

    if not preferred_slug:
        return saloon
    if not force and current_slug and current_slug != owner_slug:
        return saloon
    if is_reserved_saloon_slug(preferred_slug):
        return saloon

    from saloons.models import Saloon

    candidate = preferred_slug
    count = 1
    while Saloon.objects.filter(slug=candidate).exclude(id=saloon.id).exists():
        candidate = f"{preferred_slug}-{count}"
        count += 1

    if candidate and candidate != saloon.slug:
        saloon.slug = candidate
        saloon.save(update_fields=["slug", "updated_at"])
    return saloon
