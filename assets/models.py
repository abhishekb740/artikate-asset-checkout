from django.db import models
from django.db.models import Q


class Asset(models.Model):
    class Category(models.TextChoices):
        CAMERA = "CAMERA", "Camera"
        LAPTOP = "LAPTOP", "Laptop"
        SENSOR = "SENSOR", "Sensor"
        VEHICLE = "VEHICLE", "Vehicle"

    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        CHECKED_OUT = "CHECKED_OUT", "Checked out"
        MAINTENANCE = "MAINTENANCE", "Maintenance"

    asset_tag = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=16, choices=Category.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AVAILABLE)
    purchase_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["asset_tag"]

    def __str__(self) -> str:
        return f"{self.asset_tag} ({self.name})"


class Employee(models.Model):
    employee_code = models.CharField(max_length=16, unique=True, db_index=True)
    full_name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["employee_code"]

    def __str__(self) -> str:
        return f"{self.employee_code} ({self.full_name})"


class CheckOut(models.Model):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="checkouts")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="checkouts")
    checked_out_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField()
    returned_at = models.DateTimeField(null=True, blank=True)  # null means still held
    condition_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-checked_out_at"]
        constraints = [
            # Database-level backstop for business rule 7: an asset can have at
            # most one OPEN check-out. The row lock in services.check_out() is
            # what serialises concurrent requests; this partial unique index is
            # what guarantees the invariant even if some future code path
            # forgets to take the lock. (A constraint, not a new field.)
            models.UniqueConstraint(
                fields=["asset"],
                condition=Q(returned_at__isnull=True),
                name="uniq_open_checkout_per_asset",
            ),
        ]
        indexes = [
            # Serves the overdue report and the Celery task: open rows by due_at.
            models.Index(
                fields=["due_at"],
                condition=Q(returned_at__isnull=True),
                name="checkout_open_due_idx",
            ),
        ]

    @property
    def is_open(self) -> bool:
        return self.returned_at is None

    def __str__(self) -> str:
        return f"CheckOut#{self.pk} asset={self.asset_id} employee={self.employee_id}"


class OverdueNotice(models.Model):
    checkout = models.ForeignKey(CheckOut, on_delete=models.CASCADE, related_name="notices")
    notice_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-notice_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["checkout", "notice_date"],
                name="uniq_notice_per_checkout_per_day",
            ),
        ]

    def __str__(self) -> str:
        return f"Notice checkout={self.checkout_id} on {self.notice_date}"
