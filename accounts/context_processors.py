from django.conf import settings
from django.contrib.sites.shortcuts import get_current_site

from allauth.socialaccount.models import SocialApp


def google_oauth_enabled(request):
    site = get_current_site(request)
    settings_apps = (
        settings.SOCIALACCOUNT_PROVIDERS
        .get("google", {})
        .get("APPS", [])
    )
    enabled = bool(settings_apps) or SocialApp.objects.filter(provider="google", sites=site).exists()
    return {"google_oauth_enabled": enabled}
