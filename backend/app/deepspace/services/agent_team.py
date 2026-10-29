"""The named team of one DeepSpace conversation.

A team is a set of named teammates that work the same task as the agent that
created them.  Three durable records carry it: the members, the messages that
travel between them, and the task board they share.

Every operation is scoped to a tenant, a user, and a conversation, so a
teammate, a message, and a task of one conversation are never reachable from
another.  A message for a teammate that is not running is queued instead of
lost, and the teammate receives it when it resumes.

A teammate is a delegated child: the teammate's composition is prepared with
the delegation machinery, so a teammate is built the same way a delegated
child is: the same descriptor, the same provider and model fields, the same
tool filter.  The child run that works the teammate holds the messages that
wait for it, so the subagent worker answers them when the teammate works.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.ids import generate_uuid7_with_fallback
from app.deepspace.models.agent_team import (
    TEAM_MEMBER_STATE_FINISHED,
    TEAM_MEMBER_STATE_IDLE,
    TEAM_MEMBER_STATE_RUNNING,
    TEAM_MEMBER_STATES,
    TEAM_MESSAGE_STATE_DELIVERED,
    TEAM_MESSAGE_STATE_QUEUED,
    TEAM_TASK_STATE_COMPLETED,
    TEAM_TASK_STATE_IN_PROGRESS,
    TEAM_TASK_STATE_PENDING,
    TEAM_TASK_STATES,
    TeamMember,
    TeamMessage,
    TeamTask,
)
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.session_events import (
    TEAM_ENABLED,
    TEAM_MEMBER_INTERRUPTED,
    TEAM_MEMBER_SPAWNED,
    TEAM_MEMBER_STATE,
    TEAM_MESSAGE,
    TEAM_TASK,
    SessionEventStore,
)

#: The longest a teammate name may be.
MAX_MEMBER_NAME_CHARS = 64

#: The longest message body a teammate may receive.
MAX_MESSAGE_CHARS = 20_000

#: The longest persona a teammate may carry.
MAX_PERSONA_CHARS = 4_000

#: The longest task title the board may hold.
MAX_TASK_TITLE_CHARS = 200

#: The longest task body the board may hold.
MAX_TASK_BODY_CHARS = 20_000

#: The longest a waiter waits by default.
DEFAULT_WAIT_SECONDS = 30.0

#: Legacy fallback interval accepted by callers that still pass this argument.
#: Normal waits are event-driven and do not use it.
DEFAULT_POLL_INTERVAL_SECONDS = 0.25

#: The name a member address is written with.
MEMBER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

#: The largest number of messages a teammate may have waiting for its turn.
#:
#: The waiting messages are the teammate's own turn queue, so the cap keeps
#: one teammate's queue from growing without bound.
MAX_PENDING_MESSAGES = 20

#: The longest answer a teammate's projection shows.
MAX_RESULT_CHARS = 2_000

TEAM_EVENT_TYPES = frozenset(
    {
        TEAM_ENABLED,
        TEAM_MEMBER_INTERRUPTED,
        TEAM_MEMBER_SPAWNED,
        TEAM_MEMBER_STATE,
        TEAM_MESSAGE,
        TEAM_TASK,
    }
)


@dataclass(slots=True)
class _TeamActivity:
    """One process-local notification channel for a durable team."""

    condition: threading.Condition = field(default_factory=threading.Condition)
    generation: int = 0


_ACTIVITY_LOCK = threading.Lock()
_ACTIVITY_CHANNELS: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], _TeamActivity] = {}
_REMOTE_REDIS_LOCK = threading.Lock()
_REMOTE_REDIS_CLIENTS: dict[str, Any] = {}


def _activity_channel_name(
    tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> str:
    """Return the Redis channel for one tenant-scoped team."""
    return f"deepspace:team:{tenant_id}:{user_id}:{conversation_id}"


def _remote_redis_client() -> Any | None:
    """Return the configured Redis client, or None for local-only mode."""
    try:
        from redis import Redis

        from app.core.config import get_settings

        url = str(get_settings().redis_url or "").strip()
        if not url or url.startswith("memory://"):
            return None
        with _REMOTE_REDIS_LOCK:
            client = _REMOTE_REDIS_CLIENTS.get(url)
            if client is None:
                client = Redis.from_url(
                    url,
                    decode_responses=True,
                    socket_connect_timeout=0.2,
                    socket_timeout=0.2,
                )
                _REMOTE_REDIS_CLIENTS[url] = client
            return client
    except Exception:  # noqa: BLE001
        return None


def _remote_team_subscription(
    tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Any | None:
    """Subscribe to one team's cross-process wake-up channel."""
    client = _remote_redis_client()
    if client is None:
        return None
    try:
        subscription = client.pubsub()
        subscription.subscribe(_activity_channel_name(tenant_id, user_id, conversation_id))
        return subscription
    except Exception:  # noqa: BLE001
        return None


def _activity_channel(
    tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> _TeamActivity:
    """Return the notification channel for one tenant-scoped team."""
    key = (tenant_id, user_id, conversation_id)
    with _ACTIVITY_LOCK:
        channel = _ACTIVITY_CHANNELS.get(key)
        if channel is None:
            channel = _TeamActivity()
            _ACTIVITY_CHANNELS[key] = channel
        return channel


def notify_team_change(
    *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> None:
    """Wake local waiters after a committed team change.

    The database event log remains authoritative. This notification is only a
    low-latency wake-up and is deliberately emitted after commit, so a waiter
    can never observe an uncommitted team state as a successful change.
    """
    channel = _activity_channel(tenant_id, user_id, conversation_id)
    client = _remote_redis_client()
    if client is not None:
        try:
            client.publish(
                _activity_channel_name(tenant_id, user_id, conversation_id),
                "committed",
            )
        except Exception:  # noqa: BLE001
            # The database commit is authoritative. A Redis outage only
            # removes the low-latency cross-process wake-up.
            pass
    with channel.condition:
        channel.generation += 1
        channel.condition.notify_all()


class AgentTeamError(ValueError):
    """A team operation was refused."""


class StaleTaskRevisionError(AgentTeamError):
    """A task update named a revision that is no longer the current one."""


@dataclass(frozen=True, slots=True)
class TeamMemberRecord:
    """One teammate and the composition it runs under."""

    id: uuid.UUID
    name: str
    provider: str | None
    model: str | None
    persona: str | None
    tool_filter: dict[str, list[str]]
    descriptor: dict[str, Any]
    state: str
    child_id: uuid.UUID | None
    created_at: datetime | None
    #: The teammate's last answer, when it has worked.
    result: str | None = None
    #: Whether this record was returned from an idempotent replay.
    replayed: bool = False
    #: The last turn outcome, separate from the current lifecycle state.
    outcome: str = "pending"

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this teammate."""
        lifecycle = {
            TEAM_MEMBER_STATE_RUNNING: "running",
            TEAM_MEMBER_STATE_IDLE: "waiting",
            TEAM_MEMBER_STATE_FINISHED: "stopped",
        }.get(self.state, "unknown")
        return {
            "id": str(self.id),
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "persona": self.persona,
            "tool_filter": list(self.tool_filter.get("allow", [])),
            "state": self.state,
            "lifecycle": lifecycle,
            "outcome": self.outcome,
            "child_id": str(self.child_id) if self.child_id else None,
            "result": self.result,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "replayed": self.replayed,
        }

    def roster_dict(self) -> dict[str, Any]:
        """Return the compact status projection used for coordination reads."""
        return {
            "id": str(self.id),
            "name": self.name,
            "state": self.state,
            "lifecycle": {
                TEAM_MEMBER_STATE_RUNNING: "running",
                TEAM_MEMBER_STATE_IDLE: "waiting",
                TEAM_MEMBER_STATE_FINISHED: "stopped",
            }.get(self.state, "unknown"),
            "outcome": self.outcome,
            "child_id": str(self.child_id) if self.child_id else None,
        }


@dataclass(frozen=True, slots=True)
class TeamMessageRecord:
    """One message between two members."""

    id: uuid.UUID
    sender: str
    recipient: str
    body: str
    state: str
    created_at: datetime | None
    #: Whether this record was returned from an idempotent replay.
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this message."""
        return {
            "id": str(self.id),
            "from": self.sender,
            "to": self.recipient,
            "body": self.body,
            "state": self.state,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class TeamTaskRecord:
    """One task on the shared board."""

    id: uuid.UUID
    title: str
    body: str
    state: str
    owner: str | None
    revision: int
    depends_on: list[str]
    created_at: datetime | None
    updated_at: datetime | None
    write_scopes: list[str] = field(default_factory=list)
    ready: bool = True
    blocked_by: list[str] = field(default_factory=list)
    write_scope_warnings: list[str] = field(default_factory=list)
    #: Whether this record was returned from an idempotent replay.
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this task."""
        return {
            "id": str(self.id),
            "title": self.title,
            "body": self.body,
            "status": self.state,
            "owner": self.owner,
            "revision": self.revision,
            "depends_on": list(self.depends_on),
            "write_scopes": list(self.write_scopes),
            "ready": self.ready,
            "blocked_by": list(self.blocked_by),
            "write_scope_warnings": list(self.write_scope_warnings),
            "replayed": self.replayed,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(frozen=True, slots=True)
class TeamChange:
    """What changed in a team while a caller waited for it."""

    #: The member, message, and task changes, in the order they were seen.
    changed: list[dict[str, Any]] = field(default_factory=list)
    #: The team's members as they stand after the wait.
    members: list[TeamMemberRecord] = field(default_factory=list)
    #: Whether the wait ran out before anything changed.
    timed_out: bool = False
    #: True when no new committed team revision can produce progress.
    no_progress: bool = False
    #: The latest durable team event sequence observed by the waiter.
    revision: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable projection of this change."""
        return {
            "changed": list(self.changed),
            "members": [member.as_dict() for member in self.members],
            "timed_out": self.timed_out,
            "no_progress": self.no_progress,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """The state of a team at one moment."""

    members: dict[str, tuple[str, str]]
    messages: dict[str, tuple[str, str]]
    tasks: dict[str, tuple[str, str | None, int]]
    member_rows: list[TeamMember] = field(default_factory=list)


def _name_list(value: object) -> list[str]:
    """Read a list of tool names out of a stored filter.

    Args:
        value: The stored value, which may be absent or malformed.

    Returns:
        The names, or an empty list when the value is not a list.
    """
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and str(item)]


def _id_list(value: object) -> list[str]:
    """Read a list of task ids out of a stored value.

    Args:
        value: The stored value, which may be absent or malformed.

    Returns:
        The ids, or an empty list when the value is not a list.
    """
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and str(item)]


def _tool_filter(raw: object) -> dict[str, list[str]]:
    """Read the tool filter of a teammate.

    Args:
        raw: The stored filter, which may be absent or malformed.

    Returns:
        The ``allow`` and ``deny`` name lists, each possibly empty.
    """
    if not isinstance(raw, dict):
        return {"allow": [], "deny": []}
    return {"allow": _name_list(raw.get("allow")), "deny": _name_list(raw.get("deny"))}


def _member_record(
    member: TeamMember,
    *,
    result: str | None = None,
    replayed: bool = False,
    outcome: str | None = None,
) -> TeamMemberRecord:
    """Project one teammate row.

    Args:
        member: The teammate row.
        result: The teammate's last answer, when it has worked.

    Returns:
        The projected teammate.
    """
    descriptor = member.descriptor if isinstance(member.descriptor, dict) else {}
    return TeamMemberRecord(
        id=member.id,
        name=member.name,
        provider=member.provider,
        model=member.model,
        persona=member.persona,
        tool_filter=_tool_filter(member.tool_filter),
        descriptor=dict(descriptor),
        state=member.state,
        child_id=member.child_id,
        created_at=member.created_at,
        result=result,
        replayed=replayed,
        outcome=outcome
        or ("succeeded" if member.state == TEAM_MEMBER_STATE_FINISHED else "pending"),
    )


def _message_record(message: TeamMessage, *, replayed: bool = False) -> TeamMessageRecord:
    """Project one message row."""
    return TeamMessageRecord(
        id=message.id,
        sender=message.sender,
        recipient=message.recipient,
        body=message.body,
        state=message.state,
        created_at=message.created_at,
        replayed=replayed,
    )


def scopes_overlap(left: str, right: str) -> bool:
    """Whether two normalized directory prefixes overlap on path components."""
    left_scope = left.strip().strip("/")
    right_scope = right.strip().strip("/")
    return (
        left_scope == right_scope
        or left_scope.startswith(f"{right_scope}/")
        or right_scope.startswith(f"{left_scope}/")
    )


def _task_record(
    task: TeamTask,
    *,
    ready: bool = True,
    blocked_by: list[str] | None = None,
    write_scope_warnings: list[str] | None = None,
    replayed: bool = False,
) -> TeamTaskRecord:
    """Project one task row."""
    return TeamTaskRecord(
        id=task.id,
        title=task.title,
        body=task.body,
        state=task.state,
        owner=task.owner,
        revision=int(task.revision or 0),
        depends_on=_id_list(task.depends_on),
        created_at=task.created_at,
        updated_at=task.updated_at,
        write_scopes=_name_list(getattr(task, "write_scopes", None)),
        ready=ready,
        blocked_by=list(blocked_by or []),
        write_scope_warnings=list(write_scope_warnings or []),
        replayed=replayed,
    )


def _normalize_member_name(name: str) -> str:
    """Normalize the name a member is addressed by.

    Args:
        name: The name the caller supplied.

    Returns:
        The normalized name.

    Raises:
        AgentTeamError: When the name is empty, too long, or not a plain
            identifier.
    """
    normalized = str(name or "").strip()
    if not normalized:
        raise AgentTeamError("A teammate needs a name.")
    if len(normalized) > MAX_MEMBER_NAME_CHARS:
        raise AgentTeamError(
            f"A teammate name may be at most {MAX_MEMBER_NAME_CHARS} characters long."
        )
    if not MEMBER_NAME_PATTERN.match(normalized):
        raise AgentTeamError(
            "A teammate name may hold letters, digits, '-' and '_' only, and it starts "
            "with a letter or a digit."
        )
    return normalized


class AgentTeamService:
    """The named teammates, their messages, and their shared task board.

    The reads refresh the rows they return, because a teammate, a message, and
    a task may change while the caller's session is open, and a stale row
    would report a teammate that has already moved on.
    """

    def __init__(self, db: Session) -> None:
        """Create the service.

        Args:
            db: The database session.
        """
        self.db = db
        self.events = SessionEventStore(db)

    def _team_revision(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> int:
        """Read the latest committed team event sequence."""
        revision = self.db.execute(
            select(func.max(DeepSpaceSessionEvent.seq)).where(
                DeepSpaceSessionEvent.tenant_id == tenant_id,
                DeepSpaceSessionEvent.user_id == user_id,
                DeepSpaceSessionEvent.conversation_id == conversation_id,
                DeepSpaceSessionEvent.event_type.in_(TEAM_EVENT_TYPES),
            )
        ).scalar()
        return int(revision or 0)

    def _idempotent_entity_id(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        event_type: str,
        idempotency_key: str | None,
    ) -> uuid.UUID | None:
        """Find an entity created by an earlier request with the same key."""
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        rows = (
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.tenant_id == tenant_id,
                    DeepSpaceSessionEvent.user_id == user_id,
                    DeepSpaceSessionEvent.conversation_id == conversation_id,
                    DeepSpaceSessionEvent.event_type == event_type,
                )
                .order_by(DeepSpaceSessionEvent.seq.desc())
            )
            .scalars()
            .all()
        )
        for event in rows:
            payload = event.payload if isinstance(event.payload, dict) else {}
            if payload.get("idempotency_key") != key:
                continue
            try:
                return uuid.UUID(str(payload.get("entity_id")))
            except (TypeError, ValueError):
                return None
        return None

    def team_revision(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> int:
        """Return the durable revision used by coordination reads."""
        return self._team_revision(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )

    def _commit_event(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        run_id: uuid.UUID | None = None,
        tool_name: str | None = None,
    ) -> DeepSpaceSessionEvent:
        """Commit one state change and its event, then wake local waiters."""
        try:
            event = self.events.append(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                event_type=event_type,
                payload=payload,
                run_id=run_id,
                tool_name=tool_name,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        notify_team_change(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
        return event

    def _commit_events(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        events: list[tuple[str, dict[str, Any], str | None]],
        run_id: uuid.UUID | None = None,
    ) -> None:
        """Commit several state events atomically and wake local waiters once."""
        if not events:
            return
        try:
            for event_type, payload, tool_name in events:
                self.events.append(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    event_type=event_type,
                    payload=payload,
                    run_id=run_id,
                    tool_name=tool_name,
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        notify_team_change(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)

    # ------------------------------------------------------------------
    # Members
    # ------------------------------------------------------------------

    def spawn_member(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        name: str,
        provider: str | None = None,
        model: str | None = None,
        persona: str | None = None,
        tool_filter: list[str] | None = None,
        state: str = TEAM_MEMBER_STATE_IDLE,
        max_members: int | None = None,
        run_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> TeamMemberRecord:
        """Create a named teammate of a conversation.

        The teammate's composition is prepared with the delegation machinery,
        so it carries the same descriptor a delegated child carries and a later
        turn can rebuild it.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the teammate belongs to.
            name: The name the agent addresses the teammate by.
            provider: The provider route the teammate runs under.
            model: The model the teammate runs under.
            persona: The persona that shapes how the teammate works.
            tool_filter: The only tools the teammate may use.
            state: The state the teammate starts in.
            max_members: The largest number of teammates the conversation may
                hold, when the deployment sets one.
            run_id: The run that asked for the teammate.
            idempotency_key: Optional stable key used to replay a retried create.

        Returns:
            The created teammate.

        Raises:
            AgentTeamError: When the name is unusable, when a teammate with
                that name already exists, or when the conversation already
                holds as many teammates as it may.
        """
        normalized_name = _normalize_member_name(name)
        existing_id = self._idempotent_entity_id(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=TEAM_MEMBER_SPAWNED,
            idempotency_key=idempotency_key,
        )
        if existing_id is not None:
            existing_member = self.db.execute(
                select(TeamMember).where(
                    TeamMember.id == existing_id,
                    TeamMember.tenant_id == tenant_id,
                    TeamMember.user_id == user_id,
                    TeamMember.conversation_id == conversation_id,
                )
            ).scalar_one_or_none()
            if existing_member is not None:
                return _member_record(existing_member, replayed=True)
        if state not in TEAM_MEMBER_STATES:
            raise AgentTeamError(
                f"A teammate state must be one of {', '.join(TEAM_MEMBER_STATES)}."
            )
        existing = self._member_by_name(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            name=normalized_name,
        )
        if existing is not None:
            raise AgentTeamError(
                f"A teammate named {normalized_name!r} is already in this conversation."
            )
        if max_members is not None:
            held = len(
                self.list_members(
                    tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
                )
            )
            if held >= max_members:
                raise AgentTeamError(
                    f"This conversation already holds the maximum of {max_members} teammates."
                )
        allow = _name_list(list(tool_filter or []))
        filter_payload: dict[str, list[str]] = {"allow": allow, "deny": []}
        trimmed_persona = str(persona)[:MAX_PERSONA_CHARS] if persona else None
        descriptor, child_id = self._descriptor(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            name=normalized_name,
            provider=provider,
            model=model,
            persona=trimmed_persona,
            tool_filter=filter_payload,
            run_id=run_id,
        )
        member = TeamMember(
            id=generate_uuid7_with_fallback(),
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            name=normalized_name,
            provider=provider,
            model=model,
            persona=trimmed_persona,
            tool_filter=filter_payload,
            descriptor=descriptor,
            child_id=child_id,
            state=state,
        )
        self.db.add(member)
        self._commit_event(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=TEAM_MEMBER_SPAWNED,
            payload={
                "entity_id": str(member.id),
                "member_id": str(member.id),
                "name": member.name,
                "provider": member.provider,
                "model": member.model,
                "state": member.state,
                "idempotency_key": str(idempotency_key or "").strip() or None,
            },
            run_id=run_id,
            tool_name="spawn_teammate",
        )
        return _member_record(member)

    def _descriptor(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        name: str,
        provider: str | None,
        model: str | None,
        persona: str | None,
        tool_filter: dict[str, list[str]],
        run_id: uuid.UUID | None = None,
    ) -> tuple[dict[str, Any], uuid.UUID]:
        """Prepare a teammate's descriptor with the delegation machinery.

        The teammate is described exactly as a delegated child is, so the
        composition that runs it is the one the delegation code already knows.
        The child run carries the run that created the teammate as its
        parent, so the teammate's place in the conversation's delegation tree
        is recorded the same way a delegated child's is.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the teammate belongs to.
            name: The name the agent addresses the teammate by.
            provider: The provider route the teammate runs under.
            model: The model the teammate runs under.
            persona: The persona that shapes how the teammate works.
            tool_filter: The tools the teammate may use.
            run_id: The run that created the teammate.

        Returns:
            The descriptor payload, and the id of the child run that runs the
            teammate.
        """
        from app.deepspace.services.subagent_runner import (
            CHILD_STATUS_IDLE,
            build_child_descriptor,
            new_child_record,
        )

        child = new_child_record(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            parent_run_id=run_id,
            parent_agent_id=None,
            origin="subagent",
            mode="continuable",
            label=name,
            delegation_depth=1,
            agent_provider=provider,
            agent_model=model,
            reasoning_effort=None,
            persona=persona,
            tool_filter=dict(tool_filter),
            # The teammate waits for a message before it works, so it is not
            # running yet.
            status=CHILD_STATUS_IDLE,
        )
        # The child run is what the worker runs, so it is persisted with the
        # teammate.
        self.db.add(child)
        # TeamMember.child_id is a database foreign key, and there is no ORM
        # relationship for SQLAlchemy to use when ordering these inserts.
        # Flush the parent row before spawn_member adds the member row so the
        # atomic state/event commit cannot violate that foreign key.
        self.db.flush([child])
        return build_child_descriptor(child), child.id

    def list_members(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[TeamMemberRecord]:
        """List the teammates of a conversation.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The teammates, oldest first.
        """
        rows = (
            self.db.execute(
                select(TeamMember)
                .execution_options(populate_existing=True)
                .where(
                    TeamMember.tenant_id == tenant_id,
                    TeamMember.user_id == user_id,
                    TeamMember.conversation_id == conversation_id,
                )
                .order_by(TeamMember.created_at.asc())
            )
            .scalars()
            .all()
        )
        details = self._member_details(rows)
        return [
            _member_record(
                member,
                result=details.get(member.id, (None, None))[0],
                outcome=details.get(member.id, (None, None))[1],
            )
            for member in rows
        ]

    def _member_details(
        self, members: Sequence[TeamMember]
    ) -> dict[uuid.UUID, tuple[str | None, str | None]]:
        """Read the last answer and outcome of every teammate that has worked.

        The answer lives on the child run that works the teammate, so the
        teammate's projection shows what the teammate last reported.

        Args:
            members: The teammates to read the answers of.

        Returns:
            Answer and outcome pairs, keyed by teammate id.
        """
        members_by_child = {
            member.child_id: member.id for member in members if member.child_id is not None
        }
        if not members_by_child:
            return {}
        rows = self.db.execute(
            select(
                DeepSpaceSubagentRun.id,
                DeepSpaceSubagentRun.result_text,
                DeepSpaceSubagentRun.status,
                DeepSpaceSubagentRun.stop_reason,
                DeepSpaceSubagentRun.cancel_requested,
            ).where(DeepSpaceSubagentRun.id.in_(list(members_by_child)))
        ).all()
        details: dict[uuid.UUID, tuple[str | None, str | None]] = {}
        for child_id, text, status, stop_reason, cancel_requested in rows:
            if child_id not in members_by_child:
                continue
            outcome = "pending"
            if bool(cancel_requested) or str(stop_reason or "") in {"cancelled", "interrupted"}:
                outcome = "cancelled"
            elif str(status or "") == "failed" or str(stop_reason or "") in {
                "failed",
                "error",
                "lease_expired",
            }:
                outcome = "failed"
            elif str(stop_reason or "") == "completed":
                outcome = "succeeded"
            details[members_by_child[child_id]] = (
                str(text)[:MAX_RESULT_CHARS] if text else None,
                outcome,
            )
        return details

    def member_child_id(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        member_name: str,
    ) -> uuid.UUID | None:
        """Read the child run that works one teammate.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the teammate belongs to.
            member_name: The name of the teammate.

        Returns:
            The child run's id, or None when the conversation has no such
            teammate or the teammate has no child run.
        """
        try:
            member = self._require_member(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                member_name=member_name,
            )
        except AgentTeamError:
            return None
        return member.child_id

    def set_member_state(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        member_name: str,
        state: str,
    ) -> TeamMemberRecord:
        """Set the state of a teammate.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the teammate belongs to.
            member_name: The name of the teammate.
            state: ``idle``, ``running``, or ``finished``.

        Returns:
            The teammate as it stands.

        Raises:
            AgentTeamError: When the state is not one the product accepts, or
                when no teammate of that name is in the conversation.
        """
        if state not in TEAM_MEMBER_STATES:
            raise AgentTeamError(
                f"A teammate state must be one of {', '.join(TEAM_MEMBER_STATES)}."
            )
        member = self._require_member(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            member_name=member_name,
        )
        member.state = state
        self._commit_event(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=TEAM_MEMBER_STATE,
            payload={"member_id": str(member.id), "name": member.name, "state": state},
            tool_name="team_member_state",
        )
        return _member_record(member)

    def resume_member(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        member_name: str,
        run_id: uuid.UUID | None = None,
    ) -> tuple[TeamMemberRecord, list[TeamMessageRecord]]:
        """Resume a teammate and hand it the messages that waited for it.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the teammate belongs to.
            member_name: The name of the teammate.
            run_id: The run that resumed the teammate.

        Returns:
            The resumed teammate and the messages it received.

        Raises:
            AgentTeamError: When no teammate of that name is in the conversation.
        """
        member = self.set_member_state(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            member_name=member_name,
            state=TEAM_MEMBER_STATE_RUNNING,
        )
        delivered = self.deliver_pending(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            member_name=member.name,
            run_id=run_id,
        )
        return member, delivered

    def recover_stale_members(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        stale_after_seconds: int,
    ) -> list[TeamMemberRecord]:
        """Release teammates whose worker lease expired.

        The teammate and child rows, the recovery event, and the worker's
        reusable queue state are committed together. A later dispatch can
        claim the still-pending messages, so a worker crash does not lose
        work or leave the roster permanently running.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the team belongs to.
            stale_after_seconds: The configured worker lease duration.

        Returns:
            The teammates released by this call.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=max(60, int(stale_after_seconds)))
        members = (
            self.db.execute(
                select(TeamMember)
                .execution_options(populate_existing=True)
                .where(
                    TeamMember.tenant_id == tenant_id,
                    TeamMember.user_id == user_id,
                    TeamMember.conversation_id == conversation_id,
                    TeamMember.state == TEAM_MEMBER_STATE_RUNNING,
                    TeamMember.updated_at < cutoff,
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if not members:
            return []
        events: list[tuple[str, dict[str, Any], str | None]] = []
        recovered: list[TeamMemberRecord] = []
        for member in members:
            child = None
            if member.child_id is not None:
                child = self.db.execute(
                    select(DeepSpaceSubagentRun)
                    .execution_options(populate_existing=True)
                    .where(
                        DeepSpaceSubagentRun.id == member.child_id,
                        DeepSpaceSubagentRun.tenant_id == tenant_id,
                        DeepSpaceSubagentRun.user_id == user_id,
                        DeepSpaceSubagentRun.conversation_id == conversation_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
            member.state = TEAM_MEMBER_STATE_IDLE
            if child is not None:
                child.busy = False
                if child.status == "running":
                    child.status = "idle"
                child.stop_reason = "lease_expired"
                child.last_error = "stale_team_worker_recovered"
            events.append(
                (
                    TEAM_MEMBER_STATE,
                    {
                        "entity_id": str(member.id),
                        "member_id": str(member.id),
                        "name": member.name,
                        "state": member.state,
                        "stop_reason": "lease_expired",
                        "recovered": True,
                    },
                    "team_member_state",
                )
            )
            recovered.append(_member_record(member))
        self._commit_events(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            events=events,
        )
        return recovered

    def _member_by_name(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        name: str,
    ) -> TeamMember | None:
        """Read one teammate of a conversation by its name.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the teammate belongs to.
            name: The name of the teammate.

        Returns:
            The teammate row, or None when the conversation has no such
            teammate.
        """
        return self.db.execute(
            select(TeamMember)
            .execution_options(populate_existing=True)
            .where(
                TeamMember.tenant_id == tenant_id,
                TeamMember.user_id == user_id,
                TeamMember.conversation_id == conversation_id,
                TeamMember.name == name,
            )
        ).scalar_one_or_none()

    def _require_member(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        member_name: str,
    ) -> TeamMember:
        """Read one teammate of a conversation, or refuse the operation.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the teammate belongs to.
            member_name: The name of the teammate.

        Returns:
            The teammate row.

        Raises:
            AgentTeamError: When the conversation has no teammate of that name.
        """
        normalized = _normalize_member_name(member_name)
        member = self._member_by_name(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            name=normalized,
        )
        if member is None:
            raise AgentTeamError(f"No teammate named {normalized!r} is in this conversation.")
        return member

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def send_message(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        to_name: str,
        body: str,
        sender: str = "agent",
        run_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> TeamMessageRecord:
        """Send a message to a teammate.

        A message for a teammate that is running is delivered at once.  A
        message for a teammate that is not running is queued, so it waits for
        the teammate instead of being lost.  Either way the message waits in
        the queue of the child run that works the teammate, so the teammate's
        next turn answers it.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the message belongs to.
            to_name: The name of the teammate the message is for.
            body: The message text.
            sender: The name of the sender, or ``agent`` for the agent that
                owns the team.
            run_id: The run that sent the message.
            idempotency_key: Optional stable key used to replay a retried send.

        Returns:
            The recorded message.

        Raises:
            AgentTeamError: When the message has no body, when the body is
                too long, when the teammate already has as many messages
                waiting as it may hold, or when the conversation has no
                teammate of that name.
        """
        text = str(body or "").strip()
        if not text:
            raise AgentTeamError("A message needs a body.")
        if len(text) > MAX_MESSAGE_CHARS:
            raise AgentTeamError(f"A message may be at most {MAX_MESSAGE_CHARS} characters long.")
        member = self._require_member(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            member_name=to_name,
        )
        existing_id = self._idempotent_entity_id(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=TEAM_MESSAGE,
            idempotency_key=idempotency_key,
        )
        if existing_id is not None:
            existing_message = self.db.execute(
                select(TeamMessage).where(
                    TeamMessage.id == existing_id,
                    TeamMessage.tenant_id == tenant_id,
                    TeamMessage.user_id == user_id,
                    TeamMessage.conversation_id == conversation_id,
                )
            ).scalar_one_or_none()
            if existing_message is not None:
                return _message_record(existing_message, replayed=True)
        pending = self._pending_for_child(member)
        if len(pending) >= MAX_PENDING_MESSAGES:
            raise AgentTeamError(
                f"{member.name} already has {len(pending)} messages waiting for its turn."
            )
        delivered = member.state == TEAM_MEMBER_STATE_RUNNING
        message = TeamMessage(
            id=generate_uuid7_with_fallback(),
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            sender=str(sender or "agent"),
            recipient=member.name,
            body=text,
            state=TEAM_MESSAGE_STATE_DELIVERED if delivered else TEAM_MESSAGE_STATE_QUEUED,
            delivered_at=datetime.now(UTC) if delivered else None,
        )
        self.db.add(message)
        # The teammate's own turn queue is where the message waits, so a
        # message that reaches a teammate that is away is answered when the
        # teammate works rather than being lost.
        self._queue_for_child(member, message)
        self._append_message_event(message, run_id=run_id, idempotency_key=idempotency_key)
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        notify_team_change(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
        return _message_record(message)

    def _pending_for_child(self, member: TeamMember) -> list[dict[str, Any]]:
        """Read the messages that wait for one teammate's turn.

        The child row is read under a row lock, so a message that is queued
        while the teammate's turn claims the queue is not lost.

        Args:
            member: The teammate to read the queue of.

        Returns:
            The queued messages, oldest first.
        """
        if member.child_id is None:
            return []
        raw = self.db.execute(
            select(DeepSpaceSubagentRun.pending_messages)
            .where(
                DeepSpaceSubagentRun.id == member.child_id,
                DeepSpaceSubagentRun.tenant_id == member.tenant_id,
                DeepSpaceSubagentRun.user_id == member.user_id,
                DeepSpaceSubagentRun.conversation_id == member.conversation_id,
            )
            .with_for_update()
        ).scalar()
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _queue_for_child(self, member: TeamMember, message: TeamMessage) -> None:
        """Put one message in the queue of the teammate that works a child run.

        Args:
            member: The teammate the message is for.
            message: The message that waits for the teammate.
        """
        if member.child_id is None:
            return
        pending = self._pending_for_child(member)
        pending.append({"id": str(message.id), "message": message.body})
        self.db.execute(
            update(DeepSpaceSubagentRun)
            .where(
                DeepSpaceSubagentRun.id == member.child_id,
                DeepSpaceSubagentRun.tenant_id == member.tenant_id,
            )
            .values(pending_messages=pending)
        )

    def pending_messages(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        member_name: str,
    ) -> list[TeamMessageRecord]:
        """List the messages that wait for a teammate.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the messages belong to.
            member_name: The name of the teammate.

        Returns:
            The queued messages, oldest first.
        """
        return [
            _message_record(message)
            for message in self._queued_rows(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                member_name=member_name,
            )
        ]

    def list_messages(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        limit: int = 100,
    ) -> list[TeamMessageRecord]:
        """List the most recent messages shared by a conversation's team.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.
            limit: The maximum number of recent messages to return.

        Returns:
            Recent messages in chronological order.
        """
        bounded_limit = max(1, min(int(limit), 200))
        rows = (
            self.db.execute(
                select(TeamMessage)
                .execution_options(populate_existing=True)
                .where(
                    TeamMessage.tenant_id == tenant_id,
                    TeamMessage.user_id == user_id,
                    TeamMessage.conversation_id == conversation_id,
                )
                .order_by(TeamMessage.created_at.desc())
                .limit(bounded_limit)
            )
            .scalars()
            .all()
        )
        return [_message_record(message) for message in reversed(rows)]

    def _queued_rows(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        member_name: str,
    ) -> list[TeamMessage]:
        """Read the messages that wait for a teammate.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the messages belong to.
            member_name: The name of the teammate.

        Returns:
            The queued message rows, oldest first.
        """
        normalized = str(member_name or "").strip()
        return list(
            self.db.execute(
                select(TeamMessage)
                .execution_options(populate_existing=True)
                .where(
                    TeamMessage.tenant_id == tenant_id,
                    TeamMessage.user_id == user_id,
                    TeamMessage.conversation_id == conversation_id,
                    TeamMessage.recipient == normalized,
                    TeamMessage.state == TEAM_MESSAGE_STATE_QUEUED,
                )
                .order_by(TeamMessage.created_at.asc())
            )
            .scalars()
            .all()
        )

    def deliver_pending(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        member_name: str,
        run_id: uuid.UUID | None = None,
    ) -> list[TeamMessageRecord]:
        """Deliver the messages that waited for a teammate.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the messages belong to.
            member_name: The name of the teammate.
            run_id: The run that delivered the messages.

        Returns:
            The delivered messages, oldest first.
        """
        rows = self._queued_rows(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            member_name=member_name,
        )
        if not rows:
            return []
        now = datetime.now(UTC)
        for message in rows:
            message.state = TEAM_MESSAGE_STATE_DELIVERED
            message.delivered_at = now
        events = [
            (
                TEAM_MESSAGE,
                {
                    "entity_id": str(message.id),
                    "message_id": str(message.id),
                    "from": message.sender,
                    "to": message.recipient,
                    "state": message.state,
                },
                "team_send_message",
            )
            for message in rows
        ]
        self._commit_events(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            events=events,
            run_id=run_id,
        )
        return [_message_record(message) for message in rows]

    def _append_message_event(
        self,
        message: TeamMessage,
        *,
        run_id: uuid.UUID | None,
        idempotency_key: str | None = None,
    ) -> None:
        """Record one message in the conversation's typed log.

        Args:
            message: The message that was recorded.
            run_id: The run that sent the message.
        """
        self.events.append(
            tenant_id=message.tenant_id,
            user_id=message.user_id,
            conversation_id=message.conversation_id,
            event_type=TEAM_MESSAGE,
            payload={
                "entity_id": str(message.id),
                "message_id": str(message.id),
                "from": message.sender,
                "to": message.recipient,
                "state": message.state,
                "idempotency_key": str(idempotency_key or "").strip() or None,
            },
            run_id=run_id,
            tool_name="team_send_message",
        )

    # ------------------------------------------------------------------
    # The shared task board
    # ------------------------------------------------------------------

    def _project_tasks(self, tasks: list[TeamTask]) -> list[TeamTaskRecord]:
        """Enrich a list of tasks with DAG readiness and write scope warnings."""
        tasks_by_id = {str(task.id): task for task in tasks}
        records: list[TeamTaskRecord] = []
        for task in tasks:
            deps = _id_list(task.depends_on)
            uncompleted_deps = [
                dep_id
                for dep_id in deps
                if dep_id in tasks_by_id and tasks_by_id[dep_id].state != TEAM_TASK_STATE_COMPLETED
            ]
            ready = (task.state == TEAM_TASK_STATE_PENDING) and (len(uncompleted_deps) == 0)

            task_scopes = _name_list(getattr(task, "write_scopes", None))
            warnings: list[str] = []
            if task_scopes:
                for other in tasks:
                    if other.id == task.id or other.state != TEAM_TASK_STATE_IN_PROGRESS:
                        continue
                    other_scopes = _name_list(getattr(other, "write_scopes", None))
                    if any(scopes_overlap(s1, s2) for s1 in task_scopes for s2 in other_scopes):
                        warnings.append(f"write scopes overlap with {other.title or str(other.id)}")
            records.append(
                _task_record(
                    task,
                    ready=ready,
                    blocked_by=uncompleted_deps,
                    write_scope_warnings=warnings,
                )
            )
        return records

    def create_task(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        title: str,
        body: str | None = None,
        owner: str | None = None,
        depends_on: list[str] | None = None,
        write_scopes: list[str] | None = None,
        run_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> TeamTaskRecord:
        """Create a task on the shared board.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the task belongs to.
            title: The short title of the task.
            body: What the task is.
            owner: The name of the teammate that owns the task.
            depends_on: The ids of the tasks this one waits for.
            write_scopes: The advisory directory prefixes this task writes to.
            run_id: The run that created the task.

        Returns:
            The created task.

        Raises:
            AgentTeamError: When the title is missing, when the title or the
                body is too long, when the owner is not a teammate of the
                conversation, or when a dependency is not a task of the same
                conversation.
        """
        normalized_title = str(title or "").strip()
        if not normalized_title:
            raise AgentTeamError("A task needs a title.")
        if len(normalized_title) > MAX_TASK_TITLE_CHARS:
            raise AgentTeamError(
                f"A task title may be at most {MAX_TASK_TITLE_CHARS} characters long."
            )
        text = str(body or "").strip()
        if len(text) > MAX_TASK_BODY_CHARS:
            raise AgentTeamError(
                f"A task body may be at most {MAX_TASK_BODY_CHARS} characters long."
            )
        owner_name = str(owner or "").strip() or None
        if owner_name is not None:
            self._require_member(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                member_name=owner_name,
            )
        dependencies = [str(item).strip() for item in (depends_on or []) if str(item).strip()]
        for dependency in dependencies:
            if (
                self._task(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    task_id=dependency,
                )
                is None
            ):
                raise AgentTeamError(f"The task {dependency!r} is not a task of this conversation.")
        scopes = _name_list(write_scopes)
        existing_id = self._idempotent_entity_id(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=TEAM_TASK,
            idempotency_key=idempotency_key,
        )
        if existing_id is not None:
            existing_task = self.db.execute(
                select(TeamTask).where(
                    TeamTask.id == existing_id,
                    TeamTask.tenant_id == tenant_id,
                    TeamTask.user_id == user_id,
                    TeamTask.conversation_id == conversation_id,
                )
            ).scalar_one_or_none()
            if existing_task is not None:
                return self.get_task(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    task_id=str(existing_task.id),
                )
        task = TeamTask(
            id=generate_uuid7_with_fallback(),
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            title=normalized_title,
            body=text,
            state=TEAM_TASK_STATE_PENDING,
            owner=owner_name,
            revision=1,
            depends_on=dependencies,
            write_scopes=scopes,
        )
        self.db.add(task)
        self._append_task_event(
            task, action="created", run_id=run_id, idempotency_key=idempotency_key
        )
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        notify_team_change(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
        return self.get_task(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            task_id=str(task.id),
        )

    def get_task(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        task_id: str,
    ) -> TeamTaskRecord:
        """Read one task on the shared board with DAG readiness and warnings.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the task belongs to.
            task_id: The id of the task to read.

        Returns:
            The task record.

        Raises:
            AgentTeamError: When the task is not on the shared board.
        """
        all_tasks = list(
            self.db.execute(
                select(TeamTask)
                .execution_options(populate_existing=True)
                .where(
                    TeamTask.tenant_id == tenant_id,
                    TeamTask.user_id == user_id,
                    TeamTask.conversation_id == conversation_id,
                )
                .order_by(TeamTask.created_at.asc())
            )
            .scalars()
            .all()
        )
        target = next((t for t in all_tasks if str(t.id) == str(task_id)), None)
        if target is None:
            raise AgentTeamError(f"No task with id {task_id!r} is on the shared board.")
        projected = self._project_tasks(all_tasks)
        return next(p for p in projected if str(p.id) == str(target.id))

    def list_tasks(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[TeamTaskRecord]:
        """List the tasks on the shared board.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The tasks, oldest first.
        """
        rows = list(
            self.db.execute(
                select(TeamTask)
                .execution_options(populate_existing=True)
                .where(
                    TeamTask.tenant_id == tenant_id,
                    TeamTask.user_id == user_id,
                    TeamTask.conversation_id == conversation_id,
                )
                .order_by(TeamTask.created_at.asc())
            )
            .scalars()
            .all()
        )
        return self._project_tasks(rows)

    def update_task(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        task_id: str,
        revision: int,
        status: str | None = None,
        owner: str | None = None,
        body: str | None = None,
        write_scopes: list[str] | None = None,
        run_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> TeamTaskRecord:
        """Update a task on the shared board.

        The update names the revision it read, and it is refused when that
        revision is no longer the current one, so two members cannot silently
        overwrite each other.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the task belongs to.
            task_id: The id of the task to update.
            revision: The revision the caller read.
            status: The new state of the task.
            owner: The name of the teammate that owns the task.
            body: The new body of the task.
            write_scopes: The new write scopes of the task.
            run_id: The run that updated the task.

        Returns:
            The updated task, at its new revision.

        Raises:
            AgentTeamError: When the task is not a task of the conversation, or
                when the new state is not one the product accepts.
            StaleTaskRevisionError: When the named revision is not the current
                revision of the task.
        """
        task = self._task(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            task_id=task_id,
        )
        if task is None:
            raise AgentTeamError("No task with that id belongs to this conversation.")
        existing_id = self._idempotent_entity_id(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=TEAM_TASK,
            idempotency_key=idempotency_key,
        )
        if existing_id is not None and existing_id == task.id:
            return self.get_task(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                task_id=str(task.id),
            )
        current_revision = int(task.revision or 0)
        if int(revision) != current_revision:
            raise StaleTaskRevisionError(
                f"The task is at revision {current_revision}, not {int(revision)}. Read the "
                "task again and apply the change to what it says now."
            )
        if status is not None:
            if status not in TEAM_TASK_STATES:
                raise AgentTeamError(f"A task status must be one of {', '.join(TEAM_TASK_STATES)}.")
            task.state = status
        if owner is not None:
            owner_name = str(owner).strip()
            if owner_name:
                self._require_member(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    member_name=owner_name,
                )
            task.owner = owner_name or None
        if body is not None:
            text = str(body).strip()
            if len(text) > MAX_TASK_BODY_CHARS:
                raise AgentTeamError(
                    f"A task body may be at most {MAX_TASK_BODY_CHARS} characters long."
                )
            task.body = text
        if write_scopes is not None:
            task.write_scopes = _name_list(write_scopes)
        task.revision = current_revision + 1
        self._append_task_event(
            task, action="updated", run_id=run_id, idempotency_key=idempotency_key
        )
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        notify_team_change(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
        return self.get_task(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            task_id=str(task.id),
        )

    def interrupt_member(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        member_name: str,
        run_id: uuid.UUID | None = None,
    ) -> TeamMemberRecord:
        """Interrupt a running teammate and set its state to finished.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the teammate belongs to.
            member_name: The name of the teammate to interrupt.
            run_id: The run that asked for the interruption.

        Returns:
            The interrupted teammate record.

        Raises:
            AgentTeamError: When no teammate of that name is in the conversation.
        """
        member = self._require_member(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            member_name=_normalize_member_name(member_name),
        )
        member.state = TEAM_MEMBER_STATE_FINISHED
        if member.child_id:
            child = self.db.execute(
                select(DeepSpaceSubagentRun).where(DeepSpaceSubagentRun.id == member.child_id)
            ).scalar_one_or_none()
            if child is not None:
                child.cancel_requested = True
        self._commit_event(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            event_type=TEAM_MEMBER_INTERRUPTED,
            payload={"member_id": str(member.id), "name": member.name, "state": member.state},
        )
        return _member_record(member)

    def is_team_enabled(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        deployment_default: bool = True,
    ) -> bool:
        """Check whether agent teams are enabled for this conversation."""
        event = (
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.tenant_id == tenant_id,
                    DeepSpaceSessionEvent.user_id == user_id,
                    DeepSpaceSessionEvent.conversation_id == conversation_id,
                    DeepSpaceSessionEvent.event_type == TEAM_ENABLED,
                )
                .order_by(DeepSpaceSessionEvent.seq.desc())
            )
            .scalars()
            .first()
        )
        if event is not None and isinstance(event.payload, dict) and "enabled" in event.payload:
            return bool(event.payload["enabled"])
        return deployment_default

    def set_team_enabled(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        enabled: bool,
        run_id: uuid.UUID | None = None,
    ) -> bool:
        """Persist whether agent teams are enabled for this conversation."""
        self._commit_event(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            event_type=TEAM_ENABLED,
            payload={"enabled": bool(enabled)},
        )
        return bool(enabled)

    def _task(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        task_id: str,
    ) -> TeamTask | None:
        """Read one task of a conversation.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the task belongs to.
            task_id: The id of the task.

        Returns:
            The task row, or None when the conversation has no such task.
        """
        parsed = _parse_task_id(task_id)
        if parsed is None:
            return None
        return self.db.execute(
            select(TeamTask)
            .execution_options(populate_existing=True)
            .where(
                TeamTask.id == parsed,
                TeamTask.tenant_id == tenant_id,
                TeamTask.user_id == user_id,
                TeamTask.conversation_id == conversation_id,
            )
        ).scalar_one_or_none()

    def _append_task_event(
        self,
        task: TeamTask,
        *,
        action: str,
        run_id: uuid.UUID | None,
        idempotency_key: str | None = None,
    ) -> None:
        """Record one task change in the conversation's typed log.

        Args:
            task: The task that changed.
            action: ``created`` or ``updated``.
            run_id: The run that changed the task.
        """
        self.events.append(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            conversation_id=task.conversation_id,
            event_type=TEAM_TASK,
            payload={
                "entity_id": str(task.id),
                "task_id": str(task.id),
                "action": action,
                "status": task.state,
                "owner": task.owner,
                "revision": int(task.revision or 0),
                "idempotency_key": str(idempotency_key or "").strip() or None,
            },
            run_id=run_id,
            tool_name="team_task_update",
        )

    # ------------------------------------------------------------------
    # Waiting for a change
    # ------------------------------------------------------------------

    def wait_for_change(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        timeout_seconds: float = DEFAULT_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> TeamChange:
        """Wait for a committed team change without tight database polling.

        Local writers wake the condition after commit. Configured Redis
        deployments receive the same after-commit signal across workers. The
        durable event revision is checked on a coarse fallback interval only
        when no notifier is available.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to watch.
            timeout_seconds: How long the wait lasts.
            poll_interval: Legacy fallback interval; it is clamped to at
                least one second and does not control local or Redis
                notifications.

        Returns:
            The change that ended the wait, or an empty change when the wait
            ran out.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        channel = _activity_channel(tenant_id, user_id, conversation_id)
        fallback_seconds = max(1.0, float(poll_interval))
        remote_subscription = _remote_team_subscription(tenant_id, user_id, conversation_id)
        try:
            baseline = self._snapshot(
                tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
            )
            baseline_revision = self._team_revision(
                tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
            )
            observed_generation = channel.generation
            while True:
                current_revision = self._team_revision(
                    tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
                )
                current = self._snapshot(
                    tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
                )
                changes = _diff(baseline, current)
                members = [_member_record(member) for member in current.member_rows]
                if changes or current_revision != baseline_revision:
                    return TeamChange(
                        changed=changes,
                        members=members,
                        timed_out=False,
                        revision=current_revision,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return TeamChange(
                        changed=[],
                        members=members,
                        timed_out=True,
                        no_progress=channel.generation == observed_generation,
                        revision=current_revision,
                    )
                if remote_subscription is not None:
                    try:
                        remote_subscription.get_message(
                            ignore_subscribe_messages=True,
                            timeout=min(remaining, 30.0),
                        )
                        observed_generation = channel.generation
                        continue
                    except Exception:  # noqa: BLE001
                        try:
                            remote_subscription.close()
                        except Exception:  # noqa: BLE001
                            pass
                        remote_subscription = None
                # Check the generation while holding the condition before
                # sleeping. A local notification that arrived during the
                # database read therefore cannot be lost.
                with channel.condition:
                    if channel.generation != observed_generation:
                        observed_generation = channel.generation
                        continue
                    channel.condition.wait(timeout=min(remaining, fallback_seconds))
                    observed_generation = channel.generation
        finally:
            if remote_subscription is not None:
                try:
                    remote_subscription.close()
                except Exception:  # noqa: BLE001
                    pass

    def _snapshot(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> _Snapshot:
        """Read the state of a team at one moment.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The snapshot.
        """
        member_rows = (
            self.db.execute(
                select(TeamMember)
                .execution_options(populate_existing=True)
                .where(
                    TeamMember.tenant_id == tenant_id,
                    TeamMember.user_id == user_id,
                    TeamMember.conversation_id == conversation_id,
                )
                .order_by(TeamMember.created_at.asc())
            )
            .scalars()
            .all()
        )
        message_rows = (
            self.db.execute(
                select(TeamMessage)
                .execution_options(populate_existing=True)
                .where(
                    TeamMessage.tenant_id == tenant_id,
                    TeamMessage.user_id == user_id,
                    TeamMessage.conversation_id == conversation_id,
                )
                .order_by(TeamMessage.created_at.asc())
            )
            .scalars()
            .all()
        )
        task_rows = (
            self.db.execute(
                select(TeamTask)
                .execution_options(populate_existing=True)
                .where(
                    TeamTask.tenant_id == tenant_id,
                    TeamTask.user_id == user_id,
                    TeamTask.conversation_id == conversation_id,
                )
                .order_by(TeamTask.created_at.asc())
            )
            .scalars()
            .all()
        )
        return _Snapshot(
            members={str(member.id): (member.name, member.state) for member in member_rows},
            messages={
                str(message.id): (message.recipient, message.state) for message in message_rows
            },
            tasks={
                str(task.id): (task.state, task.owner, int(task.revision or 0))
                for task in task_rows
            },
            member_rows=list(member_rows),
        )


def _parse_task_id(task_id: str) -> uuid.UUID | None:
    """Parse the identity of a task.

    Args:
        task_id: The identity the caller supplied.

    Returns:
        The parsed identity, or None when it is not one.
    """
    try:
        return uuid.UUID(str(task_id).strip())
    except ValueError:
        return None


def _diff(baseline: _Snapshot, current: _Snapshot) -> list[dict[str, Any]]:
    """Report what changed between two snapshots of a team.

    Args:
        baseline: The state the waiter started from.
        current: The state the waiter reads now.

    Returns:
        One entry per change, in the order the records are read.
    """
    changes: list[dict[str, Any]] = []
    for member_id, (name, state) in current.members.items():
        previous_member = baseline.members.get(member_id)
        if previous_member is None:
            changes.append(
                {
                    "kind": "member",
                    "id": member_id,
                    "name": name,
                    "state": state,
                    "action": "added",
                }
            )
        elif previous_member[1] != state:
            changes.append(
                {
                    "kind": "member",
                    "id": member_id,
                    "name": name,
                    "state": state,
                    "action": "state",
                }
            )
    for message_id, (recipient, state) in current.messages.items():
        previous_message = baseline.messages.get(message_id)
        if previous_message is None:
            changes.append(
                {
                    "kind": "message",
                    "id": message_id,
                    "to": recipient,
                    "state": state,
                    "action": "added",
                }
            )
        elif previous_message[1] != state:
            changes.append(
                {
                    "kind": "message",
                    "id": message_id,
                    "to": recipient,
                    "state": state,
                    "action": "state",
                }
            )
    for task_id, (state, owner, revision) in current.tasks.items():
        previous_task = baseline.tasks.get(task_id)
        if previous_task is None:
            changes.append(
                {
                    "kind": "task",
                    "id": task_id,
                    "status": state,
                    "owner": owner,
                    "revision": revision,
                    "action": "added",
                }
            )
        elif previous_task != (state, owner, revision):
            changes.append(
                {
                    "kind": "task",
                    "id": task_id,
                    "status": state,
                    "owner": owner,
                    "revision": revision,
                    "action": "updated",
                }
            )
    return changes


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_WAIT_SECONDS",
    "MAX_MEMBER_NAME_CHARS",
    "MAX_MESSAGE_CHARS",
    "MAX_PERSONA_CHARS",
    "MAX_TASK_BODY_CHARS",
    "MAX_TASK_TITLE_CHARS",
    "AgentTeamError",
    "AgentTeamService",
    "StaleTaskRevisionError",
    "TeamChange",
    "TeamMemberRecord",
    "TeamMessageRecord",
    "TeamTaskRecord",
]
