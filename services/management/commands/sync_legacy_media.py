from pathlib import Path
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Copy legacy uploaded files from app folders into MEDIA_ROOT."

    LEGACY_DIRS = [
        "categories/icons",
        "services/images",
        "saloons/banner",
        "saloons/gallery",
        "saloons/services",
        "saloons/verification",
    ]

    def add_arguments(self, parser):
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Replace files in MEDIA_ROOT when they already exist.",
        )

    def handle(self, *args, **options):
        media_root = Path(settings.MEDIA_ROOT)
        base_dir = Path(settings.BASE_DIR)
        overwrite = options["overwrite"]
        copied = 0
        skipped = 0

        media_root.mkdir(parents=True, exist_ok=True)

        for relative_dir in self.LEGACY_DIRS:
            source_dir = base_dir / relative_dir
            if not source_dir.exists():
                continue

            for source_file in source_dir.rglob("*"):
                if not source_file.is_file():
                    continue

                relative_path = source_file.relative_to(base_dir)
                destination = media_root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)

                if destination.exists() and not overwrite:
                    skipped += 1
                    continue

                shutil.copy2(source_file, destination)
                copied += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Legacy media sync complete. Copied {copied} file(s), skipped {skipped} existing file(s)."
            )
        )
