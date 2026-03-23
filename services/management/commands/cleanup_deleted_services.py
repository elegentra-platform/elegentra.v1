from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from services.models import Service

class Command(BaseCommand):
    help = "Permanently delete services after undo window"

    def handle(self, *args, **kwargs):
        cutoff = timezone.now() - timedelta(seconds=5)

        qs = Service.objects.filter(
            is_deleted=True,
            deleted_at__lt=cutoff
        )

        count = qs.count()
        qs.delete()

        self.stdout.write(
            self.style.SUCCESS(f"Permanently deleted {count} services.")
        )
