"""Team tools for DeepSpace: named teammates, their messages, and their board.

A team is a set of named teammates that work the same task as the agent that
created them.  The agent addresses a teammate by its name, sends it messages
that wait for it while it is away, and hands work over on a task board every
teammate of the conversation shares.

The tools manage the durable team and hand a teammate's turn to the worker.
A teammate that is away resumes on the turn the message starts, and the
teammate's answer is read with ``list_teammates``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from app.deepspace.models.agent_team import TeamMember
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.agent_team import (
    AgentTeamError,
    AgentTeamService,
    StaleTaskRevisionError,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    define_native_tool,
    integer_property,
    require_db,
    string_property,
)

logger = logging.getLogger(__name__)

#: The longest a teammate may wait for a change.
MAX_WAIT_SECONDS = 300

#: How long a teammate waits when the caller names no timeout.
DEFAULT_WAIT_SECONDS = 30

#: The largest number of teammates a conversation may hold by default.
DEFAULT_MAX_MEMBERS = 8


class SpawnTeammateArguments(ToolArguments):
    """Create a named teammate of the current conversation."""

    name: str = Field(min_length=1, max_length=64)
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    persona: str | None = Field(default=None, max_length=4_000)
    tool_filter: list[str] | None = Field(default=None, max_length=64)
    task: str | None = Field(default=None, max_length=20_000)
    mode: Literal["fresh", "fork"] = "fresh"
    idempotency_key: str | None = Field(default=None, max_length=255)


class TeamSendMessageArguments(ToolArguments):
    """Send a message to a named teammate."""

    to: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=20_000)
    idempotency_key: str | None = Field(default=None, max_length=255)


class ListTeammatesArguments(ToolArguments):
    """List the named teammates of the current conversation."""


class TeamMemberGetArguments(ToolArguments):
    """Read one teammate's detailed result."""

    name: str = Field(min_length=1, max_length=64)


class WaitAgentArguments(ToolArguments):
    """Wait for a change in the current conversation's team."""

    timeout_seconds: int | None = Field(default=None, ge=1, le=MAX_WAIT_SECONDS)


class InterruptAgentArguments(ToolArguments):
    """Stop the current turn of a running teammate or child agent."""

    target: str | None = Field(default=None, min_length=1, max_length=64)
    agent_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _validate_target_or_agent(self) -> InterruptAgentArguments:
        if not self.target and not self.agent_id:
            raise ValueError("Either target or agent_id must be provided to interrupt_agent.")
        return self


class TeamTaskCreateArguments(ToolArguments):
    """Create a task on the shared team board."""

    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=20_000)
    depends_on: list[str] | None = Field(default=None, max_length=32)
    write_scopes: list[str] | None = Field(default=None, max_items=32)
    idempotency_key: str | None = Field(default=None, max_length=255)


class TeamTaskListArguments(ToolArguments):
    """List the tasks on the shared team board."""


class TeamTaskGetArguments(ToolArguments):
    """Get full details of a specific task on the shared team board."""

    task_id: str = Field(min_length=1, max_length=64)


class TeamTaskUpdateArguments(ToolArguments):
    """Update a task on the shared team board."""

    task_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    status: Literal["pending", "in_progress", "blocked", "completed"] | None = None
    owner: str | None = Field(default=None, max_length=64)
    body: str | None = Field(default=None, max_length=20_000)
    write_scopes: list[str] | None = Field(default=None, max_items=32)
    idempotency_key: str | None = Field(default=None, max_length=255)


def _service(context: ToolContext) -> AgentTeamService:
    """Return the team service for the calling request.

    Args:
        context: The calling request's tool context.

    Returns:
        The service.
    """
    return AgentTeamService(require_db(context))


def _team_enabled(context: ToolContext) -> bool:
    """Report whether this deployment offers teams.

    Args:
        context: The calling request's tool context.

    Returns:
        True when the conversation may use the team tools.
    """
    default_enabled = bool(getattr(context.settings, "deepspace_agent_team_enabled", True))
    try:
        if context.conversation_id:
            return _service(context).is_team_enabled(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                deployment_default=default_enabled,
            )
    except Exception:
        pass
    return default_enabled


def _max_members(context: ToolContext) -> int:
    """Read the deployment's teammate cap.

    Args:
        context: The calling request's tool context.

    Returns:
        The largest number of teammates a conversation may hold.
    """
    return int(getattr(context.settings, "deepspace_agent_team_max_members", DEFAULT_MAX_MEMBERS))


def _recover_stale_teammates(context: ToolContext, service: AgentTeamService) -> list[str]:
    """Release expired workers and re-dispatch their durable queues."""
    recovered = service.recover_stale_members(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
        stale_after_seconds=int(
            getattr(context.settings, "deepspace_agent_team_lease_seconds", 1_800)
        ),
    )
    names = [member.name for member in recovered]
    for name in names:
        _dispatch_teammate_turn(context, member_name=name, body="")
    return names


def _disabled() -> dict[str, Any]:
    """Return the refusal a deployment without teams gives.

    Returns:
        The tool result.
    """
    return {
        "status": "error",
        "message": "This deployment does not offer agent teams.",
    }


async def _spawn_teammate(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Create a named teammate of the current conversation."""
    if not _team_enabled(context):
        return _disabled()
    try:
        member = _service(context).spawn_member(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            name=str(arguments["name"]),
            provider=arguments.get("provider"),
            model=arguments.get("model"),
            persona=arguments.get("persona"),
            tool_filter=list(arguments.get("tool_filter") or []),
            max_members=_max_members(context),
            run_id=context.run_id,
            idempotency_key=arguments.get("idempotency_key"),
        )
    except AgentTeamError as exc:
        return {"status": "error", "message": str(exc)}
    task_text = str(arguments.get("task") or "").strip()
    task_queued = False
    if task_text and not member.replayed:
        try:
            _service(context).send_message(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                to_name=member.name,
                body=task_text,
                sender="lead",
                run_id=context.run_id,
            )
            task_queued = True
            _dispatch_teammate_turn(
                context,
                member_name=member.name,
                body=task_text,
            )
        except Exception:
            logger.warning("Failed to auto-dispatch initial task to teammate", exc_info=True)
    res: dict[str, Any] = {
        "status": "ok",
        "member": member.as_dict(),
        "replayed": member.replayed,
    }
    if task_text:
        res["task_queued"] = task_queued
    return res


async def _team_send_message(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Send a message to a named teammate."""
    if not _team_enabled(context):
        return _disabled()
    service = _service(context)
    _recover_stale_teammates(context, service)
    sender = "agent"
    if context.run_id or context.parent_agent_id:
        try:
            members = service.list_members(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
            )
            for m in members:
                if m.child_id and (
                    m.child_id == context.run_id or m.child_id == context.parent_agent_id
                ):
                    sender = m.name
                    break
        except Exception:
            pass
    try:
        message = service.send_message(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            to_name=str(arguments["to"]),
            body=str(arguments["body"]),
            sender=sender,
            run_id=context.run_id,
            idempotency_key=arguments.get("idempotency_key"),
        )
    except AgentTeamError as exc:
        return {"status": "error", "message": str(exc)}
    # The turn runs in the worker, so it survives the parent's turn.  A
    # teammate that is away resumes on this turn and answers the message that
    # waited for it.
    dispatched = (
        False
        if message.replayed
        else _dispatch_teammate_turn(
            context,
            member_name=str(arguments["to"]),
            body=str(arguments["body"]),
        )
    )
    return {
        "status": "ok",
        "queued": message.state == "queued",
        "dispatched": dispatched,
        "replayed": message.replayed,
    }


def teammate_turn_kwargs(
    context: ToolContext,
    *,
    member_name: str,
    body: str,
) -> dict[str, Any] | None:
    """Build the worker turn arguments for one teammate.

    The turn of a teammate is the worker's own turn for the child run that
    works the teammate, so the arguments name that child run and carry the
    caller's authority.

    Args:
        context: The calling request's tool context.
        member_name: The teammate the message was for.
        body: The message the teammate received.

    Returns:
        The turn arguments, or None when the conversation has no such
        teammate or the teammate has no child run.
    """
    child_id = _child_id_for(context, member_name)
    if child_id is None:
        return None
    return {
        "tenant_id": str(context.auth.tenant_id),
        "user_id": str(context.auth.user_id),
        "conversation_id": str(context.conversation_id),
        "child_id": str(child_id),
        "message": body,
        "roles": sorted(str(role) for role in (context.auth.roles or ())),
        "permissions": sorted(str(permission) for permission in (context.auth.permissions or ())),
    }


def _dispatch_teammate_turn(context: ToolContext, *, member_name: str, body: str) -> bool:
    """Run one turn for a teammate that received a message.

    Args:
        context: The calling request's tool context.
        member_name: The teammate the message was for.
        body: The message the teammate received.

    Returns:
        Whether the turn was dispatched.  A dispatch failure leaves the
        message queued, so the teammate still runs it on its next turn.
    """
    kwargs = teammate_turn_kwargs(context, member_name=member_name, body=body)
    if kwargs is None:
        return False
    try:
        from app.deepspace.workers.subagent_tasks import run_subagent_turn_task

        run_subagent_turn_task.apply_async(kwargs=kwargs)
    except Exception:  # noqa: BLE001
        logger.warning("DeepSpace teammate turn dispatch failed", exc_info=True)
        return False
    return True


def _child_id_for(context: ToolContext, member_name: str) -> uuid.UUID | None:
    """Return the child run that runs one teammate.

    Args:
        context: The calling request's tool context.
        member_name: The teammate's name.

    Returns:
        The child run's id, or None when the teammate has none.
    """
    try:
        return _service(context).member_child_id(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            member_name=member_name,
        )
    except AgentTeamError:
        return None


async def _list_teammates(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the named teammates of the current conversation."""
    if not _team_enabled(context):
        return _disabled()
    service = _service(context)
    _recover_stale_teammates(context, service)
    members = service.list_members(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    revision = service.team_revision(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    return {
        "status": "ok",
        "members": [member.as_dict() for member in members],
        "roster": [member.roster_dict() for member in members],
        "revision": revision,
    }


async def _team_get_member(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read one teammate's detailed result after a roster lookup."""
    if not _team_enabled(context):
        return _disabled()
    service = _service(context)
    _recover_stale_teammates(context, service)
    members = service.list_members(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    target = str(arguments["name"]).strip()
    member = next((candidate for candidate in members if candidate.name == target), None)
    if member is None:
        return {
            "status": "error",
            "message": f"No teammate named {target!r} is in this conversation.",
        }
    return {"status": "ok", "member": member.as_dict()}


async def _team_list_members(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the compact canonical team roster."""
    del arguments
    if not _team_enabled(context):
        return _disabled()
    service = _service(context)
    _recover_stale_teammates(context, service)
    members = service.list_members(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    return {
        "status": "ok",
        "roster": [member.roster_dict() for member in members],
        "revision": service.team_revision(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        ),
    }


async def _wait_agent(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Wait for a change in the current conversation's team."""
    if not _team_enabled(context):
        return _disabled()
    timeout = int(arguments.get("timeout_seconds") or DEFAULT_WAIT_SECONDS)
    service = _service(context)
    _recover_stale_teammates(context, service)
    members = service.list_members(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    tasks = service.list_tasks(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    task_payloads = [task.as_dict() for task in tasks]

    def progress_members() -> list[dict[str, str]]:
        """Return teammates that can still produce a durable change."""
        active: list[dict[str, str]] = []
        for member in members:
            if member.state == "running":
                active.append({"name": member.name, "state": member.state})
                continue
            if service.pending_messages(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                member_name=member.name,
            ):
                active.append({"name": member.name, "state": "queued"})
        return active

    active_members = progress_members()
    revision = service.team_revision(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    if not active_members:
        return {
            "status": "ok",
            "changed": [],
            "members": [member.as_dict() for member in members],
            "timed_out": False,
            "no_progress": True,
            "active_members": active_members,
            "all_tasks_completed": bool(task_payloads)
            and all(task["status"] == "completed" for task in task_payloads),
            "tasks": task_payloads,
            "revision": revision,
            "message": (
                "No teammate is running. Do not wait again; inspect the board and wake a "
                "required teammate with team_send_message."
            ),
        }
    try:
        change = await asyncio.to_thread(
            service.wait_for_change,
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            timeout_seconds=timeout,
        )
    except AgentTeamError as exc:
        return {"status": "error", "message": str(exc)}
    members = service.list_members(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    tasks = service.list_tasks(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    task_payloads = [task.as_dict() for task in tasks]
    active_members = progress_members()
    return {
        "status": "ok",
        "changed": list(change.changed),
        "members": [member.as_dict() for member in members],
        "timed_out": change.timed_out,
        "no_progress": change.no_progress,
        "active_members": active_members,
        "all_tasks_completed": bool(task_payloads)
        and all(task["status"] == "completed" for task in task_payloads),
        "tasks": task_payloads,
        "revision": service.team_revision(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        ),
    }


async def _team_task_create(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Create a task on the shared team board."""
    if not _team_enabled(context):
        return _disabled()
    try:
        task = _service(context).create_task(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            title=str(arguments["title"]),
            body=arguments.get("body"),
            depends_on=list(arguments.get("depends_on") or []),
            write_scopes=(
                list(arguments.get("write_scopes") or [])
                if arguments.get("write_scopes") is not None
                else None
            ),
            run_id=context.run_id,
            idempotency_key=arguments.get("idempotency_key"),
        )
    except AgentTeamError as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", "task": task.as_dict()}


async def _team_task_get(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Get full details of a specific task on the shared team board."""
    if not _team_enabled(context):
        return _disabled()
    try:
        task = _service(context).get_task(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            task_id=str(arguments["task_id"]),
        )
    except AgentTeamError as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", "task": task.as_dict()}


async def _team_task_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the tasks on the shared team board."""
    if not _team_enabled(context):
        return _disabled()
    service = _service(context)
    tasks = service.list_tasks(
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        conversation_id=context.conversation_id,
    )
    return {
        "status": "ok",
        "tasks": [task.as_dict() for task in tasks],
        "revision": service.team_revision(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
        ),
    }


async def _team_task_update(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Update a task on the shared team board."""
    if not _team_enabled(context):
        return _disabled()
    try:
        task = _service(context).update_task(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            task_id=str(arguments["task_id"]),
            revision=int(arguments["revision"]),
            status=arguments.get("status"),
            owner=arguments.get("owner"),
            body=arguments.get("body"),
            write_scopes=(
                list(arguments.get("write_scopes") or [])
                if arguments.get("write_scopes") is not None
                else None
            ),
            run_id=context.run_id,
            idempotency_key=arguments.get("idempotency_key"),
        )
    except StaleTaskRevisionError as exc:
        return {"status": "error", "message": str(exc)}
    except AgentTeamError as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", "task": task.as_dict()}


async def _interrupt_agent(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Stop the current turn of a running teammate or child agent."""
    if not _team_enabled(context):
        return _disabled()
    target = arguments.get("target")
    agent_id = arguments.get("agent_id")
    if target:
        try:
            member = _service(context).interrupt_member(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                member_name=str(target).strip(),
                run_id=context.run_id,
            )
            return {
                "status": "ok",
                "member": member.as_dict(),
                "message": f"Teammate {member.name!r} has been interrupted.",
            }
        except AgentTeamError as exc:
            return {"status": "error", "message": str(exc)}

    if agent_id:
        db = require_db(context)
        child_uuid: uuid.UUID | None = None
        try:
            child_uuid = uuid.UUID(str(agent_id))
        except (ValueError, AttributeError):
            child_uuid = None

        member_cond = TeamMember.name == str(agent_id)
        if child_uuid is not None:
            member_cond = (TeamMember.child_id == child_uuid) | member_cond

        member = (
            db.execute(
                select(TeamMember).where(
                    TeamMember.tenant_id == context.auth.tenant_id,
                    TeamMember.user_id == context.auth.user_id,
                    TeamMember.conversation_id == context.conversation_id,
                    member_cond,
                )
            )
            .scalars()
            .first()
        )
        if member is not None:
            res = _service(context).interrupt_member(
                tenant_id=context.auth.tenant_id,
                user_id=context.auth.user_id,
                conversation_id=context.conversation_id,
                member_name=member.name,
                run_id=context.run_id,
            )
            return {
                "status": "accepted",
                "agent_id": str(member.child_id or agent_id),
                "member": res.as_dict(),
                "message": f"Teammate {member.name!r} has been interrupted.",
            }
        if child_uuid is not None:
            child = (
                db.execute(
                    select(DeepSpaceSubagentRun).where(
                        DeepSpaceSubagentRun.tenant_id == context.auth.tenant_id,
                        DeepSpaceSubagentRun.user_id == context.auth.user_id,
                        DeepSpaceSubagentRun.id == child_uuid,
                    )
                )
                .scalars()
                .first()
            )
            if child is not None:
                child.cancel_requested = True
                db.commit()
                return {
                    "status": "accepted",
                    "agent_id": str(child.id),
                    "message": (
                        "A stop was requested for the child's current turn. Queued messages stay queued "
                        "and the child stays available for follow-ups."
                    ),
                }
        return {"status": "error", "message": f"Agent {agent_id} not found."}

    return {"status": "error", "message": "Either target or agent_id must be provided."}


SPAWN_TEAMMATE_DESCRIPTION = (
    "Create a named teammate that works the same task as you. The name is how you "
    "address the teammate later, in team_send_message and on the task board, so "
    "choose a short name of letters, digits, '-' and '_'. The teammate is durable: "
    "it survives a restart and keeps the messages that are queued for it."
)
TEAM_SEND_MESSAGE_DESCRIPTION = (
    "Send a message to a named teammate. The message reaches a running teammate at "
    "once, and it waits in the queue for a teammate that is not running, so nothing "
    "is lost. The teammate works on the message in the background. The call does not "
    "return the teammate's answer; read it with list_teammates."
)
LIST_TEAMMATES_DESCRIPTION = (
    "Compatibility alias for team_list_members. List the named teammates of this conversation."
)
TEAM_LIST_MEMBERS_DESCRIPTION = (
    "List the compact canonical team roster with stable teammate names, lifecycle state, "
    "and the current durable team revision. Use team_get_member for detailed results."
)
WAIT_AGENT_DESCRIPTION = (
    "Wait until a teammate's state, a message, or a task on the shared board changes, "
    "and read what changed. The result includes timed_out, changed, active_members, "
    "all_tasks_completed, and the current task board. After every wait, inspect that "
    "board and the teammates; if all tasks are completed or no member is active, stop "
    "waiting and answer. Repeated waits are safe but bounded, and a limit result says "
    "to re-check teammates and tasks."
)
TEAM_TASK_CREATE_DESCRIPTION = (
    "Create a task on the shared team board. Every teammate of this conversation "
    "works from the same board, so a task is how work is handed over."
)
TEAM_TASK_LIST_DESCRIPTION = "List the tasks on the shared team board with their status, owner, dependencies, write scopes, and revision."
TEAM_TASK_GET_DESCRIPTION = "Get full details of a specific task from the shared team board, including full body, readiness, blockers, and write scopes."
TEAM_TASK_UPDATE_DESCRIPTION = (
    "Update a task on the shared team board. Name the revision you read; an update "
    "with an older revision is refused, so read the task again and apply the change "
    "to what it says now."
)
INTERRUPT_AGENT_DESCRIPTION = "Stop the current turn of a running teammate. Cancels its active work while keeping queued messages."

TEAM_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="spawn_teammate",
        description=SPAWN_TEAMMATE_DESCRIPTION,
        properties={
            "name": string_property(
                "The name you address the teammate by.",
                min_length=1,
                max_length=64,
            ),
            "provider": string_property(
                "The provider route the teammate runs under. Omit to inherit this agent's route.",
                min_length=1,
                max_length=100,
            ),
            "model": string_property(
                "The model the teammate runs under. Omit to inherit this agent's model.",
                min_length=1,
                max_length=200,
            ),
            "persona": string_property(
                "A persona that shapes how the teammate works.", max_length=4_000
            ),
            "tool_filter": array_property(
                "The only tools the teammate may use. Omit to give it the full set.",
                max_items=64,
            ),
            "task": string_property(
                "Initial task or instruction for the teammate to immediately start working on.",
                max_length=20_000,
            ),
            "mode": string_property(
                "Spawn mode: fresh starts clean; fork inherits completed turn context.",
                enum=["fresh", "fork"],
            ),
            "idempotency_key": string_property(
                "Stable retry key. Reusing it returns the original create result.",
                max_length=255,
            ),
        },
        required=["name"],
        arguments_model=SpawnTeammateArguments,
        mode="write",
        handler=_spawn_teammate,
    ),
    define_native_tool(
        name="team_send_message",
        description=TEAM_SEND_MESSAGE_DESCRIPTION,
        properties={
            "to": string_property(
                "The name of the teammate to message.", min_length=1, max_length=64
            ),
            "body": string_property("The message to deliver.", min_length=1, max_length=20_000),
            "idempotency_key": string_property(
                "Stable retry key. Reusing it returns the original message result.",
                max_length=255,
            ),
        },
        required=["to", "body"],
        arguments_model=TeamSendMessageArguments,
        mode="write",
        handler=_team_send_message,
    ),
    define_native_tool(
        name="list_teammates",
        description=LIST_TEAMMATES_DESCRIPTION,
        properties={},
        arguments_model=ListTeammatesArguments,
        mode="read",
        handler=_list_teammates,
    ),
    define_native_tool(
        name="team_list_members",
        description=TEAM_LIST_MEMBERS_DESCRIPTION,
        properties={},
        arguments_model=ListTeammatesArguments,
        mode="read",
        handler=_team_list_members,
    ),
    define_native_tool(
        name="team_get_member",
        description="Read one teammate's detailed status and last answer by name.",
        properties={
            "name": string_property(
                "The teammate name returned by team_list_members.", max_length=64
            )
        },
        required=["name"],
        arguments_model=TeamMemberGetArguments,
        mode="read",
        handler=_team_get_member,
    ),
    define_native_tool(
        name="wait_agent",
        description=WAIT_AGENT_DESCRIPTION,
        properties={
            "timeout_seconds": integer_property(
                f"How long to wait for a change, at most {MAX_WAIT_SECONDS} seconds.",
                minimum=1,
                maximum=MAX_WAIT_SECONDS,
            )
        },
        arguments_model=WaitAgentArguments,
        mode="read",
        handler=_wait_agent,
    ),
    define_native_tool(
        name="interrupt_agent",
        description=INTERRUPT_AGENT_DESCRIPTION,
        properties={
            "target": string_property(
                "The name of the teammate to stop or interrupt.",
                min_length=1,
                max_length=64,
            ),
            "agent_id": string_property(
                "The id of the child agent or teammate to stop.",
                min_length=1,
                max_length=64,
            ),
        },
        arguments_model=InterruptAgentArguments,
        mode="write",
        handler=_interrupt_agent,
    ),
    define_native_tool(
        name="team_task_create",
        description=TEAM_TASK_CREATE_DESCRIPTION,
        properties={
            "title": string_property("The short title of the task.", min_length=1, max_length=200),
            "body": string_property("What the task is.", max_length=20_000),
            "depends_on": array_property(
                "The ids of the tasks this one waits for.",
                max_items=32,
            ),
            "write_scopes": array_property(
                "Advisory directory or file paths this task writes to (e.g. ['research/scout']).",
                max_items=32,
            ),
            "idempotency_key": string_property(
                "Stable retry key. Reusing it returns the original task result.",
                max_length=255,
            ),
        },
        required=["title"],
        arguments_model=TeamTaskCreateArguments,
        mode="write",
        handler=_team_task_create,
    ),
    define_native_tool(
        name="team_task_get",
        description=TEAM_TASK_GET_DESCRIPTION,
        properties={
            "task_id": string_property("The id of the task to read.", min_length=1, max_length=64),
        },
        required=["task_id"],
        arguments_model=TeamTaskGetArguments,
        mode="read",
        handler=_team_task_get,
    ),
    define_native_tool(
        name="team_task_list",
        description=TEAM_TASK_LIST_DESCRIPTION,
        properties={},
        arguments_model=TeamTaskListArguments,
        mode="read",
        handler=_team_task_list,
    ),
    define_native_tool(
        name="team_task_update",
        description=TEAM_TASK_UPDATE_DESCRIPTION,
        properties={
            "task_id": string_property(
                "The id of the task to update.", min_length=1, max_length=64
            ),
            "revision": integer_property("The revision of the task you read.", minimum=1),
            "status": string_property(
                "The new state of the task.",
                enum=["pending", "in_progress", "blocked", "completed"],
            ),
            "owner": string_property("The name of the teammate that owns the task.", max_length=64),
            "body": string_property("The new body of the task.", max_length=20_000),
            "write_scopes": array_property(
                "Advisory directory or file paths this task writes to.",
                max_items=32,
            ),
            "idempotency_key": string_property(
                "Stable retry key. Reusing it returns the original update result.",
                max_length=255,
            ),
        },
        required=["task_id", "revision"],
        arguments_model=TeamTaskUpdateArguments,
        mode="write",
        handler=_team_task_update,
    ),
]

__all__ = [
    "DEFAULT_MAX_MEMBERS",
    "DEFAULT_WAIT_SECONDS",
    "MAX_WAIT_SECONDS",
    "TEAM_TOOLS",
    "InterruptAgentArguments",
    "ListTeammatesArguments",
    "SpawnTeammateArguments",
    "TeamSendMessageArguments",
    "TeamMemberGetArguments",
    "TeamTaskCreateArguments",
    "TeamTaskGetArguments",
    "TeamTaskListArguments",
    "TeamTaskUpdateArguments",
    "WaitAgentArguments",
    "teammate_turn_kwargs",
]
