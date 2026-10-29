from __future__ import annotations

from fastapi import APIRouter, Response

from app.system.services.metrics_service import metrics_payload

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def get_metrics() -> Response:
    """Raw Prometheus scrape endpoint — kept unauthenticated for internal scraper compatibility."""
    payload, content_type = metrics_payload()
    return Response(content=payload, media_type=content_type)
