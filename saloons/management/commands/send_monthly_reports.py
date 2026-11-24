"""
Send the monthly WhatsApp report to salon partners.

Meant to run once a day (Railway cron). It does nothing except on report day
(the 29th, or the last day of shorter months), and never sends a salon the
same month's report twice.

    python manage.py send_monthly_reports                 # normal daily run
    python manage.py send_monthly_reports --dry-run       # show what would be sent
    python manage.py send_monthly_reports --date 2026-12-29 --dry-run
    python manage.py send_monthly_reports --only-saloon 12 --to 919876543210   # preview to a phone
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from saloons import monthly_report as report
from saloons.models import Saloon
from saloons.utils import format_whatsapp_number


class Command(BaseCommand):
    help = "Send the monthly WhatsApp performance report to eligible salons."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Pretend today is this date (YYYY-MM-DD).")
        parser.add_argument("--dry-run", action="store_true", help="Print the reports, send nothing.")
        parser.add_argument("--force", action="store_true", help="Run even if today isn't report day.")
        parser.add_argument("--only-saloon", type=int, help="Only this salon id (skips eligibility checks).")
        parser.add_argument("--to", help="Send to this number instead of the salon (preview; not recorded).")

    def handle(self, *args, **options):
        if options["date"]:
            try:
                today = date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError("--date must be YYYY-MM-DD") from exc
        else:
            today = timezone.localdate()

        preview_to = None
        if options["to"]:
            raw = options["to"].strip().replace(" ", "")
            if not raw.startswith("+"):
                raw = f"+91{raw}" if len(raw) == 10 else f"+{raw}"
            preview_to = format_whatsapp_number(raw)
            if not preview_to:
                raise CommandError("--to is not a valid phone number")
            if not options["only_saloon"]:
                raise CommandError("--to needs --only-saloon so only one preview is sent")

        if options["only_saloon"]:
            try:
                saloons = [Saloon.objects.select_related("owner", "profile").get(pk=options["only_saloon"])]
            except Saloon.DoesNotExist as exc:
                raise CommandError("No salon with that id") from exc
        else:
            if not (report.is_report_day(today) or options["force"]):
                self.stdout.write(f"{today}: not report day, nothing to do.")
                return
            saloons = list(report.eligible_saloons(today))

        period = report.report_period(today)
        self.stdout.write(f"Report for {period.label} ({period.key}), {len(saloons)} salon(s).")
        sent = skipped = failed = 0

        for saloon in saloons:
            if not preview_to and report.already_sent(saloon, today):
                skipped += 1
                continue

            if options["dry_run"]:
                stats = report.collect_stats(saloon, period)
                template = report.choose_template(saloon, stats)
                params = report.report_parameters(saloon, stats, period, template)
                self.stdout.write(f"\n#{saloon.pk} {saloon} -> {template}")
                for i, value in enumerate(params, start=1):
                    self.stdout.write(f"  {{{{{i}}}}} {value}")
                continue

            try:
                template, _, message_id = report.send_report(
                    saloon, today, to_number=preview_to, record=preview_to is None
                )
            except Exception as exc:  # keep going for the other salons
                failed += 1
                self.stderr.write(f"#{saloon.pk} {saloon}: FAILED {exc}")
                continue
            sent += 1
            self.stdout.write(f"#{saloon.pk} {saloon}: sent {template} ({message_id})")

        if not options["dry_run"]:
            self.stdout.write(f"Done. Sent {sent}, already sent {skipped}, failed {failed}.")
