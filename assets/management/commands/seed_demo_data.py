"""python manage.py seed_demo_data

Populates a demo dataset. Safe to re-run: assets and employees are upserted by
their natural keys, and the demo check-outs are rebuilt relative to "now" each
run (only rows belonging to the DEMO- assets are touched), so the overdue /
on-time / late spread is always correct no matter when you run it.

Also creates an API user and prints its token.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from rest_framework.authtoken.models import Token

from assets.models import Asset, CheckOut, Employee, OverdueNotice

ASSETS = [
    # tag, name, category
    ("DEMO-CAM-01", "Sony A7 IV camera", Asset.Category.CAMERA),
    ("DEMO-CAM-02", "GoPro Hero 12", Asset.Category.CAMERA),
    ("DEMO-LAP-01", "ThinkPad T14", Asset.Category.LAPTOP),
    ("DEMO-LAP-02", "MacBook Air M3", Asset.Category.LAPTOP),
    ("DEMO-LAP-03", "Dell Latitude 5440", Asset.Category.LAPTOP),
    ("DEMO-SEN-01", "Air-quality sensor kit", Asset.Category.SENSOR),
    ("DEMO-SEN-02", "Vibration sensor", Asset.Category.SENSOR),
    ("DEMO-SEN-03", "Thermal probe", Asset.Category.SENSOR),
    ("DEMO-VEH-01", "Mahindra Bolero (field van)", Asset.Category.VEHICLE),
    ("DEMO-VEH-02", "Hero Splendor (bike)", Asset.Category.VEHICLE),
]

EMPLOYEES = [
    # code, name, email, active
    ("EMP001", "Asha Rao", "asha.rao@example.com", True),
    ("EMP002", "Ravi Kumar", "ravi.kumar@example.com", True),
    ("EMP003", "Meera Iyer", "meera.iyer@example.com", True),
    ("EMP004", "Karan Singh", "karan.singh@example.com", True),
    ("EMP005", "Old Timer", "old.timer@example.com", False),   # inactive
]

# asset, employee, checked out N days ago, due N days from now (negative = past),
# returned N days ago (None = still held), needs maintenance on return
CHECKOUTS = [
    ("DEMO-CAM-01", "EMP001", 12, -5, None, False),   # OVERDUE by 5 days
    ("DEMO-VEH-01", "EMP002", 8, -2, None, False),    # OVERDUE by 2 days
    ("DEMO-LAP-01", "EMP001", 2, 5, None, False),     # held, not overdue
    ("DEMO-SEN-01", "EMP003", 1, 10, None, False),    # held, not overdue
    ("DEMO-LAP-02", "EMP002", 20, -12, 14, False),    # returned ON TIME (2 days early)
    ("DEMO-SEN-02", "EMP004", 15, -9, 10, False),     # returned ON TIME (1 day early)
    ("DEMO-CAM-02", "EMP003", 25, -18, 13, True),     # returned LATE (5 days late) -> maintenance
    ("DEMO-LAP-03", "EMP004", 40, -30, 29, False),    # returned LATE (1 day late)
]

DEMO_USERNAME = "reviewer"
DEMO_PASSWORD = "reviewer-demo-pass"  # demo only; printed below, never use outside local/docker


class Command(BaseCommand):
    help = "Populate the database with demo assets, employees and check-outs (safe to re-run)."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()

        assets = {}
        for i, (tag, name, category) in enumerate(ASSETS):
            assets[tag], _ = Asset.objects.update_or_create(
                asset_tag=tag,
                defaults={
                    "name": name,
                    "category": category,
                    "purchase_date": date(2024, 1, 1) + timedelta(days=30 * i),
                },
            )

        employees = {}
        for code, name, email, active in EMPLOYEES:
            employees[code], _ = Employee.objects.update_or_create(
                employee_code=code, defaults={"full_name": name, "email": email, "is_active": active},
            )

        # Rebuild demo check-outs from scratch so re-running never duplicates them.
        demo_checkouts = CheckOut.objects.filter(asset__asset_tag__startswith="DEMO-")
        OverdueNotice.objects.filter(checkout__in=demo_checkouts).delete()
        demo_checkouts.delete()
        Asset.objects.filter(asset_tag__startswith="DEMO-").update(status=Asset.Status.AVAILABLE)

        for tag, code, out_days_ago, due_in_days, returned_days_ago, maintenance in CHECKOUTS:
            returned_at = now - timedelta(days=returned_days_ago) if returned_days_ago is not None else None
            co = CheckOut.objects.create(
                asset=assets[tag], employee=employees[code],
                due_at=now + timedelta(days=due_in_days), returned_at=returned_at,
                condition_note="needs service" if maintenance else "",
            )
            # checked_out_at is auto_now_add, so backdate it with an UPDATE.
            CheckOut.objects.filter(pk=co.pk).update(checked_out_at=now - timedelta(days=out_days_ago))
            if returned_at is None:
                new_status = Asset.Status.CHECKED_OUT
            else:
                new_status = Asset.Status.MAINTENANCE if maintenance else Asset.Status.AVAILABLE
            Asset.objects.filter(pk=assets[tag].pk).update(status=new_status)

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=DEMO_USERNAME, defaults={"is_staff": True, "is_superuser": True},
        )
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save()
        token, _ = Token.objects.get_or_create(user=user)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(ASSETS)} assets, {len(EMPLOYEES)} employees (1 inactive), "
            f"{len(CHECKOUTS)} check-outs (2 overdue, 2 held, 2 returned on time, 2 returned late)."
        ))
        self.stdout.write(f"API user: {DEMO_USERNAME} / {DEMO_PASSWORD} (also works for /admin/)")
        self.stdout.write(f"API token: {token.key}")
        self.stdout.write(f'Try: curl -H "Authorization: Token {token.key}" http://localhost:8000/api/v1/reports/overdue/')
