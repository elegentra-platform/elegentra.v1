from django.utils import timezone
from datetime import timedelta
from .models import Service

def cleanup_expired_services():
    cutoff = timezone.now() - timedelta(seconds=5)
    Service.objects.filter(
        is_deleted=True,
        deleted_at__lt=cutoff
    ).delete()
