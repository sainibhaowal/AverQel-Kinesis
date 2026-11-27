"""Tests for system schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.system.schemas.common import HealthResponse
from app.system.schemas.metrics_summary import MetricsSummaryResponse


class TestHealthResponse:
    """Tests for the HealthResponse schema."""

    def test_creation_with_all_fields(self) -> None:
        response = HealthResponse(
            status="healthy",
            version="1.0.0",
            git_sha="abc123",
            build_timestamp_utc="2024-01-01T00:00:00Z",
        )
        assert response.status == "healthy"
        assert response.version == "1.0.0"
        assert response.git_sha == "abc123"
        assert response.build_timestamp_utc == "2024-01-01T00:00:00Z"

    def test_creation_with_required_only(self) -> None:
        response = HealthResponse(status="healthy")
        assert response.status == "healthy"
        assert response.version is None
        assert response.git_sha is None
        assert response.build_timestamp_utc is None

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            HealthResponse(status="healthy", extra_field="not allowed")

    def test_model_dump(self) -> None:
        response = HealthResponse(
            status="healthy",
            version="1.0.0",
        )
        data = response.model_dump()
        assert data == {
            "status": "healthy",
            "version": "1.0.0",
            "git_sha": None,
            "build_timestamp_utc": None,
        }


class TestMetricsSummaryResponse:
    """Tests for the MetricsSummaryResponse schema."""

    def test_creation_with_all_fields(self) -> None:
        response = MetricsSummaryResponse(
            api_requests_total=100,
            api_errors_total=5,
            db_query_count=50,
            worker_retries_total=10,
            worker_dead_letters_total=2,
            deepspace_cache_reads_total=1000,
            deepspace_cache_writes_total=500,
            deepspace_context_compactions_total=25,
            deepspace_overflow_prevented_total=3,
            deepspace_mcp_result_references_total=50,
            deepspace_budget_allocations_total=10,
            deepspace_result_reference_events_total=200,
            deepspace_execution_batches_total=30,
            deepspace_canary_mismatches_total=1,
        )
        assert response.api_requests_total == 100
        assert response.api_errors_total == 5
        assert response.db_query_count == 50
        assert response.worker_retries_total == 10
        assert response.worker_dead_letters_total == 2
        assert response.deepspace_cache_reads_total == 1000
        assert response.deepspace_cache_writes_total == 500
        assert response.deepspace_context_compactions_total == 25
        assert response.deepspace_overflow_prevented_total == 3
        assert response.deepspace_mcp_result_references_total == 50
        assert response.deepspace_budget_allocations_total == 10
        assert response.deepspace_result_reference_events_total == 200
        assert response.deepspace_execution_batches_total == 30
        assert response.deepspace_canary_mismatches_total == 1

    def test_creation_with_required_only(self) -> None:
        response = MetricsSummaryResponse(
            api_requests_total=100,
            api_errors_total=5,
            db_query_count=50,
        )
        assert response.api_requests_total == 100
        assert response.api_errors_total == 5
        assert response.db_query_count == 50
        assert response.worker_retries_total == 0
        assert response.worker_dead_letters_total == 0
        assert response.deepspace_cache_reads_total == 0
        assert response.deepspace_cache_writes_total == 0
        assert response.deepspace_context_compactions_total == 0
        assert response.deepspace_overflow_prevented_total == 0
        assert response.deepspace_mcp_result_references_total == 0
        assert response.deepspace_budget_allocations_total == 0
        assert response.deepspace_result_reference_events_total == 0
        assert response.deepspace_execution_batches_total == 0
        assert response.deepspace_canary_mismatches_total == 0

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            MetricsSummaryResponse(
                api_requests_total=100,
                api_errors_total=5,
                db_query_count=50,
                extra_field="not allowed",
            )

    def test_model_dump(self) -> None:
        response = MetricsSummaryResponse(
            api_requests_total=100,
            api_errors_total=5,
            db_query_count=50,
        )
        data = response.model_dump()
        assert data["api_requests_total"] == 100
        assert data["api_errors_total"] == 5
        assert data["db_query_count"] == 50
        assert data["worker_retries_total"] == 0
        assert data["deepspace_cache_reads_total"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
