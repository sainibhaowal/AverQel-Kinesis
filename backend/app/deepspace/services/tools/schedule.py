"""Scheduled follow-up tools for DeepSpace.

A schedule is a recurring prompt that DeepSpace sends back into a
conversation on an interval.  The REST API lets a human manage the
schedules of an account; these tools let the agent manage the schedules of
the conversation it is running in.

The service owns the tenant and user scope and the persistence rules, the
same ones the REST routes apply.  The conversation scope belongs to the
tool, so the tool applies it to what the service returns: a schedule of
another conversation is refused exactly like one that does not exist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.core.errors import ApiError
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.schedule import DeepSpaceSchedule
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    require_db,
    string_property,
)

#: The largest name a schedule may carry.
MAX_NAME_CHARS = 160

#: The largest prompt a schedule may carry.
MAX_PROMPT_CHARS = 20_000

#: The shortest interval between two runs, in minutes.
MIN_INTERVAL_MINUTES = 5

#: The longest interval between two runs, in minutes.
MAX_INTERVAL_MINUTES = 43_200

#: The interval a schedule runs at when the caller does not choose one.
DEFAULT_INTERVAL_MINUTES = 60

#: The message a missing or unreachable schedule reports.
SCHEDULE_NOT_FOUND_MESSAGE = "Schedule not found."

#: The conversation kind a schedule may belong to.
DEEPSPACE_CONVERSATION_KIND = "deepspace"


class ScheduleNotFoundError(Exception):
    """A schedule the caller may not reach, or one that does not exist."""


class ScheduleCreateArguments(ToolArguments):
    """Create a recurring follow-up for the calling conversation."""

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    interval_minutes: int = Field(
        default=DEFAULT_INTERVAL_MINUTES,
        ge=MIN_INTERVAL_MINUTES,
        le=MAX_INTERVAL_MINUTES,
    )


class ScheduleListArguments(ToolArguments):
    """List the recurring follow-ups of the calling conversation."""


class ScheduleUpdateArguments(ToolArguments):
    """Change one recurring follow-up of the calling conversation."""

    schedule_id: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_CHARS)
    prompt: str | None = Field(default=None, min_length=1, max_length=MAX_PROMPT_CHARS)
    interval_minutes: int | None = Field(
        default=None, ge=MIN_INTERVAL_MINUTES, le=MAX_INTERVAL_MINUTES
    )
    status: Literal["active", "paused"] | None = None


class ScheduleDeleteArguments(ToolArguments):
    """Remove one recurring follow-up of the calling conversation."""

    schedule_id: str = Field(min_length=1, max_length=64)


class ScheduleService:
    """The schedule operations the tools share, over one request's session.

    The service owns the tenant and user scope and the field limits, the
    first run time, and the run time a new interval produces, so the tools
    and the REST routes persist a schedule the same way.
    """

    def __init__(self, db: Session) -> None:
        """Create the service over one request's database session.

        Args:
            db: The request's database session.
        """
        self._db = db

    def list_schedules(self, auth: AuthContext) -> list[DeepSpaceSchedule]:
        """List every schedule the user owns, newest first.

        Args:
            auth: The authenticated tenant and user that own the call.

        Returns:
            The schedules.
        """
        return list(
            self._db.execute(
                select(DeepSpaceSchedule)
                .where(
                    DeepSpaceSchedule.tenant_id == auth.tenant_id,
                    DeepSpaceSchedule.user_id == auth.user_id,
                )
                .order_by(DeepSpaceSchedule.created_at.desc())
            )
            .scalars()
            .all()
        )

    def get_schedule(self, auth: AuthContext, schedule_id: uuid.UUID) -> DeepSpaceSchedule:
        """Load one schedule the user owns.

        Args:
            auth: The authenticated tenant and user that own the call.
            schedule_id: The schedule's identity.

        Returns:
            The schedule.

        Raises:
            ScheduleNotFoundError: When the user owns no schedule with that
                identity.
        """
        schedule = self._db.execute(
            select(DeepSpaceSchedule).where(
                DeepSpaceSchedule.id == schedule_id,
                DeepSpaceSchedule.tenant_id == auth.tenant_id,
                DeepSpaceSchedule.user_id == auth.user_id,
            )
        ).scalar_one_or_none()
        if schedule is None:
            raise ScheduleNotFoundError(SCHEDULE_NOT_FOUND_MESSAGE)
        return schedule

    def create_schedule(
        self,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        *,
        name: str,
        prompt: str,
        interval_minutes: int,
    ) -> DeepSpaceSchedule:
        """Create one schedule for a conversation.

        Args:
            auth: The authenticated tenant and user that own the call.
            conversation_id: The conversation the schedule belongs to.
            name: The schedule's name.
            prompt: The prompt the schedule sends into the conversation.
            interval_minutes: The interval between two runs.

        Returns:
            The created schedule.

        Raises:
            ApiError: When the conversation is not a DeepSpace conversation
                of the user.
            ValueError: When a field breaks the schedule's limits.
        """
        self._require_conversation(auth, conversation_id)
        clean_name = _clean_text(name, "name", MAX_NAME_CHARS)
        clean_prompt = _clean_text(prompt, "prompt", MAX_PROMPT_CHARS)
        _check_interval(interval_minutes)
        now = datetime.now(UTC)
        schedule = DeepSpaceSchedule(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            name=clean_name,
            prompt=clean_prompt,
            interval_minutes=interval_minutes,
            next_run_at=now + timedelta(minutes=interval_minutes),
        )
        self._db.add(schedule)
        self._db.commit()
        self._db.refresh(schedule)
        return schedule

    def update_schedule(
        self,
        auth: AuthContext,
        schedule_id: uuid.UUID,
        *,
        name: str | None = None,
        prompt: str | None = None,
        interval_minutes: int | None = None,
        status: str | None = None,
    ) -> DeepSpaceSchedule:
        """Change one schedule the user owns.

        A field the caller left out keeps its value.  A new interval starts
        the next run one interval from now, the same rule the REST route
        applies.

        Args:
            auth: The authenticated tenant and user that own the call.
            schedule_id: The schedule's identity.
            name: The schedule's new name.
            prompt: The prompt the schedule sends into the conversation.
            interval_minutes: The new interval between two runs.
            status: The schedule's new state.

        Returns:
            The changed schedule.

        Raises:
            ScheduleNotFoundError: When the user owns no schedule with that
                identity.
            ValueError: When a field breaks the schedule's limits.
        """
        schedule = self.get_schedule(auth, schedule_id)
        if name is not None:
            schedule.name = _clean_text(name, "name", MAX_NAME_CHARS)
        if prompt is not None:
            schedule.prompt = _clean_text(prompt, "prompt", MAX_PROMPT_CHARS)
        if interval_minutes is not None:
            _check_interval(interval_minutes)
            schedule.interval_minutes = interval_minutes
            schedule.next_run_at = datetime.now(UTC) + timedelta(minutes=interval_minutes)
        if status is not None:
            if status not in {"active", "paused"}:
                raise ValueError("A schedule is either active or paused.")
            schedule.status = status
        self._db.commit()
        self._db.refresh(schedule)
        return schedule

    def delete_schedule(self, auth: AuthContext, schedule_id: uuid.UUID) -> None:
        """Remove one schedule the user owns.

        Args:
            auth: The authenticated tenant and user that own the call.
            schedule_id: The schedule's identity.

        Raises:
            ScheduleNotFoundError: When the user owns no schedule with that
                identity.
        """
        schedule = self.get_schedule(auth, schedule_id)
        self._db.delete(schedule)
        self._db.commit()

    def _require_conversation(self, auth: AuthContext, conversation_id: uuid.UUID) -> None:
        """Refuse a conversation that is not a DeepSpace conversation of the user.

        Args:
            auth: The authenticated tenant and user that own the call.
            conversation_id: The conversation the schedule belongs to.

        Raises:
            ApiError: When the conversation is missing or belongs elsewhere.
        """
        conversation = self._db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == auth.tenant_id,
                Conversation.user_id == auth.user_id,
                Conversation.kind == DEEPSPACE_CONVERSATION_KIND,
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise ApiError(
                code="CONVERSATION_NOT_FOUND",
                message="DeepSpace conversation not found.",
                status_code=404,
            )


def _clean_text(value: str, field: str, max_chars: int) -> str:
    """Return one schedule field trimmed and checked against its limit.

    Args:
        value: The field the caller supplied.
        field: The field's name, used in the failure message.
        max_chars: The largest number of characters the field may carry.

    Returns:
        The trimmed field.

    Raises:
        ValueError: When the field is empty or longer than its limit.
    """
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError(f"The schedule {field} must not be empty.")
    if len(cleaned) > max_chars:
        raise ValueError(f"The schedule {field} must be at most {max_chars} characters.")
    return cleaned


def _check_interval(interval_minutes: int) -> None:
    """Refuse an interval outside the limits a schedule may run at.

    Args:
        interval_minutes: The interval between two runs.

    Raises:
        ValueError: When the interval is outside the limits.
    """
    if not MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES:
        raise ValueError(
            f"The interval must be between {MIN_INTERVAL_MINUTES} and "
            f"{MAX_INTERVAL_MINUTES} minutes."
        )


def _schedule_service(context: ToolContext) -> ScheduleService:
    """Return the schedule service for the calling request.

    Args:
        context: The calling request's tool context.

    Returns:
        The service.

    Raises:
        RuntimeError: When the runtime has no database session.
    """
    return ScheduleService(require_db(context))


def _schedule_id(value: str) -> uuid.UUID:
    """Return the schedule identity the model supplied.

    Args:
        value: The identity as the model wrote it.

    Returns:
        The identity.

    Raises:
        ValueError: When the value is not a schedule identity.
    """
    try:
        return uuid.UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("The schedule id is not a valid identifier.") from exc


def _conversation_schedules(
    service: ScheduleService, context: ToolContext
) -> list[DeepSpaceSchedule]:
    """Return the schedules of the calling conversation."""
    return [
        schedule
        for schedule in service.list_schedules(context.auth)
        if schedule.conversation_id == context.conversation_id
    ]


def _conversation_schedule(
    service: ScheduleService, context: ToolContext, schedule_id: uuid.UUID
) -> DeepSpaceSchedule:
    """Return one schedule of the calling conversation.

    A schedule of another conversation is refused exactly like one that does
    not exist, so a caller cannot learn that another conversation has one.

    Args:
        service: The schedule service of the calling request.
        context: The calling request's tool context.
        schedule_id: The schedule's identity.

    Returns:
        The schedule.

    Raises:
        ScheduleNotFoundError: When the schedule is missing or belongs to
            another conversation.
    """
    schedule = service.get_schedule(context.auth, schedule_id)
    if schedule.conversation_id != context.conversation_id:
        raise ScheduleNotFoundError(SCHEDULE_NOT_FOUND_MESSAGE)
    return schedule


def _serialize(schedule: DeepSpaceSchedule) -> dict[str, Any]:
    """Project one schedule for a model-facing result."""
    return {
        "id": str(schedule.id),
        "name": schedule.name,
        "interval_minutes": schedule.interval_minutes,
        "status": schedule.status,
        "next_run_at": schedule.next_run_at.isoformat(),
    }


async def _schedule_create(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Create a recurring follow-up for the calling conversation."""
    try:
        service = _schedule_service(context)
        raw_interval = arguments.get("interval_minutes")
        interval_minutes = DEFAULT_INTERVAL_MINUTES if raw_interval is None else int(raw_interval)
        schedule = service.create_schedule(
            context.auth,
            context.conversation_id,
            name=str(arguments.get("name") or ""),
            prompt=str(arguments.get("prompt") or ""),
            interval_minutes=interval_minutes,
        )
        return {"status": "ok", "schedule": _serialize(schedule)}
    except Exception as exc:  # noqa: BLE001 - a tool reports its failure to the model
        return {"status": "error", "message": str(exc)}


async def _schedule_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the recurring follow-ups of the calling conversation."""
    del arguments
    try:
        service = _schedule_service(context)
        schedules = _conversation_schedules(service, context)
        return {"status": "ok", "schedules": [_serialize(schedule) for schedule in schedules]}
    except Exception as exc:  # noqa: BLE001 - a tool reports its failure to the model
        return {"status": "error", "message": str(exc)}


async def _schedule_update(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Change one recurring follow-up of the calling conversation."""
    try:
        service = _schedule_service(context)
        schedule_id = _schedule_id(str(arguments.get("schedule_id") or ""))
        _conversation_schedule(service, context, schedule_id)
        name = arguments.get("name")
        prompt = arguments.get("prompt")
        interval_minutes = arguments.get("interval_minutes")
        status = arguments.get("status")
        schedule = service.update_schedule(
            context.auth,
            schedule_id,
            name=None if name is None else str(name),
            prompt=None if prompt is None else str(prompt),
            interval_minutes=None if interval_minutes is None else int(interval_minutes),
            status=None if status is None else str(status),
        )
        return {"status": "ok", "schedule": _serialize(schedule)}
    except Exception as exc:  # noqa: BLE001 - a tool reports its failure to the model
        return {"status": "error", "message": str(exc)}


async def _schedule_delete(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Remove one recurring follow-up of the calling conversation."""
    try:
        service = _schedule_service(context)
        schedule_id = _schedule_id(str(arguments.get("schedule_id") or ""))
        _conversation_schedule(service, context, schedule_id)
        service.delete_schedule(context.auth, schedule_id)
        return {"status": "ok", "deleted": True}
    except Exception as exc:  # noqa: BLE001 - a tool reports its failure to the model
        return {"status": "error", "message": str(exc)}


SCHEDULE_CREATE_DESCRIPTION = (
    "Create a recurring follow-up for this conversation. The prompt is sent back into "
    "this conversation every interval_minutes minutes, so it has to stand on its own "
    "and name what to report. Use this when the user asks for a recurring check, a "
    "periodic report, or a reminder."
)
SCHEDULE_LIST_DESCRIPTION = (
    "List the recurring follow-ups of this conversation with their interval, their "
    "state, and the time they run next."
)
SCHEDULE_UPDATE_DESCRIPTION = (
    "Change one recurring follow-up of this conversation: its name, its prompt, its "
    "interval, or whether it is active or paused. A paused schedule keeps its place "
    "but does not run."
)
SCHEDULE_DELETE_DESCRIPTION = (
    "Remove one recurring follow-up of this conversation. The follow-up stops running "
    "and is no longer listed."
)

SCHEDULE_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="schedule_create",
        description=SCHEDULE_CREATE_DESCRIPTION,
        properties={
            "name": string_property(
                "The follow-up's name, such as 'Daily deploy check'.",
                min_length=1,
                max_length=MAX_NAME_CHARS,
            ),
            "prompt": string_property(
                "The prompt the follow-up sends into this conversation.",
                min_length=1,
                max_length=MAX_PROMPT_CHARS,
            ),
            "interval_minutes": integer_property(
                "How many minutes pass between two runs.",
                minimum=MIN_INTERVAL_MINUTES,
                maximum=MAX_INTERVAL_MINUTES,
            ),
        },
        required=["name", "prompt"],
        arguments_model=ScheduleCreateArguments,
        mode="write",
        handler=_schedule_create,
    ),
    define_native_tool(
        name="schedule_list",
        description=SCHEDULE_LIST_DESCRIPTION,
        properties={},
        arguments_model=ScheduleListArguments,
        mode="read",
        handler=_schedule_list,
    ),
    define_native_tool(
        name="schedule_update",
        description=SCHEDULE_UPDATE_DESCRIPTION,
        properties={
            "schedule_id": string_property(
                "The id of the follow-up to change.", min_length=1, max_length=64
            ),
            "name": string_property(
                "The follow-up's new name.", min_length=1, max_length=MAX_NAME_CHARS
            ),
            "prompt": string_property(
                "The prompt the follow-up sends into this conversation.",
                min_length=1,
                max_length=MAX_PROMPT_CHARS,
            ),
            "interval_minutes": integer_property(
                "How many minutes pass between two runs.",
                minimum=MIN_INTERVAL_MINUTES,
                maximum=MAX_INTERVAL_MINUTES,
            ),
            "status": string_property("Whether the follow-up runs.", enum=["active", "paused"]),
        },
        required=["schedule_id"],
        arguments_model=ScheduleUpdateArguments,
        mode="write",
        handler=_schedule_update,
    ),
    define_native_tool(
        name="schedule_delete",
        description=SCHEDULE_DELETE_DESCRIPTION,
        properties={
            "schedule_id": string_property(
                "The id of the follow-up to remove.", min_length=1, max_length=64
            )
        },
        required=["schedule_id"],
        arguments_model=ScheduleDeleteArguments,
        mode="write",
        handler=_schedule_delete,
    ),
]

__all__ = ["SCHEDULE_TOOLS"]
