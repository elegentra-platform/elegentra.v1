from django.conf import settings
from django.contrib.sites.shortcuts import get_current_site

from allauth.socialaccount.models import SocialApp
from saloons.utils import build_public_root_url


def google_oauth_enabled(request):
    site = get_current_site(request)
    settings_apps = (
        settings.SOCIALACCOUNT_PROVIDERS
        .get("google", {})
        .get("APPS", [])
    )
    google_client_id = next((app.get("client_id") for app in settings_apps if app.get("client_id")), "")
    if not google_client_id:
        db_app = SocialApp.objects.filter(provider="google", sites=site).only("client_id").first()
        google_client_id = db_app.client_id if db_app else ""
    enabled = bool(google_client_id)
    root_url = build_public_root_url(request)
    return {
        "google_oauth_enabled": enabled,
        "google_client_id": google_client_id,
        "public_root_url": root_url,
        "public_home_url": f"{root_url}/",
        "public_search_url": f"{root_url}/search/",
        "public_favorites_url": f"{root_url}/favorites/",
        "public_account_url": f"{root_url}/account/",
        "public_settings_url": f"{root_url}/settings/",
        "public_faq_url": f"{root_url}/faq/",
    }

def user_notifications(request):
    if not request.user.is_authenticated:
        return {
            "recent_notifications": [],
            "unread_notifications_count": 0,
        }

    from public.models import UserNotification

    queryset = UserNotification.objects.filter(user=request.user)
    return {
        "recent_notifications": list(queryset[:5]),
        "unread_notifications_count": queryset.filter(is_read=False).count(),
    }
