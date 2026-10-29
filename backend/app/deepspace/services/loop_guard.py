"""The per-conversation loop guard for DeepSpace.

An agent that keeps calling the same tool, or keeps calling tools at all,
never ends the turn on its own: the run continues until the provider's own
budget ends, and the deployment pays for every call.  The guard watches one
conversation's turn and refuses the call that crosses a limit, so the turn
ends with a typed refusal instead of hanging or burning tokens.

Four limits bound a turn: the number of tool calls it may make, how often
one tool call with the same arguments may repeat, how often one set of
arguments may repeat whichever tool carries it, and the turn's wall-clock
budget. Repetition limits refuse the offending call only; they do not make
an earlier read refusal deny unrelated later tools. The turn-wide call and
time limits still stop the turn when reached.

The guard is a plain in-memory object with no I/O, so it is cheap enough to
consult on every tool call.  A registry holds one guard per conversation,
and the caller starts the turn, checks each call before it runs, and records
the calls that were allowed.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

#: The typed code the API layer surfaces when the guard stops a turn.
LOOP_GUARD_CODE = "LOOP_GUARD_TRIPPED"

#: The default number of tool calls one turn may make.
DEFAULT_MAX_TOOL_CALLS = 64
#: The default number of times one tool call with the same arguments may repeat.
DEFAULT_MAX_IDENTICAL_CALLS = 4
#: The default number of times one set of arguments may repeat.
DEFAULT_MAX_REPEATED_ARGUMENTS = 6
#: The default wall-clock budget one turn has, in seconds.
DEFAULT_MAX_TURN_SECONDS = 900.0

# These tools only observe coordination state. They may be repeated while a
# team changes, but the turn-wide call and time limits still apply. Keep this
# policy beside the guard so every caller uses the same classification.
REPEATABLE_COORDINATION_READ_TOOLS = frozenset(
    {
        "discover_tools",
        "get_agent",
        "list_agents",
        "list_teammates",
        "team_get_member",
        "team_list_members",
        "team_task_get",
        "team_task_list",
        "wait_agent",
    }
)

# These reads observe the team board or child-agent state. A caller may use
# the result to detect progress, but repeated identical observations must not
# keep a turn alive forever. ``discover_tools`` is intentionally not included:
# it discovers capabilities rather than observing team progress.
COORDINATION_PROGRESS_READ_TOOLS = frozenset(
    {
        "list_agents",
        "list_teammates",
        "team_get_member",
        "team_list_members",
        "team_task_get",
        "team_task_list",
        "wait_agent",
    }
)


@dataclass(frozen=True, slots=True)
class LoopGuardLimits:
    """The limits one agent turn runs under.

    The limits belong to the deployment, so every conversation on a
    deployment is protected the same way.  ``from_settings`` reads them from
    the deployment settings when the settings carry them, and the defaults
    apply until they do.
    """

    #: The tool calls one turn may make.
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    #: The times one tool call with the same arguments may repeat in a turn.
    max_identical_calls: int = DEFAULT_MAX_IDENTICAL_CALLS
    #: The times one set of arguments may repeat in a turn, whichever tool
    #: carries it.
    max_repeated_arguments: int = DEFAULT_MAX_REPEATED_ARGUMENTS
    #: The wall-clock budget one turn has, in seconds.
    max_turn_seconds: float = DEFAULT_MAX_TURN_SECONDS

    @classmethod
    def from_settings(cls, settings: Any) -> LoopGuardLimits:
        """Read the limits from the deployment settings.

        A settings object that does not carry a limit keeps the default, so
        the guard works before the settings land.

        Args:
            settings: The deployment settings, or any object with the same
                attribute names.

        Returns:
            The limits the deployment runs under.
        """
        return cls(
            max_tool_calls=_int_setting(
                settings, "deepspace_loop_guard_max_tool_calls", DEFAULT_MAX_TOOL_CALLS
            ),
            max_identical_calls=_int_setting(
                settings,
                "deepspace_loop_guard_max_identical_calls",
                DEFAULT_MAX_IDENTICAL_CALLS,
            ),
            max_repeated_arguments=_int_setting(
                settings,
                "deepspace_loop_guard_max_repeated_arguments",
                DEFAULT_MAX_REPEATED_ARGUMENTS,
            ),
            max_turn_seconds=_float_setting(
                settings,
                "deepspace_loop_guard_max_turn_seconds",
                DEFAULT_MAX_TURN_SECONDS,
            ),
        )


#: The limits a deployment runs under before its settings carry its own.
DEFAULT_LIMITS = LoopGuardLimits()


@dataclass(frozen=True, slots=True)
class LoopGuardTrip:
    """Why the guard refused a call or stopped a turn.

    The trip is the report the refusal carries: the typed code the API layer
    surfaces, a sentence a person can read, the tool the call belonged to,
    and the limit that was hit.
    """

    #: The typed code every guard refusal carries.
    code: str = LOOP_GUARD_CODE
    #: The sentence that explains the refusal.
    reason: str = ""
    #: The tool the refused call belonged to, when the refusal names one.
    tool_name: str | None = None
    #: The limit that was hit: ``tool_calls``, ``identical_calls``,
    #: ``repeated_arguments``, or ``turn_seconds``.
    limit: str = "tool_calls"

    def to_dict(self) -> dict[str, Any]:
        """Return the trip as a JSON-serializable mapping.

        Returns:
            The trip's fields.
        """
        return {
            "code": self.code,
            "reason": self.reason,
            "tool_name": self.tool_name,
            "limit": self.limit,
        }


class LoopGuard:
    """The loop breaker for one conversation's turn.

    The guard counts the tool calls of the current turn and refuses the call
    that crosses a limit.  The counters hold the calls of the turn as a
    whole, so a call that repeats after an interruption still counts
    towards the limit.

    The guard takes no lock: one conversation's turn runs on one event loop,
    and the counters are plain integers.
    """

    def __init__(
        self,
        *,
        conversation_id: uuid.UUID,
        limits: LoopGuardLimits | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the guard for one conversation.

        Args:
            conversation_id: The conversation the guard watches.
            limits: The limits the turn runs under; the deployment defaults
                apply when they are absent.
            now: The clock the turn's wall-clock budget is measured against.
        """
        self._conversation_id = conversation_id
        self._limits = limits if limits is not None else DEFAULT_LIMITS
        self._now = now
        self._turn_started_at = now()
        self._tool_calls = 0
        self._identical_calls: dict[tuple[str, str], int] = {}
        self._argument_calls: dict[str, int] = {}
        self._trip: LoopGuardTrip | None = None
        self._last_refusal: LoopGuardTrip | None = None

    @property
    def conversation_id(self) -> uuid.UUID:
        """The conversation the guard watches."""
        return self._conversation_id

    @property
    def limits(self) -> LoopGuardLimits:
        """The limits the guard applies."""
        return self._limits

    @property
    def elapsed_seconds(self) -> float:
        """The seconds the current turn has run."""
        return max(0.0, self._now() - self._turn_started_at)

    def start_turn(self) -> None:
        """Reset the turn's counters and start the turn clock."""
        self._turn_started_at = self._now()
        self._tool_calls = 0
        self._identical_calls.clear()
        self._argument_calls.clear()
        self._trip = None
        self._last_refusal = None

    def check(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        enforce_repetition_limits: bool = True,
    ) -> LoopGuardTrip | None:
        """Decide whether one tool call may run.

        The limits are consulted in a fixed order: the number of calls in the
        turn, the identical call, the repeated arguments, and the turn's
        wall-clock budget. Repetition checks can be disabled for bounded,
        read-only coordination tools; the turn-wide limits still apply.
        Repetition refusals apply only to the current call. A turn-wide
        refusal continues to refuse later calls because the turn cannot safely
        continue.

        Args:
            tool_name: The tool the model called.
            arguments: The call's arguments.
            enforce_repetition_limits: Whether this call is subject to the
                per-call repetition limits.

        Returns:
            The trip that stops the turn, or None when the call may run.
        """
        if self._trip is not None:
            return self._trip
        if self._tool_calls >= self._limits.max_tool_calls:
            return self._stop(
                LoopGuardTrip(
                    reason=(
                        f"This turn reached its limit of {self._limits.max_tool_calls} tool calls."
                    ),
                    tool_name=tool_name,
                    limit="tool_calls",
                )
            )
        canonical = _canonical_arguments(arguments)
        if enforce_repetition_limits:
            identical = self._identical_calls.get((tool_name, canonical), 0)
            if identical >= self._limits.max_identical_calls:
                return self._refuse(
                    LoopGuardTrip(
                        reason=f"The same tool call repeated {identical + 1} times.",
                        tool_name=tool_name,
                        limit="identical_calls",
                    )
                )
            repeated = self._argument_calls.get(canonical, 0)
            if repeated >= self._limits.max_repeated_arguments:
                return self._refuse(
                    LoopGuardTrip(
                        reason=f"These tool arguments repeated {repeated + 1} times.",
                        tool_name=tool_name,
                        limit="repeated_arguments",
                    )
                )
        budget = self.turn_budget_trip()
        if budget is not None:
            return self._stop(budget)
        return None

    def record(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        enforce_repetition_limits: bool = True,
    ) -> None:
        """Record one tool call the guard allowed.

        Args:
            tool_name: The tool that ran.
            arguments: The call's arguments.
            enforce_repetition_limits: Whether to include this call in the
                per-call repetition counters.
        """
        self._tool_calls += 1
        if not enforce_repetition_limits:
            return
        canonical = _canonical_arguments(arguments)
        key = (tool_name, canonical)
        self._identical_calls[key] = self._identical_calls.get(key, 0) + 1
        self._argument_calls[canonical] = self._argument_calls.get(canonical, 0) + 1

    def trip(self) -> LoopGuardTrip | None:
        """Return the latest trip, when a call was refused or a turn stopped."""
        return self._trip or self._last_refusal

    def stats(self) -> dict[str, int]:
        """Return the turn's counters for a diagnostics route.

        Returns:
            The call counters and the seconds the turn has run.
        """
        return {
            "tool_calls": self._tool_calls,
            "distinct_calls": len(self._identical_calls),
            "max_identical_calls": max(self._identical_calls.values(), default=0),
            "max_repeated_arguments": max(self._argument_calls.values(), default=0),
            "turn_elapsed_seconds": int(self.elapsed_seconds),
        }

    def turn_budget_trip(self) -> LoopGuardTrip | None:
        """Return the trip when the turn's wall-clock budget is spent.

        The check does not change the guard, so the caller can ask for it
        between provider rounds without stopping the turn by itself.

        Returns:
            The trip that ends the turn, or None when the turn has budget.
        """
        elapsed = self.elapsed_seconds
        budget = self._limits.max_turn_seconds
        if elapsed <= budget:
            return None
        return LoopGuardTrip(
            reason=(
                f"This turn ran for {elapsed:.0f} seconds, over its {budget:.0f}-second budget."
            ),
            tool_name=None,
            limit="turn_seconds",
        )

    def _stop(self, trip: LoopGuardTrip) -> LoopGuardTrip:
        """Remember the trip that stopped the turn.

        Args:
            trip: The trip the guard hit.

        Returns:
            The same trip, so the caller can return it directly.
        """
        self._trip = trip
        self._last_refusal = trip
        return trip

    def _refuse(self, trip: LoopGuardTrip) -> LoopGuardTrip:
        """Refuse one repeated call without stopping unrelated tool calls.

        The attempted call still consumes one turn-wide call budget, so a
        model cannot avoid the global bound by replaying a refused write.
        """
        self._tool_calls += 1
        self._last_refusal = trip
        return trip


class LoopGuardRegistry:
    """The process-wide registry of per-conversation guards.

    The registry creates a guard on the conversation's first use and keeps
    it until the conversation's guard is dropped.  A guard that already
    exists keeps the limits it was created with.
    """

    def __init__(self) -> None:
        self._guards: dict[uuid.UUID, LoopGuard] = {}

    def get(self, conversation_id: uuid.UUID, limits: LoopGuardLimits | None = None) -> LoopGuard:
        """Return the conversation's guard.

        Args:
            conversation_id: The conversation the guard watches.
            limits: The limits for a guard the registry creates now; they
                are ignored for a guard that already exists.

        Returns:
            The conversation's guard.
        """
        guard = self._guards.get(conversation_id)
        if guard is None:
            guard = LoopGuard(conversation_id=conversation_id, limits=limits)
            self._guards[conversation_id] = guard
        return guard

    def drop(self, conversation_id: uuid.UUID) -> None:
        """Forget the conversation's guard.

        Args:
            conversation_id: The conversation whose guard is forgotten.
        """
        self._guards.pop(conversation_id, None)

    def clear(self) -> None:
        """Forget every guard."""
        self._guards.clear()


#: The registry the process shares.
_REGISTRY = LoopGuardRegistry()


def loop_guard_registry() -> LoopGuardRegistry:
    """Return the process-wide registry of per-conversation guards."""
    return _REGISTRY


def turn_budget_exhausted(guard: LoopGuard) -> LoopGuardTrip | None:
    """Return the trip when a turn's wall-clock budget is spent.

    The task loop asks for this between provider rounds, so a turn that
    spends its budget without calling a tool still ends.

    Args:
        guard: The conversation's guard.

    Returns:
        The trip that ends the turn, or None when the turn has budget left.
    """
    return guard.turn_budget_trip()


def guard_result(trip: LoopGuardTrip) -> dict[str, Any]:
    """Return the refusal result for a tripped guard.

    The result is the refusal a tool call reports: the status, the message
    the model reads, and the typed code the API layer surfaces.

    Args:
        trip: The trip that stopped the turn.

    Returns:
        The refusal result.
    """
    return {
        "status": "denied",
        "error_code": trip.code,
        "message": trip.reason,
        "limit": trip.limit,
        "tool_name": trip.tool_name,
        # A turn-wide limit is terminal. The caller must not send the same
        # denial through another provider round until a second limit is hit.
        "stop_turn": trip.limit in {"tool_calls", "turn_seconds"},
        "recoverable": trip.limit not in {"tool_calls", "turn_seconds"},
    }


def _canonical_arguments(arguments: Mapping[str, Any]) -> str:
    """Return the stable string form of one call's arguments.

    The keys are sorted and the values are rendered as text, so two calls
    that carry the same arguments in a different order are the same call.

    Args:
        arguments: The call's arguments.

    Returns:
        The canonical form of the arguments.
    """
    return json.dumps(dict(arguments), sort_keys=True, default=str)


def _int_setting(settings: Any, name: str, default: int) -> int:
    """Read one integer setting, falling back to the default.

    Args:
        settings: The deployment settings.
        name: The setting's name.
        default: The value used when the settings do not carry it.

    Returns:
        The setting's value.
    """
    value = getattr(settings, name, None)
    if value is None:
        return default
    return int(value)


def _float_setting(settings: Any, name: str, default: float) -> float:
    """Read one floating-point setting, falling back to the default.

    Args:
        settings: The deployment settings.
        name: The setting's name.
        default: The value used when the settings do not carry it.

    Returns:
        The setting's value.
    """
    value = getattr(settings, name, None)
    if value is None:
        return default
    return float(value)


__all__ = [
    "DEFAULT_LIMITS",
    "DEFAULT_MAX_IDENTICAL_CALLS",
    "DEFAULT_MAX_REPEATED_ARGUMENTS",
    "DEFAULT_MAX_TOOL_CALLS",
    "DEFAULT_MAX_TURN_SECONDS",
    "COORDINATION_PROGRESS_READ_TOOLS",
    "LOOP_GUARD_CODE",
    "REPEATABLE_COORDINATION_READ_TOOLS",
    "LoopGuard",
    "LoopGuardLimits",
    "LoopGuardRegistry",
    "LoopGuardTrip",
    "guard_result",
    "loop_guard_registry",
    "turn_budget_exhausted",
]
