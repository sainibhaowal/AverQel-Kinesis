"""Configuration for DeepSpace Jobs service."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class JobsConfig:
    """Jobs service configuration."""

    max_concurrent_jobs_per_owner: int = 10
    retain_bytes: int = 256 * 1024
    settled_retain_bytes: int = 16 * 1024
    pump_poll_ms: int = 150

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> JobsConfig:
        s = settings or get_settings()
        return cls(
            max_concurrent_jobs_per_owner=getattr(s, "deepspace_jobs_max_concurrent", 10),
            retain_bytes=getattr(s, "deepspace_jobs_retain_bytes", 256 * 1024),
            settled_retain_bytes=getattr(s, "deepspace_jobs_settled_retain_bytes", 16 * 1024),
            pump_poll_ms=getattr(s, "deepspace_jobs_pump_poll_ms", 150),
        )


__all__ = ["JobsConfig"]
