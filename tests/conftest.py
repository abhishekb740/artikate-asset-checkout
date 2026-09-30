from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from assets.models import Asset, CheckOut, Employee


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="tester", password="pw-for-tests-only")


@pytest.fixture
def api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def make_asset(db):
    counter = {"n": 0}

    def _make(**kwargs):
        counter["n"] += 1
        defaults = {
            "asset_tag": f"TAG-{counter['n']:03d}",
            "name": f"Asset {counter['n']}",
            "category": Asset.Category.LAPTOP,
            "purchase_date": date(2025, 1, 1),
        }
        defaults.update(kwargs)
        return Asset.objects.create(**defaults)

    return _make


@pytest.fixture
def make_employee(db):
    counter = {"n": 0}

    def _make(**kwargs):
        counter["n"] += 1
        defaults = {
            "employee_code": f"E{counter['n']:03d}",
            "full_name": f"Employee {counter['n']}",
            "email": f"e{counter['n']}@example.com",
        }
        defaults.update(kwargs)
        return Employee.objects.create(**defaults)

    return _make


@pytest.fixture
def make_checkout(db):
    """Insert a CheckOut directly with explicit timestamps (bypasses auto_now_add)."""

    def _make(asset, employee, *, checked_out_at, due_at, returned_at=None):
        co = CheckOut.objects.create(asset=asset, employee=employee, due_at=due_at, returned_at=returned_at)
        CheckOut.objects.filter(pk=co.pk).update(checked_out_at=checked_out_at)
        if returned_at is None:
            Asset.objects.filter(pk=asset.pk).update(status=Asset.Status.CHECKED_OUT)
        co.refresh_from_db()
        return co

    return _make


def due_in(days=7):
    return (timezone.now() + timedelta(days=days)).isoformat()
