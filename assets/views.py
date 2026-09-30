from django.db import connection
from django.utils import timezone
from rest_framework import generics, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import selectors, services
from .models import CheckOut
from .serializers import (
    AssetSerializer,
    CheckOutCreateSerializer,
    CheckOutSerializer,
    OverdueRowSerializer,
    ReturnSerializer,
)


class AssetViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """POST /assets/, GET /assets/?status=&category=&search=, GET /assets/{id}/"""

    serializer_class = AssetSerializer
    filterset_fields = ["status", "category"]
    search_fields = ["name", "asset_tag"]

    def get_queryset(self):
        return selectors.assets_with_holder().order_by("asset_tag")


class CheckOutViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """POST /checkouts/, GET /checkouts/, GET /checkouts/{id}/, POST /checkouts/{id}/return/"""

    serializer_class = CheckOutSerializer
    queryset = CheckOut.objects.select_related("asset", "employee").order_by("-checked_out_at", "-id")

    def create(self, request):
        data = CheckOutCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        checkout = services.check_out(**data.validated_data)
        return Response(CheckOutSerializer(checkout).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="return")
    def return_(self, request, pk=None):
        data = ReturnSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        checkout = services.return_checkout(checkout_id=pk, **data.validated_data)
        return Response(CheckOutSerializer(checkout).data, status=status.HTTP_200_OK)


class EmployeeSummaryView(APIView):
    """GET /employees/{employee_code}/summary/ - four numbers, one query."""

    def get(self, request, employee_code):
        summary = selectors.employee_summary(employee_code, now=timezone.now())
        if summary is None:
            raise NotFound(f"Employee '{employee_code}' not found.")
        return Response(summary)


class OverdueReportView(generics.ListAPIView):
    """GET /reports/overdue/ - open check-outs past due, most overdue first (paginated)."""

    serializer_class = OverdueRowSerializer

    def get_queryset(self):
        return selectors.overdue_checkouts(now=self._now())

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "now": self._now()}

    def _now(self):
        # One "now" per request, so the filter and days_overdue agree.
        if not hasattr(self, "_request_now"):
            self._request_now = timezone.now()
        return self._request_now


class HealthView(APIView):
    """GET /health/ - unauthenticated liveness + database connectivity."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:  # noqa: BLE001 - any DB failure means "not healthy"
            return Response(
                {"status": "error", "database": "unreachable", "detail": exc.__class__.__name__},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"status": "ok", "database": "ok"})
