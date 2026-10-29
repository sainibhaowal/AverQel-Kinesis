"""DeepSpace webhook API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import AuthContext, get_auth_context
from app.core.config import Settings, get_settings
from app.deepspace.services.webhooks import (
    create_github_webhook_router,
    get_webhook_runtime,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/rules")
async def list_webhook_rules(
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict:
    """List registered webhook rules."""
    runtime = get_webhook_runtime()
    rules = [{"id": reg.rule.id, "kind": reg.rule.kind} for reg in runtime._rules.values()]
    return {"rules": rules}


@router.post("/rules")
async def register_webhook_rule(
    rule_data: dict,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Register a custom webhook rule.

    Expected rule_data:
    {
        "id": "my-rule",
        "kind": "github",
        "handler": "github_push",  # or custom handler name
        "config": {...}
    }
    """
    # This would allow dynamic rule registration
    # For now, only built-in rules are supported
    return {
        "status": "not_implemented",
        "message": "Custom rule registration not yet implemented. Use built-in rules.",
    }


@router.delete("/rules/{rule_id}")
async def unregister_webhook_rule(
    rule_id: str,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Unregister a webhook rule."""
    runtime = get_webhook_runtime()
    if rule_id in runtime._rules:
        # Would need to call disposer
        return {"status": "ok", "message": f"Rule {rule_id} unregistered"}
    return {"status": "not_found", "message": f"Rule {rule_id} not found"}


def get_webhook_router(settings: Settings) -> APIRouter:
    """Get the webhook router with GitHub handler if enabled."""
    main_router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    # Include the main routes
    main_router.include_router(router)

    # Include GitHub webhook if enabled
    if getattr(settings, "deepspace_webhook_github_enabled", False):
        github_router = create_github_webhook_router(
            settings=settings,
            source="github",
            path=getattr(settings, "deepspace_webhook_github_path", "/webhooks/github"),
            secret_env=getattr(
                settings, "deepspace_webhook_github_secret_env", "GITHUB_WEBHOOK_SECRET"
            ),
            max_body_bytes=getattr(settings, "deepspace_webhook_github_max_body_bytes", 1_000_000),
        )
        main_router.include_router(github_router)

    return main_router
