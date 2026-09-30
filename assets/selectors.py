"""Read-side queries. Each function is a single database round-trip."""
from __future__ import annotations

from datetime import datetime

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, OuterRef, Q, QuerySet, Subquery

from .models import Asset, CheckOut, Employee


def overdue_q(now: datetime, prefix: str = "") -> Q:
    """An open check-out is overdue when due_at is strictly before `now`.

    An item due *exactly* now is not yet overdue (see README, Assumptions).
    """
    return Q(**{f"{prefix}returned_at__isnull": True, f"{prefix}due_at__lt": now})


def overdue_checkouts(now: datetime) -> QuerySet[CheckOut]:
    """Open check-outs past due, most overdue first, with asset/employee joined in.

    select_related turns the per-row asset/employee lookups into one JOIN, so
    the report costs one query regardless of row count.
    """
    return (
        CheckOut.objects.filter(overdue_q(now))
        .select_related("asset", "employee")
        .only(
            "id", "due_at", "checked_out_at",
            "asset__name", "asset__asset_tag",
            "employee__employee_code", "employee__full_name",
        )
        .order_by("due_at", "id")  # earliest due = most overdue; id breaks ties deterministically
    )


def days_overdue(due_at: datetime, now: datetime) -> int:
    """Whole days elapsed since due_at (floor). 0 for something due minutes ago."""
    return max((now - due_at).days, 0)


def employee_summary(employee_code: str, now: datetime) -> dict | None:
    """The four summary numbers for one employee, in ONE query.

    Annotating the Employee row (rather than aggregating CheckOut separately)
    means the existence check and all four numbers come back together. There is
    only one join (employee -> checkouts), so the conditional COUNTs cannot be
    inflated by a join fan-out.
    """
    hold_duration = ExpressionWrapper(
        F("checkouts__returned_at") - F("checkouts__checked_out_at"),
        output_field=DurationField(),
    )
    row = (
        Employee.objects.filter(employee_code=employee_code)
        .annotate(
            lifetime_checkouts=Count("checkouts"),
            currently_held=Count("checkouts", filter=Q(checkouts__returned_at__isnull=True)),
            currently_overdue=Count("checkouts", filter=overdue_q(now, prefix="checkouts__")),
            mean_hold=Avg(hold_duration, filter=Q(checkouts__returned_at__isnull=False)),
        )
        .values(
            "employee_code", "full_name",
            "lifetime_checkouts", "currently_held", "currently_overdue", "mean_hold",
        )
        .first()
    )
    if row is None:
        return None
    mean_hold = row.pop("mean_hold")
    row["mean_hold_days"] = round(mean_hold.total_seconds() / 86400, 2) if mean_hold is not None else None
    return row


def assets_with_holder() -> QuerySet[Asset]:
    """Assets annotated with their current holder (from the single open check-out).

    Correlated subqueries keep the asset list endpoint at one query instead of
    one extra lookup per asset.
    """
    open_checkout = CheckOut.objects.filter(asset=OuterRef("pk"), returned_at__isnull=True)
    return Asset.objects.annotate(
        holder_code=Subquery(open_checkout.values("employee__employee_code")[:1]),
        holder_name=Subquery(open_checkout.values("employee__full_name")[:1]),
    )
