from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.rbac import require_permissions
from app.auth.tenancy import TenantContext, get_tenant_context
from app.platform.database.session import get_db
from app.query.models.query import Query

router = APIRouter(tags=["analytics"])

_WINDOW = timedelta(days=30)
_MIN_CONTRIBUTORS = 5


class QueryVolumeDay(BaseModel):
    date: date
    queries: int


class AnalyticsDashboardResponse(BaseModel):
    suppressed: bool
    total_queries: int | None
    volume_over_time: list[QueryVolumeDay]


@router.get(
    "/analytics/dashboard",
    response_model=AnalyticsDashboardResponse,
    dependencies=[Depends(require_permissions("admin:analytics:read"))],
)
def get_analytics_dashboard(
    tenant_context: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> AnalyticsDashboardResponse:
    """Return privacy-preserving query aggregates for the active tenant."""
    window_end = datetime.now(UTC)
    window_start = window_end - _WINDOW
    scoped_queries = (
        Query.tenant_id == tenant_context.tenant_id,
        Query.created_at >= window_start,
        Query.created_at <= window_end,
    )
    total_queries, contributor_count = db.execute(
        select(
            func.count(Query.id),
            func.count(func.distinct(Query.user_id)),
        ).where(*scoped_queries)
    ).one()

    if int(contributor_count or 0) < _MIN_CONTRIBUTORS:
        return AnalyticsDashboardResponse(
            suppressed=True,
            total_queries=None,
            volume_over_time=[],
        )

    day = func.date(Query.created_at)
    daily_rows = db.execute(
        select(
            day,
            func.count(Query.id),
            func.count(func.distinct(Query.user_id)),
        )
        .where(*scoped_queries)
        .group_by(day)
        .order_by(day)
    ).all()
    volume = [
        QueryVolumeDay(date=date.fromisoformat(str(day_value)), queries=int(query_count))
        for day_value, query_count, daily_contributors in daily_rows
        if int(daily_contributors or 0) >= _MIN_CONTRIBUTORS
    ]
    return AnalyticsDashboardResponse(
        suppressed=False,
        total_queries=int(total_queries or 0),
        volume_over_time=volume,
    )
