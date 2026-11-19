"""
Firebase Phone Authentication (SMS codes sent by Google).

The browser runs Firebase's phone sign-in: Google sends the SMS, the owner types
the code, and Firebase gives the page a signed ID token. The page posts that token
here and we verify it with Google's public keys before trusting the phone number.
"""
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)

# The token must come from a sign-in that just happened (stops replaying old tokens).
MAX_SIGN_IN_AGE_SECONDS = 10 * 60


class FirebaseVerificationError(Exception):
    """The ID token is missing, invalid, expired, or not a phone sign-in for this project."""


def firebase_phone_auth_enabled() -> bool:
    config = getattr(settings, "FIREBASE_WEB_CONFIG", {}) or {}
    return bool(config.get("apiKey") and config.get("projectId"))


def verify_phone_id_token(token: str) -> str:
    """Return the verified E.164 phone number from a Firebase ID token, or raise."""
    if not token:
        raise FirebaseVerificationError("Missing verification token.")

    project_id = (getattr(settings, "FIREBASE_WEB_CONFIG", {}) or {}).get("projectId")
    if not project_id:
        raise FirebaseVerificationError("Firebase is not configured.")

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_firebase_token(token, google_requests.Request(), audience=project_id)
    except Exception as exc:  # signature, expiry, audience, network
        logger.warning("Firebase ID token rejected: %s", exc)
        raise FirebaseVerificationError("We couldn't confirm the code. Please try again.") from exc

    if not claims:
        raise FirebaseVerificationError("We couldn't confirm the code. Please try again.")
    if claims.get("iss") != f"https://securetoken.google.com/{project_id}":
        raise FirebaseVerificationError("This verification came from the wrong project.")
    if (claims.get("firebase") or {}).get("sign_in_provider") != "phone":
        raise FirebaseVerificationError("This verification wasn't a phone code.")

    auth_time = claims.get("auth_time") or 0
    if time.time() - float(auth_time) > MAX_SIGN_IN_AGE_SECONDS:
        raise FirebaseVerificationError("This code has expired. Please request a new one.")

    phone = claims.get("phone_number") or ""
    if not phone.startswith("+"):
        raise FirebaseVerificationError("No phone number was verified.")
    return phone
