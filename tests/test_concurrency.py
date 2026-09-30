"""Rule 7: two simultaneous check-outs of one asset -> exactly one succeeds.

These tests need real concurrent transactions, so they use
django_db(transaction=True) (a TransactionTestCase: data is committed and
visible across connections) and run each request on its own thread, and so
its own database connection. A Barrier releases both threads at the same
instant.
"""
import threading
import time
from datetime import timedelta
from unittest import mock

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.test import APIClient

from assets.models import Asset, CheckOut

_real_count = QuerySet.count


def _slow_count(self):
    """QuerySet.count() that holds the transaction open for a moment afterwards.

    Starting two threads at a Barrier is not enough on its own: the first
    request can finish before the second one reaches its own reads, and then
    the test passes whether or not the code locks anything (verified by
    deleting the locks - it still passed). Sleeping right after the
    open-check-out count widens the check-then-insert window so that, without
    a lock, both requests are guaranteed to read before either one writes.
    """
    result = _real_count(self)
    time.sleep(0.3)
    return result


def _race(user, payloads):
    """POST every payload to /checkouts/ at the same moment; return status codes."""
    barrier = threading.Barrier(len(payloads))
    results = [None] * len(payloads)

    def worker(i, payload):
        client = APIClient()
        client.force_authenticate(user)
        try:
            barrier.wait()
            results[i] = client.post("/api/v1/checkouts/", payload, format="json").status_code
        finally:
            connection.close()  # each thread owns its own connection

    with mock.patch.object(QuerySet, "count", _slow_count):
        threads = [threading.Thread(target=worker, args=(i, p)) for i, p in enumerate(payloads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    return results


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("attempt", range(5))  # repeat: a race test that passes once proves little
def test_two_simultaneous_checkouts_of_one_asset(user, make_asset, make_employee, attempt):
    asset = make_asset()
    e1, e2 = make_employee(), make_employee()
    due = (timezone.now() + timedelta(days=3)).isoformat()

    codes = _race(user, [
        {"asset_tag": asset.asset_tag, "employee_code": e1.employee_code, "due_at": due},
        {"asset_tag": asset.asset_tag, "employee_code": e2.employee_code, "due_at": due},
    ])

    assert sorted(codes) == [201, 409]
    assert CheckOut.objects.filter(asset=asset).count() == 1
    asset.refresh_from_db()
    assert asset.status == Asset.Status.CHECKED_OUT


@pytest.mark.django_db(transaction=True)
def test_same_employee_racing_for_fourth_slot(user, make_asset, make_employee, make_checkout):
    """Rule 3 under concurrency: employee holds 2, two requests race for the 3rd slot
    with different assets. Without the Employee row lock both would see 2 and both insert."""
    emp = make_employee()
    now = timezone.now()
    for _ in range(2):
        make_checkout(make_asset(), emp, checked_out_at=now, due_at=now + timedelta(days=2))
    a, b = make_asset(), make_asset()
    due = (now + timedelta(days=3)).isoformat()

    codes = _race(user, [
        {"asset_tag": a.asset_tag, "employee_code": emp.employee_code, "due_at": due},
        {"asset_tag": b.asset_tag, "employee_code": emp.employee_code, "due_at": due},
    ])

    assert sorted(codes) == [201, 409]
    assert CheckOut.objects.filter(employee=emp, returned_at__isnull=True).count() == 3


@pytest.mark.django_db
def test_partial_unique_index_is_the_backstop(make_asset, make_employee):
    """Even code that skips the lock cannot create a second open check-out."""
    asset = make_asset()
    due = timezone.now() + timedelta(days=1)
    CheckOut.objects.create(asset=asset, employee=make_employee(), due_at=due)
    with pytest.raises(IntegrityError), transaction.atomic():
        CheckOut.objects.create(asset=asset, employee=make_employee(), due_at=due)
