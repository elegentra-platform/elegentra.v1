import secrets

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.account.utils import user_email
from django.conf import settings
from django.contrib.auth import get_user_model

User = get_user_model()


class CustomAccountAdapter(DefaultAccountAdapter):
    def generate_email_verification_code(self) -> str:
        """6-digit numeric code, like the salon OTP (allauth's default mixes letters)."""
        return f"{secrets.randbelow(10**6):06d}"


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def get_app(self, request, provider, client_id=None):
        provider_id = getattr(provider, "id", provider)

        if provider_id == "google":
            configured_apps = settings.SOCIALACCOUNT_PROVIDERS.get("google", {}).get("APPS", [])
            configured_client_ids = {
                app.get("client_id")
                for app in configured_apps
                if app.get("client_id")
            }

            if configured_client_ids:
                apps = self.list_apps(request, provider=provider, client_id=client_id)
                env_apps = [app for app in apps if getattr(app, "client_id", "") in configured_client_ids]
                if env_apps:
                    return env_apps[0]

        return super().get_app(request, provider, client_id=client_id)

    def pre_social_login(self, request, sociallogin):
        email = user_email(sociallogin.user)

        if not email:
            return

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return

        if not sociallogin.is_existing:
            sociallogin.connect(request, user)

        # Google already verified this address: record it as verified so an old,
        # never-confirmed email/password account doesn't trigger a confirmation email.
        google_verified = any(
            address.verified and address.email.lower() == email.lower()
            for address in sociallogin.email_addresses
        )
        if google_verified:
            mark_email_verified(user, email)


def mark_email_verified(user, email):
    from allauth.account.models import EmailAddress

    existing = EmailAddress.objects.filter(user=user, email__iexact=email)
    if existing.exists():
        existing.update(verified=True)
    else:
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
