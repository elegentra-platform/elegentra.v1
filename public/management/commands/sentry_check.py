from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Send a test event to Sentry to confirm error tracking is connected."

    def handle(self, *args, **options):
        if not getattr(settings, "SENTRY_DSN", ""):
            raise CommandError(
                "Sentry is off. Set SENTRY_DSN (in .env or Railway variables); "
                "on your own computer (DEBUG) also set SENTRY_IN_DEBUG=1."
            )

        import sentry_sdk

        event_id = sentry_sdk.capture_message("Elegentra Sentry check: error tracking is connected.", level="info")
        sentry_sdk.flush(timeout=10)
        self.stdout.write(self.style.SUCCESS(f"Test event sent (id {event_id}). Check Sentry → Issues."))
