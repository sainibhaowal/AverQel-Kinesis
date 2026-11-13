from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from app.system.api.admin import require_platform_admin_access
from app.system.services.metrics_service import (
    API_ERRORS_TOTAL,
    API_REQUESTS_TOTAL,
    DB_QUERY_DURATION_SECONDS,
    metrics_payload,
)

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def get_metrics() -> Response:
    """Raw Prometheus scrape endpoint — kept unauthenticated for internal scraper compatibility."""
    payload, content_type = metrics_payload()
    return Response(content=payload, media_type=content_type)


def _metric_sample_sum(collector: Any, suffix: str) -> int:
    """Sum one aggregate sample across a Prometheus collector."""
    total = 0.0
    for family in collector.collect():
        sample_name = f"{family.name}{suffix}"
        total += sum(sample.value for sample in family.samples if sample.name == sample_name)
    return int(total)


@router.get(
    "/metrics/summary",
    tags=["admin metrics"],
    dependencies=[Depends(require_platform_admin_access)],
)
def get_metrics_summary() -> dict[str, int]:
    """Return aggregate operational counters to allowlisted platform admins."""
    return {
        "api_requests_total": _metric_sample_sum(API_REQUESTS_TOTAL, "_total"),
        "api_errors_total": _metric_sample_sum(API_ERRORS_TOTAL, "_total"),
        "db_query_count": _metric_sample_sum(DB_QUERY_DURATION_SECONDS, "_count"),
    }
