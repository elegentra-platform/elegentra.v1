from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0006_alter_servicecategory_is_approved"),
    ]

    operations = [
        migrations.CreateModel(
            name="MainCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("slug", models.SlugField(blank=True, unique=True)),
                ("icon_class", models.CharField(default="fa-solid fa-tag", max_length=120)),
                ("display_order", models.PositiveSmallIntegerField(default=1)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["display_order", "name"],
            },
        ),
        migrations.AddField(
            model_name="servicecategory",
            name="main_category",
            field=models.ForeignKey(
                blank=True,
                help_text="Mapped buyer-facing category",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="service_categories",
                to="services.maincategory",
            ),
        ),
    ]
