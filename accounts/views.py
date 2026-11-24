import requests
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.helpers import complete_social_login, render_authentication_error
from allauth.socialaccount.models import SocialLogin
from allauth.socialaccount.providers.oauth2.client import OAuth2Error
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST


def signup_redirect(request):
    """Google is the only sign-up, so the old sign-up link goes to the login page."""
    url = reverse("account_login")
    if request.META.get("QUERY_STRING"):
        url = f"{url}?{request.META['QUERY_STRING']}"
    return redirect(url)


@require_POST
def google_one_tap(request):
    """
    Finish a Google One Tap sign-in. The page posts the ID token Google gave it
    (protected by Django's normal CSRF token), we check Google's signature and
    that it was issued for our client ID, then log in and return to `next`.
    """
    provider = get_adapter().get_provider(request, "google")
    try:
        login = provider.verify_token(request, {"id_token": request.POST.get("credential", "")})
        login.state = SocialLogin.state_from_request(request)
        return complete_social_login(request, login)
    except ImmediateHttpResponse as exc:
        return exc.response
    except (ValidationError, PermissionDenied, OAuth2Error, requests.RequestException) as exc:
        return render_authentication_error(request, provider, exception=exc)
