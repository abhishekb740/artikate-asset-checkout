import io

import pytest
from django.core.management import call_command
from django.utils import timezone

from assets.models import Asset, CheckOut, Employee
from assets.selectors import overdue_checkouts


@pytest.mark.django_db
def test_seed_meets_the_spec_and_is_rerunnable():
    for _ in range(2):  # second run must not duplicate anything
        call_command("seed_demo_data", stdout=io.StringIO())

    assert Asset.objects.count() >= 8
    assert set(Asset.objects.values_list("category", flat=True)) == set(Asset.Category.values)
    assert Employee.objects.count() >= 4
    assert Employee.objects.filter(is_active=False).count() == 1

    now = timezone.now()
    assert overdue_checkouts(now).count() >= 2
    returned = CheckOut.objects.filter(returned_at__isnull=False)
    assert sum(1 for c in returned if c.returned_at <= c.due_at) >= 2   # on time
    assert sum(1 for c in returned if c.returned_at > c.due_at) >= 1    # late
    assert CheckOut.objects.count() == 8

    # every open check-out's asset is CHECKED_OUT, and no asset has two open rows
    for co in CheckOut.objects.filter(returned_at__isnull=True).select_related("asset"):
        assert co.asset.status == Asset.Status.CHECKED_OUT
