import json

from django.conf import settings
from django.templatetags.static import static
from django.utils.html import strip_tags
from django.utils.text import Truncator


BRAND_NAME = "Elegentra"
DEFAULT_DESCRIPTION = (
    "Discover salons, spas, and beauty professionals near you on Elegentra. "
    "Explore services, reviews, timings, and contact details in one place."
)


def build_absolute_url(request, path=None):
    if path and str(path).startswith(("http://", "https://")):
        return str(path)

    site_url = getattr(settings, "SITE_URL", "").rstrip("/")
    if site_url:
        if path:
            normalized = str(path)
            if not normalized.startswith("/"):
                normalized = f"/{normalized}"
            return f"{site_url}{normalized}"
        return site_url

    return request.build_absolute_uri(path or request.get_full_path())


def canonicalize_url(url):
    if "?" not in url and "#" not in url:
        return url
    return url.split("?", 1)[0].split("#", 1)[0]


def truncate_text(value, length=160):
    cleaned = " ".join(strip_tags(value or "").split())
    if not cleaned:
        cleaned = DEFAULT_DESCRIPTION
    return Truncator(cleaned).chars(length)


def build_image_url(request, image_url=None):
    chosen = image_url or static("services/images/hero.webp")
    return build_absolute_url(request, chosen)


def build_seo_payload(
    request,
    *,
    title,
    description="",
    canonical_url="",
    image_url="",
    robots="index,follow",
    og_type="website",
    json_ld=None,
):
    canonical = canonicalize_url(canonical_url or build_absolute_url(request))
    final_description = truncate_text(description)
    absolute_image = build_image_url(request, image_url)
    payload = {
        "title": title,
        "description": final_description,
        "canonical_url": canonical,
        "image_url": absolute_image,
        "robots": robots,
        "og_type": og_type,
        "site_name": BRAND_NAME,
    }
    if json_ld:
        payload["json_ld"] = json.dumps(json_ld, ensure_ascii=True)
    return payload
