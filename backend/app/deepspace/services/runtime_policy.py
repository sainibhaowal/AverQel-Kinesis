from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.deepspace.services.mcp_tool_broker import MCP_BROKER_TOOL_NAMES
from app.deepspace.services.tools import NATIVE_TOOL_MODES
from app.deepspace.services.tools._base import ToolMode as NativeToolMode

ToolMode = Literal["read", "write", "interactive"]


def native_tool_modes() -> dict[str, NativeToolMode]:
    """Return the execution mode of every tool the tool package owns.

    The tool modules declare their own modes, so the policy allowlist and the
    tool declarations are read from one place.
    """
    return dict(NATIVE_TOOL_MODES)


@dataclass(frozen=True, slots=True)
class ToolPolicyDecision:
    allowed: bool
    mode: ToolMode | None = None
    reason: str | None = None
    requires_approval: bool = False
    risk_level: str = "read"


#: Native tools whose effect is hard to undo, so ``ask`` mode pauses them.
APPROVAL_REQUIRED_TOOLS = frozenset(
    {
        "write",
        "edit",
        "delete",
        "sandbox_execute",
        "fs_write",
        "fs_edit",
        "fs_delete",
        "fs_str_replace",
        "bash",
        "pwsh",
    }
)


class DeepSpaceToolPolicy:
    """The allowlist, permission preset, and approval policy for native tools."""

    _MODES: dict[str, ToolMode] = {
        "discover_tools": "read",
        "get_tool_result": "read",
        "web_search": "read",
        "url_read": "read",
        "image_read": "read",
        "analyze": "read",
        "read": "read",
        "find": "read",
        "todo_write": "write",
        "write": "write",
        "edit": "write",
        "delete": "write",
        "sandbox_execute": "write",
        "artifact_create": "write",
        "ask_user": "interactive",
        # The file, web, shell, delegation, and workflow tools.  Each one
        # declares its own mode in the module that owns it, so the allowlist
        # and the tool's declaration cannot drift apart.
        **native_tool_modes(),
        # The model-facing MCP broker resolves its operations to these private
        # names before execution, so policy must recognize them too.
        **{name: "read" for name in MCP_BROKER_TOOL_NAMES},
    }

    def __init__(
        self,
        *,
        approval_mode: str = "never",
        permission_mode: str = "workspace-write",
        denied_tools: frozenset[str] | set[str] | None = None,
        approval_tools: frozenset[str] | set[str] | None = None,
        plugin_modes: Mapping[str, ToolMode] | None = None,
    ) -> None:
        """Create the policy for one deployment.

        Args:
            approval_mode: ``ask`` pauses a destructive native tool until the
                user answers; ``never`` runs it unattended.
            permission_mode: ``read-only`` refuses every write tool;
                ``workspace-write`` and ``danger-full-access`` allow them.
            denied_tools: Tools an extension refuses.  A denied tool stays
                refused even when the deployment would otherwise allow it.
            approval_tools: Tools an extension always asks about, whatever
                the deployment's approval mode is.
            plugin_modes: The execution modes of the tools the enabled
                plugins offer.  A plugin's tool is a native tool for the
                policy, so it is judged by the same rules.
        """
        self._approval_mode = approval_mode
        self._permission_mode = permission_mode
        self._denied_tools = frozenset(denied_tools or ())
        self._approval_tools = frozenset(approval_tools or ())
        self._plugin_modes: dict[str, ToolMode] = dict(plugin_modes or {})
        self._mcp_tool_names: frozenset[str] = frozenset()
        self._allowed_tools: frozenset[str] | None = None

    def set_mcp_tool_names(self, names: set[str] | frozenset[str]) -> None:
        """Register the conversation-scoped MCP names for pipeline checks.

        Args:
            names: Namespaced MCP tools discovered for the current turn.
        """
        self._mcp_tool_names = frozenset(str(name) for name in names)

    def set_plugin_rules(
        self,
        *,
        modes: Mapping[str, ToolMode] | None = None,
        denied_tools: frozenset[str] | set[str] | None = None,
    ) -> None:
        """Apply the tools the enabled plugins offer and refuse.

        The rules are replaced on every turn, so a plugin that is switched
        off stops being callable on the next turn.

        Args:
            modes: The execution mode of each enabled plugin's tool.
            denied_tools: The tools the enabled plugins refuse.
        """
        self._plugin_modes = dict(modes or {})
        if denied_tools:
            self._denied_tools = frozenset(denied_tools)

    def set_preset_rules(self, allowed_tools: frozenset[str] | set[str] | None) -> None:
        """Apply the tool set the conversation's preset allows.

        The rules are replaced on every turn, so a preset that is changed
        takes effect on the next turn.

        Args:
            allowed_tools: The tools the preset allows. An empty set means
                the preset does not restrict the tools.
        """
        self._allowed_tools = frozenset(allowed_tools) if allowed_tools else None

    def mode_of(self, tool_name: str) -> ToolMode | None:
        """Return the execution mode of one tool.

        Args:
            tool_name: The tool's name.

        Returns:
            The mode, or None when the tool is not in the allowlist.
        """
        mode = self._plugin_modes.get(tool_name)
        if mode is not None:
            return mode
        if tool_name in self._mcp_tool_names:
            return "read"
        return self._MODES.get(tool_name)

    def set_extension_rules(
        self,
        *,
        denied_tools: frozenset[str] | set[str] | None = None,
        approval_tools: frozenset[str] | set[str] | None = None,
    ) -> None:
        """Apply the tool restrictions the conversation's extensions declare.

        The rules are replaced on every turn, so removing an extension
        removes its restrictions on the next turn.

        Args:
            denied_tools: Tools an extension refuses.
            approval_tools: Tools an extension always asks about.
        """
        self._denied_tools = frozenset(denied_tools or ())
        self._approval_tools = frozenset(approval_tools or ())

    def set_permission_mode(self, mode: str) -> None:
        """Apply the permission mode this conversation runs in.

        The mode is replaced on every turn, so the conversation's own mode
        decides and the deployment's mode is only the default.

        Args:
            mode: The permission mode for this conversation.
        """
        normalized = str(mode or "").strip().lower()
        if normalized:
            self._permission_mode = normalized

    def decide(self, tool_name: str, arguments: dict[str, Any]) -> ToolPolicyDecision:
        if tool_name == "todo_write" and str(arguments.get("operation") or "") in {
            "read",
            "check",
        }:
            return ToolPolicyDecision(True, mode="read")
        mode = self.mode_of(tool_name)
        if mode is None:
            return ToolPolicyDecision(
                False, reason="Tool is not in the DeepSpace productivity allowlist."
            )
        # MCP names are conversation-scoped bindings resolved by the bridge,
        # not static preset entries.  A preset allowlist must not reject an
        # exact binding that the bridge authorized for this conversation.
        if (
            self._allowed_tools is not None
            and tool_name not in self._allowed_tools
            and tool_name not in self._mcp_tool_names
        ):
            return ToolPolicyDecision(
                False,
                mode=mode,
                reason=(
                    f"The tool {tool_name!r} is not part of the agent preset this "
                    "conversation runs."
                ),
            )
        if tool_name in self._denied_tools:
            return ToolPolicyDecision(
                False,
                mode=mode,
                reason=(
                    f"The tool {tool_name!r} is disabled for this conversation by an "
                    "installed extension."
                ),
            )
        if self._permission_mode == "read-only" and mode == "write":
            return ToolPolicyDecision(
                False,
                mode=mode,
                reason="This deployment runs read-only and does not permit write tools.",
            )
        destructive = tool_name in APPROVAL_REQUIRED_TOOLS
        requires_approval = self._approval_mode == "ask" and destructive
        if tool_name in self._approval_tools:
            requires_approval = True
        return ToolPolicyDecision(
            True,
            mode=mode,
            requires_approval=requires_approval,
            risk_level="high" if destructive else ("medium" if mode == "write" else "read"),
        )

    def before_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolPolicyDecision:
        """Hook point for future tenant policy extensions; deny by default outside the allowlist."""

        return self.decide(tool_name, arguments)

    def after_tool(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        """Hook point that prevents internal runtime values from reaching the model/UI."""

        del tool_name
        return {key: value for key, value in result.items() if not key.startswith("_")}

    @classmethod
    def mode(cls, tool_name: str) -> ToolMode | None:
        return cls._MODES.get(tool_name)
