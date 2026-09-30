from rest_framework import serializers

from .models import Asset, CheckOut
from .selectors import days_overdue


class AssetSerializer(serializers.ModelSerializer):
    current_holder = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = [
            "id", "asset_tag", "name", "category", "status", "purchase_date",
            "current_holder", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "current_holder", "created_at", "updated_at"]

    def get_current_holder(self, obj):
        # holder_code/holder_name are annotated by selectors.assets_with_holder().
        code = getattr(obj, "holder_code", None)
        if code is None:
            return None
        return {"employee_code": code, "full_name": obj.holder_name}

    def validate_status(self, value):
        # CHECKED_OUT is only reachable through POST /checkouts/, which also
        # creates the CheckOut row. Allowing it here would create an asset that
        # is "checked out" to nobody.
        if value == Asset.Status.CHECKED_OUT:
            raise serializers.ValidationError("Use POST /api/v1/checkouts/ to check an asset out.")
        return value


class CheckOutSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.asset_tag", read_only=True)
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    asset_status = serializers.CharField(source="asset.status", read_only=True)
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)

    class Meta:
        model = CheckOut
        fields = [
            "id", "asset_tag", "asset_name", "asset_status", "employee_code", "employee_name",
            "checked_out_at", "due_at", "returned_at", "condition_note",
        ]
        read_only_fields = fields


class CheckOutCreateSerializer(serializers.Serializer):
    """Input shape only; the rules live in services.check_out()."""
    asset_tag = serializers.CharField(max_length=32)
    employee_code = serializers.CharField(max_length=16)
    due_at = serializers.DateTimeField()


class ReturnSerializer(serializers.Serializer):
    condition_note = serializers.CharField(required=False, allow_blank=True, default="")
    needs_maintenance = serializers.BooleanField(required=False, default=False)


class OverdueRowSerializer(serializers.Serializer):
    checkout_id = serializers.IntegerField(source="id")
    asset_name = serializers.CharField(source="asset.name")
    asset_tag = serializers.CharField(source="asset.asset_tag")
    employee_code = serializers.CharField(source="employee.employee_code")
    employee_name = serializers.CharField(source="employee.full_name")
    due_at = serializers.DateTimeField()
    days_overdue = serializers.SerializerMethodField()

    def get_days_overdue(self, obj):
        return days_overdue(obj.due_at, self.context["now"])
