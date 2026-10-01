import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0010_alter_service_image"),
        ("saloons", "0022_saloon_is_verified"),
    ]

    operations = [
        migrations.AddField(
            model_name="gallerypost",
            name="service",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="gallery_posts",
                to="services.service",
            ),
        ),
    ]
