"""Hooks for DeepSpace tool calls.

A hook is a command the deployment owns that takes part in a tool call.  The
runtime asks the command about the call, and the command answers with a
decision: allow the call, block it, or ask about it.  The four event names
follow the Claude Code and Codex convention, so a guard script written for
one of those harnesses can be pointed at DeepSpace.

Four events exist:

* ``PreToolUse`` runs before a tool call and may block it.
* ``PostToolUse`` runs after a tool call and may add context for the model.
* ``Stop`` runs when the agent finishes a turn.
* ``SessionStart`` runs when a conversation starts.

The contract is one JSON object on the hook's standard input and one JSON
object on its standard output.  The input carries the event, the tool name,
the call's arguments, and the conversation the call belongs to.  The output
carries the decision, the reason for it, and any context the hook adds for
the model.

The answer may be written in either of two dialects.  DeepSpace's own dialect
names the action directly:

    {"decision": "allow", "reason": "the path is inside the workspace"}

The Claude Code and Codex dialect names a permission decision inside
``hookSpecificOutput``, and can add a message for the user, context for the
model, and the arguments that replace the call's own:

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "no writes here"}}

That dialect also lets a hook block a call by exiting with status 2 and
writing the reason on standard error, stop the agent with
``{"continue": false, "stopReason": "..."}``, and carry
``{"systemMessage": "..."}``, ``{"additionalContext": "..."}``, and
``{"updatedInput": {...}}``.  A hook that answers in both dialects is
accepted, and the Claude shape wins.

A hook that answers with nothing, answers with something that is not JSON,
exits with a status other than 2, or does not finish in time is logged and
ignored: a broken hook never blocks the agent.

A manifest names the tool a hook watches with a matcher.  A Claude Code
matcher is the tool's own name, and a Codex matcher is a regular expression
over the tool name.  The manifest says which one it is with
``matcher_kind``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, cast

logger = logging.getLogger(__name__)

#: The actions a hook may answer with.
HookAction = Literal["allow", "block", "ask"]

#: The actions a hook may answer with, in the order they are folded.
HOOK_ACTIONS: tuple[HookAction, ...] = ("allow", "block", "ask")

#: The Claude Code and Codex permission decisions, and the action each one
#: means.  ``deny`` is Claude's name for a block, and ``approve`` is the
#: older spelling of an allow.
CLAUDE_PERMISSION_ACTIONS: dict[str, HookAction] = {
    "allow": "allow",
    "approve": "allow",
    "deny": "block",
    "block": "block",
    "ask": "ask",
}

#: How a hook's matcher is read.
MatcherKind = Literal["literal", "regex"]

#: The matcher kinds a manifest may declare.
MATCHER_KINDS: tuple[MatcherKind, ...] = ("literal", "regex")

#: The number of seconds one hook command may take when the manifest names
#: no timeout.
DEFAULT_HOOK_TIMEOUT_SECONDS = 15

#: The longest a hook command may take, whatever the manifest asks for.
MAX_HOOK_TIMEOUT_SECONDS = 600

#: The shortest a hook command may be given, so a manifest cannot make a
#: hook unrunnable.
MIN_HOOK_TIMEOUT_SECONDS = 0.1

#: The environment names a hook command receives.
HOOK_ENVIRONMENT_NAMES = ("PATH", "HOME", "LANG")

#: The marker that tells a hook command it runs inside DeepSpace.
HOOK_ENVIRONMENT_MARKER = "DEEPSPACE_HOOK"

#: The largest number of hooks one manifest may declare.
MAX_HOOKS_PER_MANIFEST = 100

#: The largest number of hooks one registry may hold.
MAX_HOOKS = 100

#: The largest number of characters one hook answer may carry.
MAX_HOOK_ANSWER_CHARS = 64_000

#: The matcher that applies a hook to every tool call.
MATCH_ALL = "*"

#: The exit status a hook uses to block a call, with the reason on its
#: standard error.
HOOK_BLOCK_EXIT_CODE = 2


class HookError(ValueError):
    """A hook manifest, a hook event, or a hook answer was rejected."""


class HookEvent(str, Enum):
    """The moments a hook can take part in.

    The values are the names a manifest declares, and they match the Claude
    Code and Codex event names.
    """

    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SESSION_START = "SessionStart"


def _coerce_action(value: object, *, claude_dialect: bool = True) -> HookAction:
    """Return one action name as an action.

    Args:
        value: The action the hook named.
        claude_dialect: Whether the Claude Code and Codex spellings are
            accepted beside DeepSpace's own.

    Returns:
        The action.

    Raises:
        HookError: When the name is not an action a hook may answer with.
    """
    if not isinstance(value, str):
        raise HookError("A hook's decision must be a string.")
    normalized = value.strip().lower()
    if claude_dialect:
        action = CLAUDE_PERMISSION_ACTIONS.get(normalized)
        if action is not None:
            return action
    for action in HOOK_ACTIONS:
        if normalized == action:
            return action
    raise HookError(f"A hook must answer with allow, block, or ask, not {value!r}.")


def _text_field(value: object, name: str) -> str:
    """Return one text field of a hook's answer.

    Args:
        value: The field's value.
        name: The field's name, for the error message.

    Returns:
        The text, empty when the field is absent.

    Raises:
        HookError: When the field is not text.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HookError(f"A hook's {name} must be a string.")
    return value.strip()


def _first_value(sources: Sequence[tuple[Mapping[str, Any], str]]) -> object | None:
    """Return the value of the first source that names a field.

    Args:
        sources: The mappings and field names to read, in order.

    Returns:
        The value, or None when no source names the field.
    """
    for mapping, name in sources:
        if name in mapping:
            value: object = mapping[name]
            return value
    return None


def _claude_stop(claude: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[bool, str]:
    """Read the stop request a Claude-style hook answered with.

    ``{"continue": false}`` stops the agent, and the optional
    ``stopReason`` says why.  A hook that names only a stop reason stops
    the agent as well.

    Args:
        claude: The hook's ``hookSpecificOutput``.
        payload: The hook's whole answer.

    Returns:
        Whether the agent must stop, and why.

    Raises:
        HookError: When the answer's ``continue`` is not a boolean.
    """
    raw = _first_value(((claude, "continue"), (payload, "continue")))
    if raw is not None and not isinstance(raw, bool):
        raise HookError("A hook's continue must be a boolean.")
    reason = _text_field(
        _first_value(((claude, "stopReason"), (payload, "stopReason"))),
        "stopReason",
    )
    return raw is False or bool(reason), reason


def _coerce_event(value: HookEvent | str) -> HookEvent:
    """Return one event name as an event.

    Args:
        value: The event the caller named.

    Returns:
        The event.

    Raises:
        HookError: When the name is not an event a hook may run for.
    """
    if isinstance(value, HookEvent):
        return value
    normalized = str(value).strip().lower()
    for event in HookEvent:
        if event.value.lower() == normalized:
            return event
    raise HookError(f"Unknown hook event: {value!r}.")


@dataclass(frozen=True, slots=True)
class HookDecision:
    """What one hook decided about a call.

    Attributes:
        action: The decision: ``allow``, ``block``, or ``ask``.
        reason: Why the hook decided that, for a block or an ask.
        additional_context: The text the hook added for the model.
        system_message: The text the hook showed the user.
        stop: Whether the agent must stop here.
        stop_reason: Why the agent must stop.
        updated_input: The arguments that replace the call's own arguments.
    """

    action: HookAction = "allow"
    reason: str = ""
    additional_context: str = ""
    system_message: str = ""
    stop: bool = False
    stop_reason: str = ""
    updated_input: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, raw: str, *, claude_dialect: bool = True) -> HookDecision:
        """Read the decision one hook answered with.

        The answer is one JSON object written in either dialect.  The
        Claude Code and Codex dialect names a permission decision inside
        ``hookSpecificOutput``, and DeepSpace's own dialect names the
        action in ``decision``.  An answer that names neither allows the
        call.

        Args:
            raw: The hook's standard output.
            claude_dialect: Whether the Claude Code and Codex dialect is
                accepted.

        Returns:
            The decision.

        Raises:
            HookError: When the answer is not a usable decision.
        """
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise HookError("A hook must answer with a JSON object.") from exc
        if not isinstance(payload, dict):
            raise HookError("A hook must answer with a JSON object.")
        return cls.from_payload(payload, claude_dialect=claude_dialect)

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        claude_dialect: bool = True,
    ) -> HookDecision:
        """Read the decision one hook answered with.

        A hook that answers in both dialects is accepted, and the Claude
        shape wins: the permission decision inside ``hookSpecificOutput``
        decides the action, and the Claude fields carry the message for
        the user, the context for the model, the stop request, and the
        arguments that replace the call's own.

        Args:
            payload: The hook's decoded answer.
            claude_dialect: Whether the Claude Code and Codex dialect is
                accepted.

        Returns:
            The decision.

        Raises:
            HookError: When the answer is not a usable decision.
        """
        claude_output = payload.get("hookSpecificOutput")
        if claude_output is not None and not isinstance(claude_output, Mapping):
            raise HookError("A hook's hookSpecificOutput must be an object.")
        claude: Mapping[str, Any] = claude_output if isinstance(claude_output, Mapping) else {}

        action: HookAction = "allow"
        reason = ""
        if claude_dialect and "permissionDecision" in claude:
            action = _coerce_action(claude.get("permissionDecision"))
            reason = _text_field(
                claude.get("permissionDecisionReason"),
                "permissionDecisionReason",
            )
            if not reason:
                reason = _text_field(payload.get("reason"), "reason")
        else:
            decision = payload.get("decision")
            if decision is not None:
                action = _coerce_action(decision, claude_dialect=claude_dialect)
            reason = _text_field(payload.get("reason"), "reason")

        additional_context = _text_field(
            payload.get("additional_context"),
            "additional_context",
        )
        system_message = ""
        stop = False
        stop_reason = ""
        updated_input: dict[str, Any] | None = None
        if claude_dialect:
            context = _first_value(((claude, "additionalContext"), (payload, "additionalContext")))
            if context is not None:
                additional_context = _text_field(context, "additionalContext")
            message = _first_value(((claude, "systemMessage"), (payload, "systemMessage")))
            if message is not None:
                system_message = _text_field(message, "systemMessage")
            replacement = _first_value(
                (
                    (claude, "updatedInput"),
                    (payload, "updatedInput"),
                    (payload, "updated_input"),
                )
            )
            if replacement is not None:
                if not isinstance(replacement, Mapping):
                    raise HookError("A hook's updatedInput must be an object.")
                updated_input = dict(replacement)
            stop, stop_reason = _claude_stop(claude, payload)
        return cls(
            action=action,
            reason=reason,
            additional_context=additional_context,
            system_message=system_message,
            stop=stop,
            stop_reason=stop_reason,
            updated_input=updated_input,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this decision.

        Returns:
            The decision's fields.
        """
        return {
            "action": self.action,
            "reason": self.reason,
            "additional_context": self.additional_context,
            "system_message": self.system_message,
            "stop": self.stop,
            "stop_reason": self.stop_reason,
            "updated_input": self.updated_input,
        }


@dataclass(frozen=True, slots=True)
class HookDefinition:
    """One hook the deployment declared.

    Attributes:
        id: The hook's identifier, unique within a manifest.
        event: The moment the hook takes part in.
        command: The command to run, as argv.
        timeout_seconds: How long the command may take.
        matcher: The tool name filter; an absent matcher applies to every
            tool call.
        matcher_kind: How the matcher is read: a literal tool name or a
            regular expression over the tool name.
    """

    id: str
    event: HookEvent
    command: tuple[str, ...]
    timeout_seconds: float = DEFAULT_HOOK_TIMEOUT_SECONDS
    matcher: str | None = None
    matcher_kind: MatcherKind = "literal"

    def matches(self, tool_name: str) -> bool:
        """Report whether this hook applies to one tool call.

        A literal matcher is the tool's own name, so it applies to that
        tool and to no other.  A regular-expression matcher is searched
        for anywhere in the tool name.  An absent matcher and ``*`` apply
        to every call.

        Args:
            tool_name: The tool the model called.

        Returns:
            True when the hook applies to the call.
        """
        matcher = (self.matcher or "").strip()
        if not matcher or matcher == MATCH_ALL:
            return True
        if self.matcher_kind == "regex":
            try:
                return re.search(matcher, tool_name) is not None
            except re.error:
                return False
        return matcher == tool_name


def _parse_definition(entry: object) -> HookDefinition:
    """Read one hook entry of a manifest.

    Args:
        entry: The manifest's entry.

    Returns:
        The hook the entry declares.

    Raises:
        HookError: When the entry does not declare a usable hook.
    """
    if not isinstance(entry, dict):
        raise HookError("A hook entry must be an object.")
    hook_id = str(entry.get("id") or "").strip()
    if not hook_id:
        raise HookError("A hook needs an id.")
    event = _coerce_event(str(entry.get("event") or ""))
    command = entry.get("command")
    if not isinstance(command, list) or not command:
        raise HookError(f"The hook {hook_id!r} needs a command.")
    if not all(isinstance(item, str) and item for item in command):
        raise HookError(f"The hook {hook_id!r} needs a command of non-empty strings.")
    timeout = entry.get("timeout_seconds")
    if timeout is None:
        timeout_seconds = float(DEFAULT_HOOK_TIMEOUT_SECONDS)
    elif isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise HookError(f"The hook {hook_id!r} has a timeout that is not a number.")
    elif timeout <= 0:
        raise HookError(f"The hook {hook_id!r} has a timeout that is not positive.")
    else:
        timeout_seconds = min(float(timeout), float(MAX_HOOK_TIMEOUT_SECONDS))
    matcher = entry.get("matcher")
    if matcher is not None and not isinstance(matcher, str):
        raise HookError(f"The hook {hook_id!r} has a matcher that is not a string.")
    matcher_kind: MatcherKind = "literal"
    raw_kind = entry.get("matcher_kind")
    if raw_kind is not None:
        if not isinstance(raw_kind, str):
            raise HookError(
                f"The hook {hook_id!r} has a matcher_kind that is not literal or regex."
            )
        normalized = raw_kind.strip().lower()
        if normalized not in MATCHER_KINDS:
            raise HookError(
                f"The hook {hook_id!r} has a matcher_kind that is not literal or regex."
            )
        matcher_kind = cast(MatcherKind, normalized)
    stripped_matcher = matcher.strip() if isinstance(matcher, str) else None
    if matcher_kind == "regex" and stripped_matcher:
        try:
            re.compile(stripped_matcher)
        except re.error as exc:
            raise HookError(
                f"The hook {hook_id!r} has a matcher that is not a valid regular expression."
            ) from exc
    return HookDefinition(
        id=hook_id,
        event=event,
        command=tuple(str(item) for item in command),
        timeout_seconds=timeout_seconds,
        matcher=stripped_matcher,
        matcher_kind=matcher_kind,
    )


class HookRegistry:
    """The hooks one deployment declared.

    The registry is the only place that knows which hooks exist.  A manifest
    that is not usable is skipped rather than fatal, so one broken entry
    cannot take the others down with it.
    """

    def __init__(self, hooks: Iterable[HookDefinition] = ()) -> None:
        """Create a registry.

        Args:
            hooks: The hooks the registry holds.
        """
        self._hooks: list[HookDefinition] = list(hooks)
        self._errors: list[str] = []

    @classmethod
    def from_manifest(cls, raw: object) -> HookRegistry:
        """Read one hook manifest.

        The manifest is one JSON object with a ``hooks`` list.  An entry
        that is not usable is skipped, and the rest of the manifest is kept.

        Args:
            raw: The manifest's decoded content.

        Returns:
            The hooks the manifest declared.
        """
        registry = cls()
        if not isinstance(raw, dict):
            registry._errors.append("A hook manifest must be an object.")
            return registry
        entries = raw.get("hooks")
        if entries is None:
            return registry
        if not isinstance(entries, list):
            registry._errors.append("A hook manifest's 'hooks' field must be a list.")
            return registry
        for entry in entries[:MAX_HOOKS_PER_MANIFEST]:
            try:
                definition = _parse_definition(entry)
            except HookError as exc:
                registry._errors.append(str(exc))
                continue
            if any(existing.id == definition.id for existing in registry._hooks):
                registry._errors.append(
                    f"The hook {definition.id!r} is declared more than once; "
                    "the first declaration wins."
                )
                continue
            registry._hooks.append(definition)
        return registry

    @classmethod
    def from_file(cls, path: Path | str) -> HookRegistry:
        """Read one hook manifest file.

        Args:
            path: The manifest's path.

        Returns:
            The hooks the manifest declared, empty when it cannot be read.
        """
        registry = cls()
        registry.load_file(path)
        return registry

    def load_file(self, path: Path | str) -> int:
        """Add the hooks one manifest file declares.

        Args:
            path: The manifest's path.

        Returns:
            The number of hooks the manifest added.
        """
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._errors.append(f"The hook manifest {str(path)!r} could not be read: {exc}")
            return 0
        loaded = self.from_manifest(raw)
        self._errors.extend(loaded.errors())
        return self.extend(loaded.all())

    def load_directory(self, directory: Path | str) -> int:
        """Add the hooks every manifest in one directory declares.

        Args:
            directory: The directory that holds the manifests.

        Returns:
            The number of hooks the manifests added.
        """
        root = Path(directory)
        if not root.is_dir():
            return 0
        added = 0
        for path in sorted(root.glob("*.json")):
            added += self.load_file(path)
        return added

    def extend(self, hooks: Iterable[HookDefinition]) -> int:
        """Add hooks to the registry.

        A hook whose id is already held is refused, so a second manifest
        cannot silently replace the first one's hook.  The registry holds a
        bounded number of hooks, so one directory of manifests cannot make
        the runtime start an unbounded number of commands.

        Args:
            hooks: The hooks to add.

        Returns:
            The number of hooks that were added.
        """
        added = 0
        for definition in hooks:
            if len(self._hooks) >= MAX_HOOKS:
                self._errors.append(
                    f"The registry holds at most {MAX_HOOKS} hooks; the rest were skipped."
                )
                break
            if any(existing.id == definition.id for existing in self._hooks):
                self._errors.append(
                    f"The hook {definition.id!r} is declared more than once; "
                    "the first declaration wins."
                )
                continue
            self._hooks.append(definition)
            added += 1
        return added

    def replace(self, hooks: Iterable[HookDefinition]) -> None:
        """Replace every hook the registry holds.

        Args:
            hooks: The hooks the registry holds afterwards.
        """
        self._hooks = list(hooks)
        self._errors = []

    def all(self) -> tuple[HookDefinition, ...]:
        """Return every hook the registry holds.

        Returns:
            The hooks, in the order they were declared.
        """
        return tuple(self._hooks)

    def hooks_for(self, event: HookEvent | str, tool_name: str) -> tuple[HookDefinition, ...]:
        """Return the hooks that apply to one tool call.

        Args:
            event: The moment the call reaches.
            tool_name: The tool the model called.

        Returns:
            The hooks that run for the event and match the tool.

        Raises:
            HookError: When the event is not an event a hook may run for.
        """
        resolved = _coerce_event(event)
        return tuple(
            definition
            for definition in self._hooks
            if definition.event is resolved and definition.matches(tool_name)
        )

    def errors(self) -> list[str]:
        """Return the problems the registry found while loading.

        Returns:
            The messages, empty when every manifest was clean.
        """
        return list(self._errors)


_HOOK_REGISTRY = HookRegistry()


def hook_registry() -> HookRegistry:
    """Return the process-wide hook registry.

    The registry is the deployment's hook list.  A deployment that declares
    hooks loads them into it once, and every tool call reads it.

    Returns:
        The registry.
    """
    return _HOOK_REGISTRY


def hook_environment() -> dict[str, str]:
    """Build the environment one hook command receives.

    The environment carries the paths a command needs and none of the
    deployment's secrets.

    Returns:
        The environment.
    """
    values: dict[str, str] = {}
    for name in HOOK_ENVIRONMENT_NAMES:
        value = os.environ.get(name)
        if value:
            values[name] = value
    values[HOOK_ENVIRONMENT_MARKER] = "1"
    return values


@dataclass(frozen=True, slots=True)
class _HookRun:
    """What one hook command produced.

    Attributes:
        returncode: The command's exit status.
        stdout: The command's standard output.
        stderr: The command's standard error.
    """

    returncode: int
    stdout: str
    stderr: str


async def _stop(process: asyncio.subprocess.Process) -> None:
    """Stop a hook command that did not finish in time.

    The command is killed and waited for, so the process does not outlive
    the call that started it.

    Args:
        process: The running command.
    """
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        await process.wait()
    except OSError:
        pass


class HookAuditRecorder(Protocol):
    """Records that a hook ran and what it decided.

    The runner writes both records for every hook it starts, so a
    deployment can see which hooks ran, how long they took, and what they
    answered.
    """

    def record_invoked(
        self,
        *,
        hook_id: str,
        event: str,
        tool_name: str,
        duration_ms: int,
    ) -> None:
        """Record that one hook ran.

        Args:
            hook_id: The hook's identifier.
            event: The moment the hook ran for.
            tool_name: The tool the call concerned.
            duration_ms: How long the hook took.
        """
        ...

    def record_result(
        self,
        *,
        hook_id: str,
        event: str,
        tool_name: str,
        decision: str,
        reason: str,
    ) -> None:
        """Record what one hook decided.

        Args:
            hook_id: The hook's identifier.
            event: The moment the hook ran for.
            tool_name: The tool the call concerned.
            decision: The action the hook answered with.
            reason: The reason the hook gave.
        """
        ...


def _combine(decisions: Sequence[HookDecision]) -> HookDecision:
    """Fold several hooks' decisions into one.

    The fold is order-independent: a blocking hook wins over a stopping
    hook, a stopping hook wins over an asking hook, and an asking hook
    wins over an allowing one.  The reason of the first hook that reached
    the winning action is the reason that is reported.

    Args:
        decisions: The decisions the hooks answered with.

    Returns:
        The combined decision.
    """
    blocked = next((item for item in decisions if item.action == "block"), None)
    asked = next((item for item in decisions if item.action == "ask"), None)
    stopped = next((item for item in decisions if item.stop), None)
    context = "\n".join(item.additional_context for item in decisions if item.additional_context)
    message = "\n".join(item.system_message for item in decisions if item.system_message)
    replacement = next(
        (item.updated_input for item in decisions if item.updated_input is not None),
        None,
    )
    if blocked is not None:
        return HookDecision(
            action="block",
            reason=blocked.reason,
            additional_context=context,
            system_message=message,
            stop=blocked.stop,
            stop_reason=blocked.stop_reason,
            updated_input=replacement,
        )
    if stopped is not None:
        return HookDecision(
            action=stopped.action,
            reason=stopped.reason,
            additional_context=context,
            system_message=message,
            stop=True,
            stop_reason=stopped.stop_reason,
            updated_input=replacement,
        )
    if asked is not None:
        return HookDecision(
            action="ask",
            reason=asked.reason,
            additional_context=context,
            system_message=message,
            updated_input=replacement,
        )
    allowed = next((item for item in decisions if item.reason), None)
    return HookDecision(
        action="allow",
        reason=allowed.reason if allowed is not None else "",
        additional_context=context,
        system_message=message,
        updated_input=replacement,
    )


class HookRunner:
    """Runs the hooks that apply to one event.

    The runner owns the process boundary: it starts each hook command, hands
    it the event as JSON on standard input, and reads the decision from its
    standard output.  A hook that fails is logged and ignored, so a broken
    hook never breaks the tool call.
    """

    def __init__(
        self,
        registry: HookRegistry | None = None,
        *,
        cwd: str | None = None,
        recorder: HookAuditRecorder | None = None,
        claude_dialect: bool = True,
    ) -> None:
        """Create a runner.

        Args:
            registry: The hooks to run, the process-wide registry when the
                caller names none.
            cwd: The directory the hook commands run in, the process's own
                directory when the caller names none.
            recorder: The recorder that writes the audit trail.  The trail
                is not written when the caller names none.
            claude_dialect: Whether the Claude Code and Codex dialect is
                accepted beside DeepSpace's own.
        """
        self._registry = registry if registry is not None else hook_registry()
        self._cwd = cwd
        self._recorder = recorder
        self._claude_dialect = claude_dialect

    @property
    def registry(self) -> HookRegistry:
        """Return the registry the runner reads."""
        return self._registry

    async def run_pre_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        conversation_id: str,
    ) -> HookDecision:
        """Ask the pre-tool hooks about one call.

        Args:
            tool_name: The tool the model called.
            arguments: The tool's arguments.
            conversation_id: The conversation the call belongs to.

        Returns:
            The combined decision.  A blocking hook wins over a stopping
            hook, a stopping hook wins over an asking hook, and an asking
            hook wins over an allowing one.
        """
        payload = {
            "event": HookEvent.PRE_TOOL_USE.value,
            "tool_name": tool_name,
            "arguments": dict(arguments),
            "conversation_id": conversation_id,
        }
        return await self._run(HookEvent.PRE_TOOL_USE, tool_name, payload)

    async def run_post_tool(self, tool_name: str, result: Any, conversation_id: str) -> str:
        """Return the context the post-tool hooks added.

        Args:
            tool_name: The tool that ran.
            result: The tool's result.
            conversation_id: The conversation the call belongs to.

        Returns:
            The context the hooks produced, empty when none did.
        """
        payload = {
            "event": HookEvent.POST_TOOL_USE.value,
            "tool_name": tool_name,
            "result": result,
            "conversation_id": conversation_id,
        }
        decision = await self._run(HookEvent.POST_TOOL_USE, tool_name, payload)
        return decision.additional_context

    async def run_event(
        self,
        event: HookEvent | str,
        *,
        tool_name: str | None = None,
        arguments: Mapping[str, Any] | None = None,
        result: Any = None,
        conversation_id: str = "",
    ) -> HookDecision:
        """Run the hooks for one event and return the combined decision.

        Args:
            event: The moment the call reaches.
            tool_name: The tool the model called.
            arguments: The tool's arguments.
            result: The tool's result.
            conversation_id: The conversation the call belongs to.

        Returns:
            The combined decision.
        """
        resolved = _coerce_event(event)
        payload = self._payload(
            resolved,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            conversation_id=conversation_id,
        )
        return await self._run(resolved, tool_name or "", payload)

    def _payload(
        self,
        event: HookEvent,
        *,
        tool_name: str | None,
        arguments: Mapping[str, Any] | None,
        result: Any,
        conversation_id: str,
    ) -> dict[str, Any]:
        """Build the JSON one hook receives on standard input.

        Args:
            event: The moment the call reaches.
            tool_name: The tool the model called.
            arguments: The tool's arguments.
            result: The tool's result.
            conversation_id: The conversation the call belongs to.

        Returns:
            The payload.
        """
        payload: dict[str, Any] = {
            "event": event.value,
            "conversation_id": conversation_id,
        }
        if event is HookEvent.PRE_TOOL_USE:
            payload["tool_name"] = tool_name or ""
            payload["arguments"] = dict(arguments or {})
        elif event is HookEvent.POST_TOOL_USE:
            payload["tool_name"] = tool_name or ""
            payload["result"] = result
        return payload

    async def _run(self, event: HookEvent, tool_name: str, payload: dict[str, Any]) -> HookDecision:
        """Run every hook that applies to one event.

        Args:
            event: The moment the call reaches.
            tool_name: The tool the model called.
            payload: The JSON one hook receives on standard input.

        Returns:
            The combined decision.
        """
        hooks = self._registry.hooks_for(event, tool_name)
        if not hooks:
            return HookDecision()
        outcomes = await asyncio.gather(
            *(self._run_one(hook, payload, event=event, tool_name=tool_name) for hook in hooks)
        )
        return _combine(outcomes)

    async def _run_one(
        self,
        hook: HookDefinition,
        payload: dict[str, Any],
        *,
        event: HookEvent,
        tool_name: str,
    ) -> HookDecision:
        """Run one hook and read its decision.

        The invocation and the result are written to the audit trail, so a
        deployment can see that the hook ran even when it failed.

        Args:
            hook: The hook to run.
            payload: The JSON the hook receives on standard input.
            event: The moment the call reaches.
            tool_name: The tool the model called.

        Returns:
            The hook's decision, or an allowing decision when the hook
            failed.
        """
        started = time.monotonic()
        decision, note = await self._decide(hook, payload)
        duration_ms = int((time.monotonic() - started) * 1000)
        self._record_invocation(hook, event, tool_name, duration_ms)
        self._record_result(hook, event, tool_name, decision, note)
        return decision

    async def _decide(
        self, hook: HookDefinition, payload: dict[str, Any]
    ) -> tuple[HookDecision, str]:
        """Run one hook command and read the answer it gave.

        Args:
            hook: The hook to run.
            payload: The JSON the hook receives on standard input.

        Returns:
            The hook's decision, and a note that explains a failure.  The
            note is empty when the hook answered.
        """
        try:
            completed = await self._execute(hook, payload)
        except TimeoutError:
            logger.warning(
                "The hook %r did not finish in time and was ignored.",
                hook.id,
            )
            return HookDecision(), "The hook did not finish in time and was ignored."
        except OSError as exc:
            logger.warning("The hook %r could not be started: %s", hook.id, exc)
            return HookDecision(), "The hook could not be started and was ignored."
        if completed.returncode == HOOK_BLOCK_EXIT_CODE and self._claude_dialect:
            reason = completed.stderr.strip()
            logger.warning("The hook %r blocked the call: %s", hook.id, reason[:500])
            return HookDecision(action="block", reason=reason), ""
        if completed.returncode != 0:
            logger.warning(
                "The hook %r exited with status %s and was ignored: %s",
                hook.id,
                completed.returncode,
                completed.stderr.strip()[:500],
            )
            return (
                HookDecision(),
                f"The hook exited with status {completed.returncode} and was ignored.",
            )
        if len(completed.stdout) > MAX_HOOK_ANSWER_CHARS:
            logger.warning(
                "The hook %r answered with more than %s characters and was ignored.",
                hook.id,
                MAX_HOOK_ANSWER_CHARS,
            )
            return (
                HookDecision(),
                f"The hook answered with more than {MAX_HOOK_ANSWER_CHARS} characters "
                "and was ignored.",
            )
        try:
            return (
                HookDecision.from_json(
                    completed.stdout,
                    claude_dialect=self._claude_dialect,
                ),
                "",
            )
        except HookError as exc:
            logger.warning("The hook %r answered with something unusable: %s", hook.id, exc)
            return HookDecision(), "The hook answered with something unusable and was ignored."

    def _record_invocation(
        self,
        hook: HookDefinition,
        event: HookEvent,
        tool_name: str,
        duration_ms: int,
    ) -> None:
        """Write the audit record of one hook invocation.

        The record is best effort: a storage problem must never break the
        tool call.

        Args:
            hook: The hook that ran.
            event: The moment the call reaches.
            tool_name: The tool the model called.
            duration_ms: How long the hook took.
        """
        recorder = self._recorder
        if recorder is None:
            return
        try:
            recorder.record_invoked(
                hook_id=hook.id,
                event=event.value,
                tool_name=tool_name,
                duration_ms=duration_ms,
            )
        except Exception:
            logger.warning("The hook invocation could not be recorded", exc_info=True)

    def _record_result(
        self,
        hook: HookDefinition,
        event: HookEvent,
        tool_name: str,
        decision: HookDecision,
        note: str = "",
    ) -> None:
        """Write the audit record of one hook's answer.

        The record is best effort: a storage problem must never break the
        tool call.

        Args:
            hook: The hook that ran.
            event: The moment the call reaches.
            tool_name: The tool the model called.
            decision: The answer the hook gave.
            note: Why the hook failed, when it failed.
        """
        recorder = self._recorder
        if recorder is None:
            return
        try:
            recorder.record_result(
                hook_id=hook.id,
                event=event.value,
                tool_name=tool_name,
                decision=decision.action,
                reason=note or decision.reason,
            )
        except Exception:
            logger.warning("The hook result could not be recorded", exc_info=True)

    async def _execute(self, hook: HookDefinition, payload: dict[str, Any]) -> _HookRun:
        """Start one hook command and collect what it produced.

        Args:
            hook: The hook to run.
            payload: The JSON the hook receives on standard input.

        Returns:
            The command's exit status and output.

        Raises:
            TimeoutError: When the command does not finish in time.
            OSError: When the command cannot be started.
        """
        body = json.dumps(payload, default=str).encode("utf-8")
        process = await asyncio.create_subprocess_exec(
            *hook.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=hook_environment(),
            cwd=self._cwd,
        )
        timeout = max(float(hook.timeout_seconds), MIN_HOOK_TIMEOUT_SECONDS)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(body), timeout=timeout)
        except TimeoutError:
            await _stop(process)
            raise
        return _HookRun(
            returncode=process.returncode if process.returncode is not None else 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


__all__ = [
    "CLAUDE_PERMISSION_ACTIONS",
    "DEFAULT_HOOK_TIMEOUT_SECONDS",
    "HOOK_ACTIONS",
    "HOOK_BLOCK_EXIT_CODE",
    "HOOK_ENVIRONMENT_MARKER",
    "HOOK_ENVIRONMENT_NAMES",
    "MATCHER_KINDS",
    "MAX_HOOK_ANSWER_CHARS",
    "MAX_HOOKS",
    "MAX_HOOKS_PER_MANIFEST",
    "MAX_HOOK_TIMEOUT_SECONDS",
    "HookAction",
    "HookAuditRecorder",
    "HookDecision",
    "HookDefinition",
    "HookError",
    "HookEvent",
    "HookRegistry",
    "HookRunner",
    "MatcherKind",
    "hook_environment",
    "hook_registry",
]
