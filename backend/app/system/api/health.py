from __future__ import annotations

import logging
import os
import platform
import sys
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.ingestion.services.security.malware_scan_service import MalwareScanService
from app.platform.database.session import get_engine
from app.system.services.cache_service import get_redis_client

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: str
    version: str | None = None
    git_sha: str | None = None
    build_timestamp_utc: str | None = None


class DetailedHealthResponse(BaseModel):
    status: str
    version: str | None = None
    git_sha: str | None = None
    build_timestamp_utc: str | None = None
    timestamp: str
    uptime_seconds: float
    checks: dict[str, Any]
    system: dict[str, Any]


class CrashReport(BaseModel):
    error_type: str
    error_message: str
    stack_trace: str | None = None
    context: dict[str, Any] | None = None
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    user_id: str | None = None
    conversation_id: str | None = None
    app_version: str | None = None
    platform: str = Field(default_factory=lambda: platform.platform())


class CrashReportResponse(BaseModel):
    success: bool
    report_id: str | None = None
    message: str


class AutoUpdateCheckResponse(BaseModel):
    update_available: bool
    current_version: str
    latest_version: str | None = None
    release_notes: str | None = None
    download_url: str | None = None
    mandatory: bool = False


router = APIRouter(prefix="/health", tags=["health"])


# Track application start time for uptime
_start_time = time.time()


def _get_uptime_seconds() -> float:
    return time.time() - _start_time


@router.get("/live", response_model=HealthResponse)
def live(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Liveness probe - indicates the process is running."""
    return HealthResponse(
        status="ok",
        version=settings.release_version or settings.app_version,
        git_sha=settings.git_sha,
        build_timestamp_utc=settings.build_timestamp_utc,
    )


@router.get("/ready", response_model=HealthResponse)
def ready(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Readiness probe - indicates the service can handle requests."""
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise ApiError(
            code="DATABASE_NOT_READY",
            message="Database dependency is not ready.",
            status_code=503,
        ) from exc

    try:
        redis_client = get_redis_client()
        redis_client.ping()
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            code="REDIS_NOT_READY",
            message="Redis dependency is not ready.",
            status_code=503,
        ) from exc

    if settings.malware_scan_enabled and settings.malware_scan_required:
        if not MalwareScanService(settings).check_available():
            raise ApiError(
                code="MALWARE_SCANNER_NOT_READY",
                message="Malware scanner dependency is not ready.",
                status_code=503,
            ) from exc

    return HealthResponse(
        status="ok",
        version=settings.release_version or settings.app_version,
        git_sha=settings.git_sha,
        build_timestamp_utc=settings.build_timestamp_utc,
    )


@router.get("/detailed", response_model=DetailedHealthResponse)
def detailed_health(
    settings: Settings = Depends(get_settings),
) -> DetailedHealthResponse:
    """Detailed health check with comprehensive system information."""
    checks = {}
    overall_status = "ok"

    # Database check
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok", "message": "Database connection successful"}
    except SQLAlchemyError as exc:
        checks["database"] = {"status": "error", "message": str(exc)}
        overall_status = "degraded"

    # Redis check
    try:
        redis_client = get_redis_client()
        redis_client.ping()
        checks["redis"] = {"status": "ok", "message": "Redis connection successful"}
    except Exception as exc:
        checks["redis"] = {"status": "error", "message": str(exc)}
        overall_status = "degraded"

    # Malware scanner check
    if settings.malware_scan_enabled and settings.malware_scan_required:
        try:
            if MalwareScanService(settings).check_available():
                checks["malware_scanner"] = {"status": "ok", "message": "Scanner available"}
            else:
                checks["malware_scanner"] = {"status": "error", "message": "Scanner not available"}
                overall_status = "degraded"
        except Exception as exc:
            checks["malware_scanner"] = {"status": "error", "message": str(exc)}
            overall_status = "degraded"
    else:
        checks["malware_scanner"] = {"status": "skipped", "message": "Malware scanner not required"}

    # Disk space check
    try:
        import shutil

        total, used, free = shutil.disk_usage("/")
        free_percent = (free / total) * 100
        if free_percent < 10:
            checks["disk"] = {
                "status": "warning",
                "message": f"Low disk space: {free_percent:.1f}% free",
            }
            overall_status = "degraded"
        else:
            checks["disk"] = {"status": "ok", "message": f"Disk space: {free_percent:.1f}% free"}
    except Exception:
        checks["disk"] = {"status": "unknown", "message": "Could not check disk space"}

    # Memory check
    try:
        import psutil

        mem = psutil.virtual_memory()
        if mem.percent > 90:
            checks["memory"] = {
                "status": "warning",
                "message": f"High memory usage: {mem.percent:.1f}%",
            }
            overall_status = "degraded"
        else:
            checks["memory"] = {"status": "ok", "message": f"Memory usage: {mem.percent:.1f}%"}
    except Exception:
        checks["memory"] = {"status": "unknown", "message": "Could not check memory"}

    # System info
    system_info = {
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "architecture": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count() or 0,
        "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
    }

    return DetailedHealthResponse(
        status=overall_status,
        version=get_settings().release_version or get_settings().app_version,
        git_sha=get_settings().git_sha,
        build_timestamp_utc=get_settings().build_timestamp_utc,
        timestamp=datetime.now(UTC).isoformat(),
        uptime_seconds=_get_uptime_seconds(),
        checks=checks,
        system=system_info,
    )


@router.post("/crash-report", response_model=CrashReportResponse)
async def submit_crash_report(report: CrashReport) -> CrashReportResponse:
    """Submit a crash report for analysis."""
    try:
        # Generate a unique report ID
        import uuid

        report_id = str(uuid.uuid4())[:8]

        # Log the crash report
        logger.error(
            "Crash report received",
            extra={
                "report_id": report_id,
                "error_type": report.error_type,
                "error_message": report.error_message,
                "user_id": report.user_id,
                "conversation_id": report.conversation_id,
                "app_version": report.app_version,
                "platform": report.platform,
            },
        )

        # In production, you would send this to a crash reporting service
        # like Sentry, Bugsnag, or a custom endpoint
        # For now, we just log it

        return CrashReportResponse(
            success=True,
            report_id=report_id,
            message="Crash report received successfully",
        )
    except Exception as exc:
        logger.error("Failed to process crash report: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to process crash report",
        ) from exc


@router.get("/auto-update", response_model=AutoUpdateCheckResponse)
async def check_auto_update(
    current_version: str = Query(..., description="Current application version"),
    platform: str = Query(..., description="Platform: windows, macos, linux"),
    arch: str = Query("x64", description="Architecture: x64, arm64"),
    channel: str = Query("stable", description="Release channel: stable, beta, nightly"),
) -> AutoUpdateCheckResponse:
    """Check for application updates."""
    # In production, this would query a release server or GitHub releases
    # For now, return no update available
    return AutoUpdateCheckResponse(
        update_available=False,
        current_version=current_version,
        latest_version=None,
        release_notes=None,
        download_url=None,
        mandatory=False,
    )


def _get_uptime_seconds() -> float:
    return time.time() - _start_time


__all__ = [
    "router",
    "HealthResponse",
    "DetailedHealthResponse",
    "CrashReport",
    "CrashReportResponse",
    "AutoUpdateCheckResponse",
]