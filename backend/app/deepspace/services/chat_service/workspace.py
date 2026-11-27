"""Workspace mixin for DeepSpaceChatService."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from app.auth.dependencies import AuthContext
from app.deepspace.services.chat_service.utils import (
    _TurnState,
)
from app.deepspace.services.presets import (
    PresetResolution,
    strictest_sandbox_mode,
)
from app.deepspace.services.tools.workflow import WORKFLOW_SYSTEM_GUIDANCE
from app.deepspace.services.workspace_root import (
    WorkspaceUnavailableError,
    workspace_root_for,
)
from app.deepspace.services.workspace_snapshot import (
    collect_snapshot_files,
    collect_workspace_stat_entries,
    snapshot_store,
)

logger = logging.getLogger(__name__)


class _WorkspaceMixin:
    @staticmethod
    def _is_non_work_greeting(prompt: str) -> bool:
        """Keep social greetings from accidentally resuming an old task plan."""

        normalized = re.sub(r"[^a-z]+", " ", prompt.casefold()).strip()
        return normalized in {
            "hi",
            "hi there",
            "hello",
            "hello there",
            "hey",
            "hey there",
            "good morning",
            "good afternoon",
            "good evening",
            "thanks",
            "thank you",
        }

    @staticmethod
    def _looks_like_clarification_request(text: str) -> bool:
        """Recognize a model clarification that should use ``ask_user``.

        This deliberately requires both a question mark and explicit
        clarification language, so ordinary answers containing questions or
        rhetorical prose are not converted into an interactive pause.
        """
        normalized = " ".join(text.lower().split())
        if "?" not in normalized or len(normalized) < 20:
            return False
        markers = (
            "could you clarify",
            "please clarify",
            "what would you like",
            "what are you trying to",
            "which action",
            "which one would you like",
            "please provide",
            "what do you mean",
            "are you asking",
            "i'm not sure what you mean",
            "your request is unclear",
            "request is unclear",
        )
        return any(marker in normalized for marker in markers)

    def _workflow_guidance(self, state: _TurnState) -> str | None:
        """Read the workflow guidance this turn's tools make relevant.

        The workflow tool's own description carries the hook contract.  The
        guidance says when to reach for the tool, which a description
        cannot say, so the model does not guess a script's shape.  A turn
        that cannot call the workflow tool receives no guidance.

        Args:
            state: The turn's state.

        Returns:
            The guidance, or None when the workflow tool is not available.
        """
        if "workflow_run" not in (state.native_tool_names or set()):
            return None
        return WORKFLOW_SYSTEM_GUIDANCE

    def _sandbox_policy_instruction(self, state: _TurnState) -> str:
        """Describe the boundary the turn runs in.

        The model plans inside the boundary it has, so it can choose a
        read-only approach when that is all the conversation allows.

        Args:
            state: The turn's state.

        Returns:
            The policy instruction.
        """
        mode = str(state.sandbox_mode or "")
        workspace = ""
        try:
            root = getattr(state, "conversation_workspace_root", None)
            if root is None:
                root = workspace_root_for(self.settings, state.auth.tenant_id, state.auth.user_id)
            workspace = str(root)
        except Exception:  # noqa: BLE001
            workspace = ""
        if mode == "read-only":
            effect = (
                "This conversation may inspect files and run read-only commands in the selected "
                "workspace. The operating-system sandbox blocks command writes. File edits, "
                "deletions, and other changes are refused. If a change is requested, explain "
                "that the user must switch to Workspace mode before doing it."
            )
        elif mode == "danger-full-access":
            effect = (
                "This conversation may access host paths the backend's local operating-system "
                "user can access, through the supplied AverQel tools. When locating a local file, "
                "use glob for the filename and grep only for file contents. For one exact "
                "filename with no path, one glob call searches the conversation workspace and "
                "the current user's home, then /mnt and /media as the bounded search budget allows; "
                "it stops at the first match. For wildcard searches or a user-named directory, use "
                "that one explicit path. Do not use bash, find, grep, sandbox_execute, or alternate "
                "spellings to repeat a filename search. Every search is capped by a filesystem-entry "
                "budget. Read search_roots, scanned_entries, search_truncated, and skipped_paths; "
                "if a path was denied or the search was cut short, explain that limitation instead "
                "of claiming the file does not exist. Never start an ordinary file "
                "search at /; use path='/' with entire_system=true only when the user explicitly "
                "requests an entire-system search. "
                "The file search skips protected virtual/system trees such as /proc, /sys, /dev, "
                "and /run, and reports skipped paths. "
                "A wildcard search with no path searches only the selected workspace. Never claim "
                "a host search was performed unless a tool result confirms its path and result. Full "
                "access does not grant root privileges or bypass operating-system permissions. "
                "Destructive actions and non-inspection shell commands pause for explicit human "
                "approval; commands that can erase disks, elevate privileges, or shut down the "
                "host are blocked. Use the smallest change that completes the request, and do "
                "not claim host-level access is risk-free."
            )
        else:
            effect = (
                "This conversation may read and change files only inside the selected project "
                "workspace. The operating-system sandbox enforces that boundary. If a requested "
                "path or operation is outside it, explain that the user must switch to Full "
                "access. Destructive operations pause for explicit approval."
            )
        lines = [f"Sandbox mode: {mode}.", effect]
        if workspace:
            lines.append(f"The workspace you may write to is {workspace}.")
        return " ".join(lines)

    def _resolve_sandbox_mode(self, state: _TurnState, resolution: PresetResolution) -> str:
        """Resolve the sandbox mode one turn runs in.

        Three modes apply: the one the conversation's preset names, the one
        the conversation recorded, and the one the deployment allows.  The
        strictest of them is the mode the turn runs in, so a preset may only
        narrow what the deployment allows and a conversation cannot widen
        the boundary.

        Args:
            state: The turn's state.
            resolution: The preset the turn runs with.

        Returns:
            The effective mode.
        """
        deployment_default = str(
            getattr(self.settings, "deepspace_permission_mode", "workspace-write")
        )
        deployment_maximum = str(
            getattr(self.settings, "deepspace_sandbox_policy", None) or deployment_default
        )
        recorded: str | None = None
        if state.conversation_id is not None:
            try:
                recorded = self.sandbox_mode.recorded_mode(
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                )
            except Exception:  # noqa: BLE001
                # A sandbox-mode read that fails must not widen the boundary,
                # so the deployment's own mode stands.
                logger.warning("DeepSpace sandbox mode could not be read", exc_info=True)
        requested = recorded or deployment_default
        mode = (
            strictest_sandbox_mode(
                resolution.preset.sandbox_mode,
                requested,
                deployment_maximum,
            )
            or deployment_default
        )
        self.tool_policy.set_permission_mode(mode)
        return mode

    def conversation_workspace_root(
        self, conversation_id: uuid.UUID | None, auth: AuthContext
    ) -> Path | None:
        """Read the workspace root one conversation works in.

        A background run reads the same workspace the conversation chose, so
        a worker can give its run the same workspace the foreground call had.

        Args:
            conversation_id: The conversation the call belongs to.
            auth: The authenticated tenant and user.

        Returns:
            The workspace root, or None when the default workspace stands.
        """
        return self._conversation_workspace_root(conversation_id, auth)

    def _conversation_workspace_root(
        self, conversation_id: uuid.UUID | None, auth: AuthContext
    ) -> Path | None:
        """Read the workspace root one conversation works in.

        The workspace a conversation selected is a per-conversation state, so
        the conversation's own choice decides and the account's default
        workspace is only the default.  A selection that cannot be read or
        resolved leaves the tools on the default workspace rather than on a
        workspace nobody chose.

        Args:
            conversation_id: The conversation the call belongs to.
            auth: The authenticated tenant and user.

        Returns:
            The workspace root, or None when the default workspace stands.
        """
        if getattr(self, "db", None) is None or conversation_id is None:
            return None
        try:
            workspace_id = self.workspace_selection.effective_workspace(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("The conversation's workspace could not be read", exc_info=True)
            return None
        try:
            return workspace_root_for(self.settings, auth.tenant_id, auth.user_id, workspace_id)
        except WorkspaceUnavailableError:
            logger.warning("The conversation's workspace could not be resolved", exc_info=True)
            return None

    def _conversation_sandbox_mode(
        self, conversation_id: uuid.UUID, auth: AuthContext
    ) -> str | None:
        """Read the sandbox mode one conversation runs in.

        The mode is the strictest of the preset the conversation runs, the
        mode the conversation recorded, and the mode the deployment allows.

        Args:
            conversation_id: The conversation the call belongs to.
            auth: The authenticated tenant and user.

        Returns:
            The conversation's mode, or None when the deployment's own mode
            stands.
        """
        deployment_default = str(
            getattr(self.settings, "deepspace_permission_mode", "workspace-write")
        )
        deployment_maximum = str(
            getattr(self.settings, "deepspace_sandbox_policy", None) or deployment_default
        )
        recorded: str | None = None
        try:
            recorded = self.sandbox_mode.recorded_mode(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("DeepSpace sandbox mode could not be read", exc_info=True)
        preset_mode: str | None = None
        resolution = getattr(self, "_preset_resolution", None)
        if isinstance(resolution, PresetResolution):
            preset_mode = resolution.preset.sandbox_mode
        return (
            strictest_sandbox_mode(
                preset_mode,
                recorded or deployment_default,
                deployment_maximum,
            )
            or deployment_default
        )

    def _resolve_workspace_root(self, state: _TurnState) -> Path | None:
        """Resolve the workspace root one turn works in.

        The turn resolves the workspace once, the way it resolves the
        sandbox mode, so the tools, the workspace baseline, and the
        instructions the model reads all name the same directory.

        Args:
            state: The turn's state.

        Returns:
            The workspace root, or None when the default workspace stands.
        """
        root = self._conversation_workspace_root(state.conversation_id, state.auth)
        state.conversation_workspace_root = root
        return root

    def _record_workspace_baseline(self, state: _TurnState) -> None:
        """Record the workspace as it is before this turn's work.

        The diff tool compares the workspace against this baseline, so the
        agent can see what its own turn changed.  The baseline covers the
        workspace the conversation works in, so the diff of a conversation
        that works in a named workspace compares that workspace.

        Args:
            state: The turn's state.
        """
        settings = getattr(self, "settings", None)
        if settings is None:
            return
        try:
            root = self._resolve_workspace_root(state)
            if root is None:
                root = workspace_root_for(settings, state.auth.tenant_id, state.auth.user_id)
            files = collect_snapshot_files(root)
            snapshot_store().save(
                tenant_id=state.auth.tenant_id,
                user_id=state.auth.user_id,
                root=str(root),
                files=files,
            )
            state.workspace_files_before = list(files)
            state.workspace_stat_before = collect_workspace_stat_entries(root)
            state.workspace_root = root
        except Exception:  # noqa: BLE001
            logger.debug("The workspace baseline could not be recorded.", exc_info=True)
