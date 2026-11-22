"""Authenticated entry point for isolated Python/SQL execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.core.config import Settings, get_settings
from app.deepspace.services.runtime_observability import runtime_activity_status
from app.deepspace.services.sandbox_backend import sandbox_status
from app.deepspace.services.sandbox_executor import SandboxExecutorError, execute_sandbox
from app.deepspace.services.tools.browser_automation import browser_automation_unavailable_reason
from app.deepspace.services.tools.computer_use import (
    COMPUTER_USE_TOOLS,
    computer_use_unavailable_reason,
)
from app.platform.database.session import get_db

router = APIRouter(prefix="/deepspace/sandbox", tags=["deepspace-sandbox"])


class SandboxExecuteRequest(BaseModel):
    language: str = Field(pattern="^(python|sql|bash|pwsh)$")
    code: str = Field(min_length=1, max_length=100_000)
    input: dict[str, Any] = Field(default_factory=dict)
    files: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    timeout_seconds: int = Field(default=20, ge=1, le=30)


@router.post(
    "/execute",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def execute(
    payload: SandboxExecuteRequest,
    _auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Execute in a separate, OS-confined child process or configured executor."""
    try:
        return await execute_sandbox(
            code=payload.code,
            language=payload.language,
            settings=settings,
            input_data=payload.input,
            files=payload.files,
            timeout_seconds=payload.timeout_seconds,
        )
    except SandboxExecutorError as exc:
        return {"status": "unavailable", "execution": "isolated_sandbox", "message": str(exc)}


@router.get(
    "/backend",
    dependencies=[Depends(require_permissions("queries:run"))],
)
async def sandbox_backend_status(
    _auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Report which operating-system cage the local shell tools will use."""
    return sandbox_status(settings)


@router.get(
    "/runtime",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def deepspace_runtime_status(
    _auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Report the runtime controls a deployment's DeepSpace uses.

    The settings a deployment chooses are read from the configuration, and
    the sandbox report says which operating-system cage the shell tools
    will actually use on this host.
    """
    permission_default = str(getattr(settings, "deepspace_permission_mode", "workspace-write"))
    permission_maximum = str(
        getattr(settings, "deepspace_sandbox_policy", None) or permission_default
    )
    browser_reason = browser_automation_unavailable_reason(settings)
    computer_reason = computer_use_unavailable_reason(settings)
    computer_operations = {
        spec.name: {
            "available": (reason := computer_use_unavailable_reason(settings, spec.name)) is None,
            "reason": reason,
        }
        for spec in COMPUTER_USE_TOOLS
    }
    try:
        activity = runtime_activity_status(db=db, auth=_auth, settings=settings)
    except Exception:  # noqa: BLE001 - preserve settings even if activity storage is unavailable
        activity = {
            "status": "unavailable",
            "reason": "Live agent activity could not be read from the runtime store.",
        }
    isolated_enabled = bool(getattr(settings, "deepspace_sandbox_enabled", False))
    isolated_endpoint = bool(
        str(getattr(settings, "deepspace_sandbox_url", "") or "").strip()
        and str(getattr(settings, "deepspace_sandbox_token", "") or "").strip()
    )
    sandbox_report = sandbox_status(settings)
    local_sandbox_ready = (
        bool(sandbox_report.get("available")) and sandbox_report.get("enforcement") == "full"
    )
    return {
        "contract_version": 4,
        "observed_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "activity": activity,
        "sandbox": sandbox_report,
        "isolated_sandbox": {
            "enabled": isolated_enabled,
            "configured": isolated_endpoint or local_sandbox_ready,
            "executor": "remote" if isolated_endpoint else "local" if local_sandbox_ready else None,
            "available": isolated_enabled and (isolated_endpoint or local_sandbox_ready),
            "languages": ["python", "sql", "bash", "pwsh"],
        },
        "approval_mode": str(getattr(settings, "deepspace_approval_mode", "never")),
        "permission_mode": permission_maximum,
        "permission_default": permission_default,
        "permission_maximum": permission_maximum,
        "restricted_modes_fail_closed": True,
        "shell_enabled": bool(getattr(settings, "deepspace_shell_enabled", True)),
        "terminal_enabled": bool(getattr(settings, "deepspace_terminal_enabled", True)),
        "max_concurrent_tools": int(getattr(settings, "deepspace_max_concurrent_tools", 8)),
        "spill_threshold_chars": int(getattr(settings, "deepspace_spill_threshold_chars", 24_000)),
        "ptc_enabled": bool(getattr(settings, "deepspace_ptc_enabled", True)),
        "plan_mode_enabled": bool(getattr(settings, "deepspace_plan_mode_enabled", True)),
        "acp_enabled": bool(getattr(settings, "deepspace_acp_enabled", False)),
        "model_selection_mode": str(getattr(settings, "deepspace_model_selection_mode", "auto")),
        "model_auto_choice_enabled": bool(
            getattr(settings, "deepspace_model_auto_choice_enabled", True)
        ),
        "agent_team_enabled": bool(getattr(settings, "deepspace_agent_team_enabled", True)),
        "ssh_runtime_enabled": bool(getattr(settings, "deepspace_ssh_runtime_enabled", True)),
        "browser_automation": {
            "enabled": bool(getattr(settings, "deepspace_browser_automation_enabled", True)),
            "available": browser_reason is None,
            "browser_type": str(
                getattr(settings, "deepspace_browser_automation_browser_type", "chromium")
            ),
            "reason": browser_reason,
        },
        "computer_use": {
            "enabled": bool(getattr(settings, "deepspace_computer_use_enabled", False)),
            "available": computer_reason is None,
            "provider": str(getattr(settings, "deepspace_computer_use_provider", "local")),
            "reason": computer_reason,
            "operations": computer_operations,
        },
    }
