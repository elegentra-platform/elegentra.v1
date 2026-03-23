from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0007_maincategory_servicecategory_main_category"),
    ]

    operations = [
        migrations.AddField(
            model_name="maincategory",
            name="icon_image",
            field=models.ImageField(
                blank=True,
                help_text="Category icon image shown in buyer app circles.",
                null=True,
                upload_to="categories/icons/",
            ),
        ),
        migrations.RemoveField(
            model_name="maincategory",
            name="icon_class",
        ),
    ]
