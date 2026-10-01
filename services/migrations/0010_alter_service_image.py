from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0009_serviceinterest"),
    ]

    operations = [
        migrations.AlterField(
            model_name="service",
            name="image",
            field=models.ImageField(blank=True, null=True, upload_to="services/images/"),
        ),
    ]
