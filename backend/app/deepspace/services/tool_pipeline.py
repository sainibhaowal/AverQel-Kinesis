"""The three-stage tool pipeline every DeepSpace tool call traverses.

A tool call is not one call into a handler.  It is a pre-dispatch decision, an
around-dispatch body, and a post-dispatch decision, in that order.  Each
stage is open, so a deployment can add its own rules without replacing the
runtime's own policy.

``pre``
    Decides whether the call may run.  A participant may allow it, deny it
    with a reason, or cancel it.

``around``
    Wraps the body, so a participant can add a timeout, a retry, or a metric
    without owning the call.

``post``
    Decides what the model sees.  A participant may accept the result, replace
    it, or block it.

The runtime's own policy is the first pre participant and the last post
participant, so a deployment's own rules sit between the product's rules and
the tool body.

Every call also runs under a wall-clock budget, so a tool that never returns
cannot stall a turn.  The budget is the tool's own deadline when it has one,
the caller's when the caller names one, and the deployment's
``deepspace_tool_timeout_seconds`` otherwise.  A call that overruns its
budget is reported as a ``TOOL_TIMEOUT`` failure.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS, Settings

logger = logging.getLogger(__name__)

#: The error code a call that overran its budget carries.
TOOL_TIMEOUT_CODE = "TOOL_TIMEOUT"

#: The decision a pre-dispatch participant may return.
PreDecision = Literal["allow", "deny", "cancel"]

#: The decision a post-dispatch participant may return.
PostDecision = Literal["accept", "replace", "block"]

#: The body of one tool call.
ToolBody = Callable[[], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class PreOutcome:
    """What one pre-dispatch participant decided."""

    #: The participant's decision.
    decision: PreDecision
    #: The reason for a denial or a cancellation.
    reason: str | None = None
    #: Whether the participant asked the user about the call instead of
    #: refusing it.  A participant that asks hands the call to the product's
    #: approval flow, and the pipeline reports the ask to its caller.
    requires_approval: bool = False


@dataclass(frozen=True, slots=True)
class PostOutcome:
    """What one post-dispatch participant decided."""

    #: The participant's decision.
    decision: PostDecision
    #: The replacement payload, for a replacement.
    payload: Any = None
    #: The reason for a block.
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ToolPipelineResult:
    """The outcome of one call through the pipeline."""

    #: Whether the call ran and produced a payload.
    success: bool
    #: The payload the model receives.
    payload: Any = None
    #: The failure detail, when the call did not succeed.
    error: str | None = None
    #: The error category, when the call failed.
    error_category: str | None = None
    #: The stable error code, when the failure has one.
    error_code: str | None = None
    #: The participants that ran, in order.
    stages: tuple[str, ...] = field(default_factory=tuple)
    #: How long the body took, in milliseconds.
    duration_ms: int = 0
    #: Whether a participant asked the user about the call instead of
    #: refusing it.  The call did not run, and the caller may hand it to the
    #: product's approval flow.
    requires_approval: bool = False


def _validated_tool_timeouts(tool_timeouts: Mapping[str, float] | None) -> dict[str, float]:
    """Read the per-tool deadlines a deployment declared.

    Args:
        tool_timeouts: The deadline each named tool runs under.

    Returns:
        The deadlines, keyed by tool name.

    Raises:
        ValueError: When a deadline is not a positive number of seconds.
    """
    if not tool_timeouts:
        return {}
    validated: dict[str, float] = {}
    for name, budget in tool_timeouts.items():
        value = float(budget)
        if value <= 0:
            raise ValueError(f"The deadline for tool {name!r} must be positive: {value}.")
        validated[str(name)] = value
    return validated


def _settings_timeout_seconds(settings: Settings | None) -> float:
    """Read the deployment's default tool deadline.

    Args:
        settings: The deployment settings, when the pipeline was given them.

    Returns:
        The deadline in seconds.

    Raises:
        ValueError: When the configured deadline is not a positive number of
            seconds.
    """
    if settings is None:
        return DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS
    raw: float | None = getattr(settings, "deepspace_tool_timeout_seconds", None)
    if raw is None:
        return DEFAULT_DEEPSPACE_TOOL_TIMEOUT_SECONDS
    value = float(raw)
    if value <= 0:
        raise ValueError(f"deepspace_tool_timeout_seconds must be positive: {value}.")
    return value


class ToolPipeline:
    """The registry of tool pipeline participants.

    Participants are registered by name and run in registration order.  A
    participant that raises is contained: the pipeline reports the failure
    and continues, so one broken rule cannot fail every tool call.

    The pipeline also owns the wall-clock budget of a call.  A pipeline that
    was given the deployment settings uses ``deepspace_tool_timeout_seconds``
    for every call that names no other budget, and a tool that declares its
    own deadline runs under that one instead.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        tool_timeouts: Mapping[str, float] | None = None,
    ) -> None:
        """Build the pipeline.

        Args:
            settings: The deployment settings, which own the default budget.
            tool_timeouts: The deadline each named tool runs under.  A tool
                that appears here runs under its own deadline instead of the
                default one.

        Raises:
            ValueError: When a deadline is not a positive number of seconds.
        """
        self._pre: list[
            tuple[str, Callable[[str, dict[str, Any]], PreOutcome | Awaitable[PreOutcome]]]
        ] = []
        self._post: list[tuple[str, Callable[[str, Any], PostOutcome]]] = []
        self._tool_timeouts = _validated_tool_timeouts(tool_timeouts)
        self._default_timeout_seconds = _settings_timeout_seconds(settings)

    def register_pre(
        self,
        name: str,
        participant: Callable[[str, dict[str, Any]], PreOutcome | Awaitable[PreOutcome]],
    ) -> None:
        """Register one pre-dispatch participant.

        Args:
            name: The participant's name, used in the stage trace.
            participant: The participant, called with the tool name and the
                validated arguments.
        """
        self._pre.append((name, participant))

    def register_post(self, name: str, participant: Callable[[str, Any], PostOutcome]) -> None:
        """Register one post-dispatch participant.

        Args:
            name: The participant's name, used in the stage trace.
            participant: The participant, called with the tool name and the
                settled payload.
        """
        self._post.append((name, participant))

    @property
    def participants(self) -> tuple[str, ...]:
        """The names of every registered participant, in registration order."""
        return tuple(name for name, _ in self._pre) + tuple(name for name, _ in self._post)

    def resolve_timeout(self, tool_name: str, requested_seconds: float | None) -> float | None:
        """The wall-clock budget one call runs under.

        A tool that declares its own deadline owns its budget, so the call
        cannot outlive the deadline that tool was given.  Otherwise the
        caller's budget applies, and the deployment's default budget applies
        to a call that names none.

        Args:
            tool_name: The tool the model called.
            requested_seconds: The budget the caller asked for, if any.

        Returns:
            The budget in seconds, or None when the call may run unbounded.
        """
        override = self._tool_timeouts.get(tool_name)
        if override is not None:
            return override
        if requested_seconds is not None:
            return float(requested_seconds)
        return self._default_timeout_seconds

    async def run(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        body: ToolBody,
        timeout_seconds: float | None = None,
    ) -> ToolPipelineResult:
        """Run one call through the whole pipeline.

        Args:
            tool_name: The tool the model called.
            arguments: The validated arguments.
            body: The tool's implementation.
            timeout_seconds: The wall-clock budget for the body.  A tool with
                its own deadline and the deployment's default budget apply
                when the caller names none.

        Returns:
            The outcome of the call.
        """
        budget = self.resolve_timeout(tool_name, timeout_seconds)
        stages: list[str] = []
        for name, participant in list(self._pre):
            try:
                outcome = participant(tool_name, arguments)
                if inspect.isawaitable(outcome):
                    # A participant that has to wait for something outside the
                    # process - a hook command, for example - is awaited here.
                    outcome = await outcome
            except Exception:  # noqa: BLE001
                logger.warning("DeepSpace tool pre participant failed", exc_info=True)
                stages.append(f"{name}:failed")
                continue
            stages.append(f"{name}:{outcome.decision}")
            if outcome.decision == "deny":
                return ToolPipelineResult(
                    success=False,
                    error=outcome.reason or "Tool blocked by policy.",
                    stages=tuple(stages),
                )
            if outcome.decision == "cancel":
                return ToolPipelineResult(
                    success=False,
                    error=outcome.reason or "Tool call cancelled.",
                    error_category="cancelled",
                    stages=tuple(stages),
                    requires_approval=outcome.requires_approval,
                )
        started = time.monotonic()
        try:
            if budget is None:
                payload = await body()
            else:
                payload = await asyncio.wait_for(body(), timeout=budget)
        except TimeoutError:
            logger.warning("DeepSpace tool %s exceeded its %s second budget", tool_name, budget)
            return ToolPipelineResult(
                success=False,
                error="Tool execution exceeded its time budget.",
                error_category="timeout",
                error_code=TOOL_TIMEOUT_CODE,
                stages=tuple(stages),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolPipelineResult(
                success=False,
                error=str(exc),
                error_category="tool",
                stages=tuple(stages),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        for post_name, post_participant in list(self._post):
            try:
                post_outcome = post_participant(tool_name, payload)
            except Exception:  # noqa: BLE001
                logger.warning("DeepSpace tool post participant failed", exc_info=True)
                stages.append(f"{post_name}:failed")
                continue
            stages.append(f"{post_name}:{post_outcome.decision}")
            if post_outcome.decision == "block":
                return ToolPipelineResult(
                    success=False,
                    error=post_outcome.reason or "Tool result blocked by policy.",
                    error_category="blocked",
                    stages=tuple(stages),
                    duration_ms=duration_ms,
                )
            if post_outcome.decision == "replace":
                payload = post_outcome.payload

        return ToolPipelineResult(
            success=True,
            payload=payload,
            stages=tuple(stages),
            duration_ms=duration_ms,
        )


class DeepSpaceToolPipeline(ToolPipeline):
    """The pipeline with DeepSpace's own policy installed as a participant.

    The policy runs first, so a deployment's own rules sit between the
    product's allowlist and the tool body, and it runs last, so a deployment
    cannot see a result the product's allowlist would have removed.
    """

    def __init__(
        self,
        policy: Any,
        settings: Settings | None = None,
        tool_timeouts: Mapping[str, float] | None = None,
    ) -> None:
        """Build the pipeline with DeepSpace's own policy installed.

        Args:
            policy: The product's tool policy.
            settings: The deployment settings, which own the default budget.
            tool_timeouts: The deadline each named tool runs under.  A tool
                that appears here runs under its own deadline instead of the
                default one.

        Raises:
            ValueError: When a deadline is not a positive number of seconds.
        """
        super().__init__(settings=settings, tool_timeouts=tool_timeouts)
        self._policy = policy
        self.register_pre("deepspace-policy", self._policy_participant)
        self.register_post("deepspace-policy", self._policy_result_participant)

    def _policy_participant(self, tool_name: str, arguments: dict[str, Any]) -> PreOutcome:
        """Ask the product's policy whether the call may run.

        Args:
            tool_name: The tool the model called.
            arguments: The validated arguments.

        Returns:
            The policy's decision.
        """
        decision = self._policy.before_tool(tool_name, arguments)
        if not getattr(decision, "allowed", False):
            return PreOutcome("deny", getattr(decision, "reason", None))
        return PreOutcome("allow")

    def _policy_result_participant(self, tool_name: str, payload: Any) -> PostOutcome:
        """Let the product's policy remove internal values from the result.

        Args:
            tool_name: The tool that ran.
            payload: The settled payload.

        Returns:
            The policy's decision.
        """
        if not isinstance(payload, dict):
            return PostOutcome("accept")
        cleaned = self._policy.after_tool(tool_name, payload)
        if cleaned == payload:
            return PostOutcome("accept")
        return PostOutcome("replace", cleaned)
