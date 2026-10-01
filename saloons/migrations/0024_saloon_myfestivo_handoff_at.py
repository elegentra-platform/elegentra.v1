from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("saloons", "0023_gallerypost_service"),
    ]

    operations = [
        migrations.AddField(
            model_name="saloon",
            name="myfestivo_handoff_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
