from django.conf import settings
from django.db import models

from saloons.models import Saloon


class FavoriteSaloon(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="favorite_saloons",
    )
    saloon = models.ForeignKey(
        Saloon,
        on_delete=models.CASCADE,
        related_name="favorited_by_users",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "saloon"], name="unique_user_saloon_favorite"),
        ]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["saloon", "created_at"]),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.saloon_id}"
