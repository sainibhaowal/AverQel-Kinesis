from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.deepspace.services.host_access import (
    blocked_shell_command_reason,
    find_mutation_has_relative_root,
    is_known_read_only_command,
    resolve_conversation_path,
    tool_confirmation_warning,
    tool_requires_human_confirmation,
)
from app.deepspace.services.mcp_tool_broker import MCP_BROKER_TOOL_NAMES
from app.deepspace.services.tools import NATIVE_TOOL_MODES
from app.deepspace.services.tools._base import ToolMode as NativeToolMode
from app.deepspace.services.tools.shell import read_only_violation
from app.deepspace.services.workspace_root import WorkspacePathError

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
    approval_warning: str | None = None


#: Native writes that ``ask`` mode pauses while permission is restricted.
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

# A read-only conversation may inspect data through these tools. Shell and
# terminal commands still run in the read-only operating-system sandbox; the
# tool's transport being interactive does not grant filesystem write access.
READ_ONLY_EXECUTION_TOOLS = frozenset(
    {
        "bash",
        "pwsh",
        "terminal_open",
        "terminal_write",
        "terminal_signal",
        "terminal_close",
    }
)

# These tools can act on the whole desktop, a remote machine, or a remote
# website. Their mutations are outside the selected local workspace.
FULL_ACCESS_WRITE_PREFIXES = ("browser_", "computer_", "ssh_")


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
            approval_mode: ``ask`` pauses writes in restricted permission
                modes; Full access still uses its risk-based review rules.
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
        self._workspace_root: Path | None = None

    def set_workspace_root(self, root: Path | None) -> None:
        """Set the workspace boundary used for path-level policy checks."""
        self._workspace_root = root

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
        if tool_name in {
            "bash",
            "pwsh",
            "terminal_open",
            "terminal_write",
            "ssh_run",
            "ssh_terminal_write",
        }:
            command = str(arguments.get("command") or arguments.get("data") or "")
            command_cwd: Path | None = None
            if tool_name in {"bash", "pwsh", "terminal_open", "terminal_write"}:
                raw_cwd = str(arguments.get("cwd") or "").strip()
                if raw_cwd:
                    command_cwd = Path(raw_cwd).expanduser()
                    if not command_cwd.is_absolute() and self._workspace_root is not None:
                        command_cwd = self._workspace_root / command_cwd
                else:
                    command_cwd = self._workspace_root
            blocked_reason = blocked_shell_command_reason(command, cwd=command_cwd)
            if blocked_reason:
                return ToolPolicyDecision(False, mode=mode, reason=blocked_reason)
            if tool_name == "terminal_write" and find_mutation_has_relative_root(command):
                return ToolPolicyDecision(
                    False,
                    mode=mode,
                    reason=(
                        "DeepSpace blocked this destructive find command because a persistent "
                        "terminal's current directory may have changed. Run the search through "
                        "bash with an explicit absolute path so its target can be checked."
                    ),
                )
        if (
            self._permission_mode != "danger-full-access"
            and self._workspace_root is not None
            and tool_name
            in {
                "fs_read",
                "fs_write",
                "fs_edit",
                "fs_delete",
                "fs_str_replace",
                "fs_grep",
                "fs_glob",
            }
        ):
            raw_path = arguments.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                try:
                    resolve_conversation_path(
                        self._workspace_root,
                        raw_path,
                        self._permission_mode,
                    )
                except WorkspacePathError:
                    return ToolPolicyDecision(
                        False,
                        mode=mode,
                        reason=(
                            "That path is outside the selected workspace. Switch to Full access "
                            "before asking DeepSpace to use it."
                        ),
                    )
        safe_read_only_ssh_run = (
            self._permission_mode == "read-only"
            and tool_name == "ssh_run"
            and is_known_read_only_command(str(arguments.get("command") or ""))
            and not blocked_shell_command_reason(str(arguments.get("command") or ""))
        )
        if self._permission_mode == "read-only" and tool_name in READ_ONLY_EXECUTION_TOOLS:
            command = str(arguments.get("command") or arguments.get("data") or "")
            violation = read_only_violation(command)
            if violation is not None:
                return ToolPolicyDecision(
                    False,
                    mode=mode,
                    reason=(
                        f"The '{violation}' command can change data. The read-only mode "
                        "blocked it; switch to Workspace to make changes."
                    ),
                )
        if (
            self._permission_mode != "danger-full-access"
            and mode == "write"
            and tool_name.startswith(FULL_ACCESS_WRITE_PREFIXES)
            and not safe_read_only_ssh_run
        ):
            return ToolPolicyDecision(
                False,
                mode=mode,
                reason=(
                    f"The {tool_name!r} action reaches a browser, computer, or remote host "
                    "outside the selected workspace. Switch to Full access before asking "
                    "DeepSpace to perform it."
                ),
            )
        if self._permission_mode == "read-only" and mode == "write":
            if tool_name not in READ_ONLY_EXECUTION_TOOLS and not safe_read_only_ssh_run:
                return ToolPolicyDecision(
                    False,
                    mode=mode,
                    reason=(
                        "This conversation is in read-only mode. The requested tool can change "
                        "data, so DeepSpace did not run it. Switch to Workspace to make changes."
                    ),
                )
        mandatory_confirmation = tool_requires_human_confirmation(
            tool_name,
            arguments,
            permission_mode=self._permission_mode,
            workspace_root=self._workspace_root,
        )
        safe_shell_read = (
            tool_name in {"bash", "pwsh"}
            and is_known_read_only_command(str(arguments.get("command") or ""))
            and not mandatory_confirmation
        )
        destructive = tool_name in APPROVAL_REQUIRED_TOOLS and not safe_shell_read
        if (
            self._permission_mode == "danger-full-access"
            and tool_name in self._plugin_modes
            and mode != "read"
        ):
            # A write-capable plugin handler is arbitrary code. Read-only
            # plugin calls do not need a prompt merely because Full access is
            # selected; mutation calls retain the explicit review gate.
            mandatory_confirmation = True
        ask_gated = (
            self._approval_mode == "ask"
            and destructive
            and self._permission_mode != "danger-full-access"
        )
        # Full Access is the user's authorization for routine scoped native
        # writes. The risk classifier still asks about deletes, moves, system
        # changes, and opaque write handlers, and hard-blocks disk/security
        # operations before reaching this approval decision.
        requires_approval = ask_gated or mandatory_confirmation
        if tool_name in self._approval_tools:
            requires_approval = True
        return ToolPolicyDecision(
            True,
            mode=mode,
            requires_approval=requires_approval,
            risk_level=(
                "critical"
                if mandatory_confirmation
                else (
                    "read"
                    if safe_shell_read
                    else "high" if ask_gated else "medium" if mode == "write" else "read"
                )
            ),
            approval_warning=(
                tool_confirmation_warning(
                    tool_name,
                    arguments,
                    permission_mode=self._permission_mode,
                    workspace_root=self._workspace_root,
                )
                if requires_approval
                else None
            ),
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
