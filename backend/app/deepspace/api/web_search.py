"""Diagnostics for the DeepSpace web-search runtime."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.auth.dependencies import AuthContext, get_auth_context
from app.core.config import get_settings
from app.providers.services.searxng_runtime import SearXNGRuntime

router = APIRouter(prefix="/deepspace/web-search", tags=["deepspace-web-search"])


@router.get("/status")
def web_search_status(
    request: Request,
    auth_context: AuthContext = Depends(get_auth_context),
) -> dict[str, object]:
    """Return the authenticated user's local web-search runtime status."""
    del auth_context
    runtime = getattr(request.app.state, "searxng_runtime", None)
    if runtime is None:
        runtime = SearXNGRuntime(get_settings())
    status = runtime.status()
    return {
        "provider": status["provider"],
        "state": status["state"],
        "enabled": status["enabled"],
        "base_url": status["base_url"],
        "local_only": status["local_only"],
        "pid": status["pid"],
        "error": status["error"],
    }


__all__ = ["router"]
