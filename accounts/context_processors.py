from django.contrib.sites.shortcuts import get_current_site

from allauth.socialaccount.models import SocialApp


def google_oauth_enabled(request):
    site = get_current_site(request)
    enabled = SocialApp.objects.filter(provider="google", sites=site).exists()
    return {"google_oauth_enabled": enabled}
