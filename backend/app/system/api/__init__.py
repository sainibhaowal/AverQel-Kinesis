"""System and administration API routes."""

from app.system.api import (
    app_feedback,
    capabilities,
    dashboard,
    feedback,
    health,
    metrics,
    notifications,
    plans,
    storage,
    support,
)

__all__ = [
    "app_feedback",
    "capabilities",
    "dashboard",
    "feedback",
    "health",
    "metrics",
    "notifications",
    "plans",
    "storage",
    "support",
]
