"""Write-side business logic for check-outs and returns.

Kept out of the views so the rules can be unit-tested (and called from the
admin, a management command or a task) without going through HTTP.

Locking strategy (business rule 7)
----------------------------------
check_out() takes a row lock on the Asset (SELECT ... FOR UPDATE) inside a
transaction *before* reading its status. Two concurrent requests for the same
asset therefore serialise on that lock: the second one blocks until the first
commits, then re-reads the row, sees CHECKED_OUT and gets 409.

The Employee row is locked too, so two concurrent check-outs of *different*
assets by the *same* employee cannot both pass the "at most three open"
count (rule 3) - without that lock both would count 2 and both would insert.

Locks are always taken in the order Asset -> Employee, and return_checkout()
never locks an Employee, so the two code paths cannot deadlock each other.

The partial unique index uniq_open_checkout_per_asset is a second, independent
line of defence: if the lock were ever bypassed, the INSERT itself fails with
IntegrityError, which is also mapped to 409.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, ValidationError

from .models import Asset, CheckOut, Employee

MAX_OPEN_CHECKOUTS = 3
MAX_LOAN_DAYS = 30


class Conflict(APIException):
    status_code = 409
    default_detail = "Conflict."
    default_code = "conflict"


def validate_due_at(due_at: datetime, now: datetime | None = None) -> None:
    """Rule 4: due_at must be in the future and at most 30 days ahead."""
    now = now or timezone.now()
    if due_at <= now:
        raise ValidationError({"due_at": "due_at must be in the future."})
    if due_at > now + timedelta(days=MAX_LOAN_DAYS):
        raise ValidationError({"due_at": f"due_at must be at most {MAX_LOAN_DAYS} days from now."})


def check_out(*, asset_tag: str, employee_code: str, due_at: datetime) -> CheckOut:
    """Check an asset out to an employee. Applies rules 1-5, 7 and 8.

    Order of checks: 400 bad due_at (pure input validation, no DB needed) ->
    404 unknown tag/code -> 400 inactive employee -> 409 asset not available
    -> 409 employee at limit.
    """
    now = timezone.now()
    validate_due_at(due_at, now)

    try:
        with transaction.atomic():
            try:
                asset = Asset.objects.select_for_update().get(asset_tag=asset_tag)
            except Asset.DoesNotExist:
                raise NotFound(f"Asset '{asset_tag}' not found.") from None
            try:
                employee = Employee.objects.select_for_update().get(employee_code=employee_code)
            except Employee.DoesNotExist:
                raise NotFound(f"Employee '{employee_code}' not found.") from None

            if not employee.is_active:
                raise ValidationError({"employee_code": "Inactive employees cannot check out assets."})

            # Status is read AFTER the lock is held, so it is the committed value.
            if asset.status != Asset.Status.AVAILABLE:
                raise Conflict(f"Asset '{asset_tag}' is not available (status {asset.status}).")

            open_count = CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count()
            if open_count >= MAX_OPEN_CHECKOUTS:
                raise Conflict(
                    f"Employee '{employee_code}' already holds {open_count} open check-outs "
                    f"(limit {MAX_OPEN_CHECKOUTS})."
                )

            # Rule 5: both writes in one transaction - they commit or roll back together.
            checkout = CheckOut.objects.create(asset=asset, employee=employee, due_at=due_at)
            asset.status = Asset.Status.CHECKED_OUT
            asset.save(update_fields=["status", "updated_at"])
    except IntegrityError as exc:
        # Only reachable if something bypassed the row lock; the partial unique
        # index rejected a second open check-out for this asset.
        raise Conflict(f"Asset '{asset_tag}' is already checked out.") from exc
    return checkout


def return_checkout(*, checkout_id: int, condition_note: str = "", needs_maintenance: bool = False) -> CheckOut:
    """Rule 6: close a check-out and release (or quarantine) the asset."""
    with transaction.atomic():
        try:
            checkout = CheckOut.objects.select_for_update().get(pk=checkout_id)
        except CheckOut.DoesNotExist:
            raise NotFound(f"Check-out {checkout_id} not found.") from None
        if checkout.returned_at is not None:
            raise Conflict(f"Check-out {checkout_id} was already returned at {checkout.returned_at.isoformat()}.")

        asset = Asset.objects.select_for_update().get(pk=checkout.asset_id)
        checkout.returned_at = timezone.now()
        checkout.condition_note = condition_note
        checkout.save(update_fields=["returned_at", "condition_note"])

        asset.status = Asset.Status.MAINTENANCE if needs_maintenance else Asset.Status.AVAILABLE
        asset.save(update_fields=["status", "updated_at"])
    checkout.asset = asset
    return checkout
