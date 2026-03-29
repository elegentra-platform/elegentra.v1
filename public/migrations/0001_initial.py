from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("saloons", "0020_saloonmapclick_saloonprofileview"),
    ]

    operations = [
        migrations.CreateModel(
            name="FavoriteSaloon",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("saloon", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="favorited_by_users", to="saloons.saloon")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="favorite_saloons", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(fields=["user", "created_at"], name="public_favo_user_id_2655f9_idx"),
                    models.Index(fields=["saloon", "created_at"], name="public_favo_saloon__4a0f83_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("user", "saloon"), name="unique_user_saloon_favorite"),
                ],
            },
        ),
    ]
