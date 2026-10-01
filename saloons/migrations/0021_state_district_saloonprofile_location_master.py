from django.db import migrations, models
import django.db.models.deletion
from django.utils.text import slugify


KERALA_DISTRICTS = [
    "Thiruvananthapuram",
    "Kollam",
    "Pathanamthitta",
    "Alappuzha",
    "Kottayam",
    "Idukki",
    "Ernakulam",
    "Thrissur",
    "Palakkad",
    "Malappuram",
    "Kozhikode",
    "Wayanad",
    "Kannur",
    "Kasaragod",
]


def seed_kerala_location_master(apps, schema_editor):
    State = apps.get_model("saloons", "State")
    District = apps.get_model("saloons", "District")

    kerala, _ = State.objects.get_or_create(
        slug="kerala",
        defaults={"name": "Kerala", "is_active": True},
    )

    for district_name in KERALA_DISTRICTS:
        District.objects.get_or_create(
            state=kerala,
            slug=slugify(district_name),
            defaults={"name": district_name, "is_active": True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("saloons", "0020_saloonmapclick_saloonprofileview"),
    ]

    operations = [
        migrations.CreateModel(
            name="State",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, unique=True)),
                ("slug", models.SlugField(blank=True, unique=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="District",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("slug", models.SlugField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("state", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="districts", to="saloons.state")),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="saloonprofile",
            name="district",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="saloon_profiles", to="saloons.district"),
        ),
        migrations.AddField(
            model_name="saloonprofile",
            name="state",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="saloon_profiles", to="saloons.state"),
        ),
        migrations.AlterUniqueTogether(
            name="district",
            unique_together={("state", "name"), ("state", "slug")},
        ),
        migrations.RunPython(seed_kerala_location_master, migrations.RunPython.noop),
    ]
