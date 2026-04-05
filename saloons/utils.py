import os

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
                "image_url": None,
                "category_id": None,
            }

        if rejection_map:
            slot["rejection"] = rejection_map.get(f"service_image_{i+1}")

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
