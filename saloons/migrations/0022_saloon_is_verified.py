from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("saloons", "0021_state_district_saloonprofile_location_master"),
    ]

    operations = [
        migrations.AddField(
            model_name="saloon",
            name="is_verified",
            field=models.BooleanField(default=False),
        ),
    ]
