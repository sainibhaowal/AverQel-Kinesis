"""Named permission presets and auto-review for DeepSpace.

Provides named bundles of sandbox + approval settings, and optional LLM auto-review.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PermissionPresetsConfig:
    """Permission presets configuration."""

    enabled: bool = True
    auto_review_enabled: bool = False
    auto_review_provider: str | None = None
    auto_review_model: str | None = None
    auto_review_prompt: str = (
        "Review this tool call for safety. The user has granted auto-review permission. "
        "Respond with 'approve' or 'deny' and a brief reason."
    )


# ──────────────────────────────────────────────────────────────────────
# Preset Definitions
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PermissionPreset:
    """A named permission preset."""

    id: str
    name: str
    description: str
    sandbox_mode: Literal["read-only", "workspace-write", "danger-full-access"]
    approval_mode: Literal["auto", "ask", "deny"]
    network_allowed: bool = False
    auto_review: bool = False
    tools_allowlist: tuple[str, ...] = ()
    tools_denylist: tuple[str, ...] = ()
    max_concurrent_tools: int = 16
    spill_threshold_chars: int = 100_000


# Built-in presets
BUILTIN_PERMISSION_PRESETS: tuple[PermissionPreset, ...] = (
    PermissionPreset(
        id="strict",
        name="Strict",
        description="Read-only access, all tool calls require approval",
        sandbox_mode="read-only",
        approval_mode="ask",
        network_allowed=False,
    ),
    PermissionPreset(
        id="standard",
        name="Standard",
        description="Workspace write access, approval for sensitive operations",
        sandbox_mode="workspace-write",
        approval_mode="ask",
        network_allowed=True,
    ),
    PermissionPreset(
        id="permissive",
        name="Permissive",
        description="Full workspace access, auto-approval for known-safe tools",
        sandbox_mode="workspace-write",
        approval_mode="auto",
        network_allowed=True,
        auto_review=True,
    ),
    PermissionPreset(
        id="danger",
        name="Dangerous",
        description="Full system access, no approvals (use with extreme caution)",
        sandbox_mode="danger-full-access",
        approval_mode="auto",
        network_allowed=True,
        auto_review=True,
    ),
    PermissionPreset(
        id="coding",
        name="Coding Assistant",
        description="Optimized for code tasks with terminal and file access",
        sandbox_mode="workspace-write",
        approval_mode="ask",
        network_allowed=True,
        tools_allowlist=(
            "fs_read",
            "fs_write",
            "fs_edit",
            "fs_glob",
            "fs_grep",
            "terminal",
            "bash",
            "lsp",
            "workspace_diff",
        ),
    ),
    PermissionPreset(
        id="research",
        name="Research",
        description="Web browsing and document analysis focused",
        sandbox_mode="read-only",
        approval_mode="ask",
        network_allowed=True,
        tools_allowlist=(
            "web_fetch",
            "web_search",
            "read_mcp_resource",
            "list_mcp_resources",
            "fs_read",
            "fs_glob",
            "browser_navigate",
            "browser_click",
            "browser_type",
            "browser_fill",
            "browser_select",
            "browser_hover",
            "browser_wait",
            "browser_screenshot",
            "browser_evaluate",
            "browser_get_content",
            "browser_pdf",
            "browser_new_tab",
            "browser_switch_tab",
            "browser_close_tab",
            "browser_download",
            "browser_drag_drop",
        ),
    ),
)


# ──────────────────────────────────────────────────────────────────────
# Auto-Review Engine
# ──────────────────────────────────────────────────────────────────────

#: An async ``(system_prompt, user_prompt) -> answer`` model call.
ReviewCallable = Callable[[str, str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """The outcome of one auto-review.

    ``reviewed_by`` records whether a fixed policy decided the call or a
    model answered, so an approval cannot be mistaken for a human decision.
    """

    approved: bool
    reason: str
    reviewed_by: Literal["policy", "model", "unavailable"]


class AutoReviewEngine:
    """Decide ordinary tool approvals with policy rules or one model call.

    The engine never invents an approval: a missing reviewer, a timeout, an
    exception, or an unparseable answer all deny the call so the normal human
    approval path still runs.
    """

    #: Read-only tools whose risk is already bounded by the tool itself.
    SAFE_TOOLS = frozenset(
        {
            "file_reference",
            "fs_glob",
            "fs_grep",
            "fs_read",
            "lsp",
            "read_mcp_resource",
            "session_reference",
            "session_search",
            "session_trace",
            "web_fetch",
            "web_search",
            "browser_get_content",
            "browser_navigate",
            "browser_screenshot",
            "browser_hover",
            "browser_wait",
            "browser_new_tab",
            "browser_switch_tab",
            "computer_screenshot",
            "computer_wait",
        }
    )

    def __init__(
        self,
        settings: Settings,
        reviewer: ReviewCallable | None = None,
    ):
        self.settings = settings
        self._reviewer = reviewer
        self.enabled = bool(getattr(settings, "deepspace_auto_review_enabled", False))
        self.prompt = str(
            getattr(
                settings,
                "deepspace_auto_review_prompt",
                "Review this tool call for safety. Respond with 'approve' or 'deny' "
                "and a brief reason.",
            )
        )
        self.timeout_s = max(
            1.0, int(getattr(settings, "deepspace_auto_review_timeout_ms", 20_000)) / 1000
        )

    async def review(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ReviewDecision:
        """Decide whether one tool call may run without asking the user.

        Args:
            tool_name: The native or MCP tool name.
            arguments: The validated tool arguments.
            context: Small, non-secret turn facts for the reviewer.

        Returns:
            The decision and the source that made it.
        """
        if not self.enabled:
            return ReviewDecision(False, "Auto-review is not enabled.", "unavailable")
        if tool_name in self.SAFE_TOOLS:
            return ReviewDecision(True, "The tool is read-only and allowlisted.", "policy")
        if self._reviewer is None:
            return ReviewDecision(
                False,
                "Auto-review is enabled but no model reviewer is available.",
                "unavailable",
            )
        try:
            answer = await asyncio.wait_for(
                self._reviewer(
                    self.prompt, self._build_review_prompt(tool_name, arguments, context)
                ),
                timeout=self.timeout_s,
            )
        except TimeoutError:
            return ReviewDecision(
                False,
                f"Auto-review did not answer within {self.timeout_s:.0f}s.",
                "unavailable",
            )
        except Exception:  # noqa: BLE001 - a review failure must not approve
            logger.warning("Auto-review model call failed", exc_info=True)
            return ReviewDecision(False, "Auto-review model call failed.", "unavailable")
        return self._parse(answer)

    @staticmethod
    def _parse(answer: str) -> ReviewDecision:
        """Parse ``approve``/``deny`` and a reason from a model answer.

        Unrecognised answers deny: an approval must be explicit.
        """
        normalized = answer.strip()
        if not normalized:
            return ReviewDecision(False, "Auto-review returned an empty answer.", "model")
        verdict, _, reason = normalized.partition(" ")
        verdict = verdict.strip(" :.\n\t").lower()
        reason = reason.strip() or "No reason given."
        if verdict.startswith("approve"):
            return ReviewDecision(True, reason, "model")
        if verdict.startswith("deny"):
            return ReviewDecision(False, reason, "model")
        return ReviewDecision(
            False,
            f"Auto-review did not answer approve or deny: {normalized[:200]}",
            "model",
        )

    @staticmethod
    def _build_review_prompt(
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        """Build the per-call reviewer prompt."""
        serialized = json.dumps(arguments, indent=2, default=str)
        if len(serialized) > 4_000:
            serialized = serialized[:4_000] + "\n... (truncated)"
        facts = json.dumps(context, default=str)[:500]
        return (
            f"Tool: {tool_name}\n"
            f"Arguments:\n{serialized}\n\n"
            f"Turn context: {facts}\n\n"
            "Answer with 'approve <reason>' or 'deny <reason>'."
        )


# ──────────────────────────────────────────────────────────────────────
# Permission Preset Service
# ──────────────────────────────────────────────────────────────────────


class PermissionPresetService:
    """Manages permission presets and auto-review."""

    def __init__(
        self,
        settings: Settings,
        reviewer: ReviewCallable | None = None,
    ):
        self.settings = settings
        self._presets: dict[str, PermissionPreset] = {
            preset.id: preset for preset in BUILTIN_PERMISSION_PRESETS
        }
        self._custom_presets: dict[str, PermissionPreset] = {}
        self.auto_review = AutoReviewEngine(settings, reviewer)

    def list_presets(self) -> list[PermissionPreset]:
        """List all available presets."""
        return list(self._presets.values()) + list(self._custom_presets.values())

    def get_preset(self, preset_id: str) -> PermissionPreset | None:
        """Get a preset by ID."""
        return self._presets.get(preset_id) or self._custom_presets.get(preset_id)

    def add_custom_preset(self, preset: PermissionPreset) -> None:
        """Add a custom preset."""
        self._custom_presets[preset.id] = preset

    def remove_custom_preset(self, preset_id: str) -> bool:
        """Remove a custom preset."""
        if preset_id in self._custom_presets:
            del self._custom_presets[preset_id]
            return True
        return False

    def get_effective_settings(self, preset_id: str) -> dict[str, Any] | None:
        """Get effective settings for a preset."""
        preset = self.get_preset(preset_id)
        if not preset:
            return None
        return {
            "sandbox_mode": preset.sandbox_mode,
            "approval_mode": preset.approval_mode,
            "network_allowed": preset.network_allowed,
            "auto_review": preset.auto_review,
            "tools_allowlist": list(preset.tools_allowlist),
            "tools_denylist": list(preset.tools_denylist),
            "max_concurrent_tools": preset.max_concurrent_tools,
            "spill_threshold_chars": preset.spill_threshold_chars,
        }

    async def auto_review_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[bool, str]:
        """Auto-review a tool call."""
        return await self.auto_review.review(tool_name, arguments, context)


# Global service
_PERMISSION_SERVICE: PermissionPresetService | None = None


def get_permission_preset_service(settings: Settings | None = None) -> PermissionPresetService:
    """Get or create the global permission preset service."""
    global _PERMISSION_SERVICE
    if _PERMISSION_SERVICE is None:
        _PERMISSION_SERVICE = PermissionPresetService(settings or get_settings())
    return _PERMISSION_SERVICE


def init_permission_preset_service(settings: Settings) -> PermissionPresetService:
    """Initialize the permission preset service."""
    global _PERMISSION_SERVICE
    _PERMISSION_SERVICE = PermissionPresetService(settings)
    return _PERMISSION_SERVICE


__all__ = [
    "AutoReviewEngine",
    "ReviewDecision",
    "PermissionPresetsConfig",
    "PermissionPreset",
    "BUILTIN_PERMISSION_PRESETS",
    "PermissionPresetService",
    "get_permission_preset_service",
    "init_permission_preset_service",
]
