"""Business rules 1-6 and 8, exercised through the HTTP API."""
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from assets.models import Asset, CheckOut

from .conftest import due_in

URL = "/api/v1/checkouts/"


def post_checkout(api, asset, employee, due_at=None):
    return api.post(
        URL,
        {"asset_tag": asset.asset_tag, "employee_code": employee.employee_code, "due_at": due_at or due_in()},
        format="json",
    )


@pytest.mark.django_db
class TestCheckOut:
    def test_success_creates_row_and_flips_status(self, api, make_asset, make_employee):
        asset, emp = make_asset(), make_employee()
        resp = post_checkout(api, asset, emp)
        assert resp.status_code == 201, resp.data
        assert resp.data["asset_tag"] == asset.asset_tag
        assert resp.data["returned_at"] is None
        asset.refresh_from_db()
        assert asset.status == Asset.Status.CHECKED_OUT
        assert CheckOut.objects.filter(asset=asset, returned_at__isnull=True).count() == 1

    @pytest.mark.parametrize("status", [Asset.Status.CHECKED_OUT, Asset.Status.MAINTENANCE])
    def test_rule1_unavailable_asset_is_409(self, api, make_asset, make_employee, status):
        resp = post_checkout(api, make_asset(status=status), make_employee())
        assert resp.status_code == 409

    def test_rule2_inactive_employee_is_400(self, api, make_asset, make_employee):
        resp = post_checkout(api, make_asset(), make_employee(is_active=False))
        assert resp.status_code == 400
        assert "employee_code" in resp.data

    def test_rule3_fourth_open_checkout_is_409(self, api, make_asset, make_employee):
        emp = make_employee()
        for _ in range(3):
            assert post_checkout(api, make_asset(), emp).status_code == 201
        resp = post_checkout(api, make_asset(), emp)
        assert resp.status_code == 409
        assert CheckOut.objects.filter(employee=emp, returned_at__isnull=True).count() == 3

    def test_rule3_returned_items_do_not_count(self, api, make_asset, make_employee):
        emp = make_employee()
        ids = [post_checkout(api, make_asset(), emp).data["id"] for _ in range(3)]
        api.post(f"{URL}{ids[0]}/return/", {}, format="json")
        assert post_checkout(api, make_asset(), emp).status_code == 201

    @pytest.mark.parametrize(
        "delta",
        [timedelta(seconds=-1), timedelta(0), timedelta(days=30, minutes=1)],
        ids=["past", "now", "over-30-days"],
    )
    def test_rule4_bad_due_at_is_400(self, api, make_asset, make_employee, delta):
        resp = post_checkout(api, make_asset(), make_employee(), due_at=(timezone.now() + delta).isoformat())
        assert resp.status_code == 400
        assert "due_at" in resp.data

    def test_rule4_just_under_30_days_is_accepted(self, api, make_asset, make_employee):
        due = (timezone.now() + timedelta(days=30) - timedelta(minutes=1)).isoformat()
        assert post_checkout(api, make_asset(), make_employee(), due_at=due).status_code == 201

    def test_rule5_failure_after_insert_rolls_back_both_writes(self, api, make_asset, make_employee):
        asset, emp = make_asset(), make_employee()
        # Make the second write (asset status update) blow up after the CheckOut INSERT.
        with mock.patch.object(Asset, "save", side_effect=RuntimeError("disk full")), pytest.raises(RuntimeError):
            post_checkout(api, asset, emp)
        asset.refresh_from_db()
        assert asset.status == Asset.Status.AVAILABLE
        assert not CheckOut.objects.filter(asset=asset).exists()

    def test_rule8_unknown_asset_is_404(self, api, make_asset, make_employee):
        resp = api.post(URL, {"asset_tag": "NOPE", "employee_code": make_employee().employee_code,
                              "due_at": due_in()}, format="json")
        assert resp.status_code == 404

    def test_rule8_unknown_employee_is_404(self, api, make_asset):
        resp = api.post(URL, {"asset_tag": make_asset().asset_tag, "employee_code": "NOPE",
                              "due_at": due_in()}, format="json")
        assert resp.status_code == 404

    def test_missing_fields_is_400_not_500(self, api):
        assert api.post(URL, {}, format="json").status_code == 400

    def test_requires_authentication(self, make_asset, make_employee):
        from rest_framework.test import APIClient
        assert post_checkout(APIClient(), make_asset(), make_employee()).status_code == 401


@pytest.mark.django_db
class TestReturn:
    def _checkout(self, api, make_asset, make_employee):
        asset = make_asset()
        return asset, post_checkout(api, asset, make_employee()).data["id"]

    def test_return_makes_asset_available(self, api, make_asset, make_employee):
        asset, cid = self._checkout(api, make_asset, make_employee)
        resp = api.post(f"{URL}{cid}/return/", {"condition_note": "fine"}, format="json")
        assert resp.status_code == 200
        assert resp.data["returned_at"] is not None
        assert resp.data["condition_note"] == "fine"
        asset.refresh_from_db()
        assert asset.status == Asset.Status.AVAILABLE

    def test_return_with_maintenance_flag(self, api, make_asset, make_employee):
        asset, cid = self._checkout(api, make_asset, make_employee)
        resp = api.post(f"{URL}{cid}/return/", {"condition_note": "cracked lens", "needs_maintenance": True},
                        format="json")
        assert resp.status_code == 200
        asset.refresh_from_db()
        assert asset.status == Asset.Status.MAINTENANCE

    def test_rule6_double_return_is_409(self, api, make_asset, make_employee):
        _, cid = self._checkout(api, make_asset, make_employee)
        assert api.post(f"{URL}{cid}/return/", {}, format="json").status_code == 200
        assert api.post(f"{URL}{cid}/return/", {}, format="json").status_code == 409

    def test_return_unknown_checkout_is_404(self, api):
        assert api.post(f"{URL}999999/return/", {}, format="json").status_code == 404
