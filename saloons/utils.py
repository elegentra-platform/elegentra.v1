import json
import logging
import os
import re
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


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


def generate_otp() -> int:
    """6-digit one-time code from a cryptographically secure source."""
    return secrets.randbelow(900000) + 100000


def _whatsapp_config():
    return {
        "token": os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip(),
        "phone_number_id": os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
        "otp_template": os.getenv("WHATSAPP_OTP_TEMPLATE", "elegentra_otp").strip(),
        "otp_language": os.getenv("WHATSAPP_OTP_LANGUAGE", "en_US").strip(),
        "graph_version": os.getenv("WHATSAPP_GRAPH_VERSION", "v26.0").strip(),
    }


def send_whatsapp_template(phone_number: str, template_name: str, language: str, components: list):
    """
    Send an approved template message through Meta's WhatsApp Cloud API.
    Expects phone_number in E.164 (+XXXXXXXX). Returns Meta's message id.
    """
    config = _whatsapp_config()
    if not config["token"] or not config["phone_number_id"]:
        raise ImproperlyConfigured("WhatsApp Cloud API is not configured (WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID).")

    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number.lstrip("+"),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    }
    url = f"https://graph.facebook.com/{config['graph_version']}/{config['phone_number_id']}/messages"
    request = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Authorization", f"Bearer {config['token']}")
    request.add_header("Content-Type", "application/json")

    try:
        with urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        try:
            error = json.loads(details).get("error", {})
            reason = f"{error.get('code')}: {error.get('message')}"
        except (json.JSONDecodeError, AttributeError):
            reason = details[:300]
        logger.warning("WhatsApp template %s send failed (%s): %s", template_name, exc.code, reason)
        raise RuntimeError(f"WhatsApp send failed: {reason}") from exc
    except URLError as exc:
        logger.warning("WhatsApp API unreachable: %s", exc)
        raise RuntimeError("WhatsApp API is unreachable right now.") from exc

    messages = body.get("messages") or [{}]
    return messages[0].get("id", "")


def send_otp_whatsapp(phone_number: str, otp: int):
    """
    Send a login/verification code on WhatsApp using the approved
    authentication template (code in the body + "Copy code" button).
    Expects phone_number in E.164 (+XXXXXXXX).
    """
    config = _whatsapp_config()

    if settings.DEBUG:
        print(f"[DEV OTP] {phone_number} -> {otp}")
        if not config["token"]:
            # Local development without Meta credentials: the code above is enough.
            return ""

    code = str(otp)
    components = [
        {"type": "body", "parameters": [{"type": "text", "text": code}]},
        # Copy-code buttons take the same code as a URL-button parameter at index 0.
        {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": code}]},
    ]
    return send_whatsapp_template(phone_number, config["otp_template"], config["otp_language"], components)


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    name, domain = email.split("@", 1)
    masked = name[0] + "*" if len(name) <= 2 else name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}"


def send_otp_email(email: str, otp: int):
    """Email fallback for salon verification codes."""
    from django.core.mail import send_mail

    if not email:
        raise RuntimeError("This account has no email address for the verification code.")
    if settings.DEBUG:
        print(f"[DEV OTP] email {email} -> {otp}")
    send_mail(
        subject="Your Elegentra verification code",
        message=(
            f"Your Elegentra verification code is {otp}.\n\n"
            "It was requested to verify your salon's WhatsApp number on Elegentra. "
            "Do not share this code with anyone.\n\n"
            "If you didn't request it, you can ignore this email."
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        recipient_list=[email],
        fail_silently=False,
    )


def send_otp_sms(phone_number: str, otp: int) -> str:
    """
    Send a verification code by SMS using Fast2SMS's OTP route (no DLT template needed;
    the message reads like "Your OTP: 482913"). Indian (+91) numbers only.
    """
    api_key = os.getenv("FAST2SMS_API_KEY", "").strip()
    if not api_key:
        raise ImproperlyConfigured("FAST2SMS_API_KEY is not configured.")

    digits = (phone_number or "").lstrip("+")
    if not (digits.startswith("91") and len(digits) == 12):
        raise RuntimeError("SMS OTP is only available for Indian (+91) numbers.")

    payload = {"route": "otp", "variables_values": str(otp), "numbers": digits[2:]}
    request = Request(
        "https://www.fast2sms.com/dev/bulkV2",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("authorization", api_key)
    request.add_header("Content-Type", "application/json")

    try:
        with urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")[:300]
        logger.warning("Fast2SMS OTP failed (%s): %s", exc.code, details)
        raise RuntimeError(f"SMS send failed: {details}") from exc
    except URLError as exc:
        logger.warning("Fast2SMS unreachable: %s", exc)
        raise RuntimeError("SMS service is unreachable right now.") from exc

    if body.get("return") is not True:
        logger.warning("Fast2SMS OTP rejected: %s", body)
        raise RuntimeError(f"SMS send failed: {body.get('message')}")
    return str(body.get("request_id", ""))


SALON_OTP_CHANNELS = {"auto", "whatsapp", "sms", "email"}


def salon_otp_channel_setting() -> str:
    """
    Server-sent codes (the fallback when the page's Firebase SMS isn't used).

    SALON_OTP_CHANNEL:
      auto     - WhatsApp, then email: the first one that works (default)
      whatsapp - WhatsApp only
      sms      - Fast2SMS only (its cheap OTP route was discontinued; kept for DLT later)
      email    - email only
    """
    value = os.getenv("SALON_OTP_CHANNEL", "auto").strip().lower()
    return value if value in SALON_OTP_CHANNELS else "auto"


def send_salon_verification_code(phone_number: str, otp: int, email: str) -> str:
    """Deliver a salon verification code; returns the channel used ("whatsapp", "sms" or "email")."""
    mode = salon_otp_channel_setting()
    senders = {
        "whatsapp": lambda: send_otp_whatsapp(phone_number, otp),
        "sms": lambda: send_otp_sms(phone_number, otp),
        "email": lambda: send_otp_email(email, otp),
    }
    order = ["whatsapp", "email"] if mode == "auto" else [mode]

    for position, channel in enumerate(order):
        try:
            senders[channel]()
            return channel
        except Exception as exc:
            if position == len(order) - 1:
                # Only this is a real problem: the salon owner received no code at all.
                logger.error("Salon verification code could not be delivered (tried %s): %s", ", ".join(order), exc)
                raise
            logger.warning("%s OTP unavailable, trying the next channel: %s", channel, exc)
    return order[-1]  # unreachable


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
