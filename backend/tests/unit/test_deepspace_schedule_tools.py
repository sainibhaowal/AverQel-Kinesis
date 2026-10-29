"""Tests for the DeepSpace schedule tools.

The checks cover the result each tool returns, the conversation scope that
keeps one conversation's schedules away from another, the error result a
failure produces, and the names, modes, and argument limits the specs
declare.  The service is an in-memory double, so no database is touched.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.deepspace.services.tools import schedule as schedule_tools
from app.deepspace.services.tools._base import NativeToolSpec, ToolContext

pytestmark = pytest.mark.unit_no_db

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
CONVERSATION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
OTHER_CONVERSATION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
SCHEDULE_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
OTHER_SCHEDULE_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
CREATED_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
NEXT_RUN_AT = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)

#: The message the tools report for a schedule they may not reach.
NOT_FOUND = {"status": "error", "message": "Schedule not found."}


class _FakeSchedule:
    """One schedule the fake service hands to the tools."""

    def __init__(
        self,
        *,
        schedule_id: uuid.UUID = SCHEDULE_ID,
        conversation_id: uuid.UUID = CONVERSATION_ID,
        name: str = "Deploy check",
        interval_minutes: int = 60,
        status: str = "active",
        next_run_at: datetime = NEXT_RUN_AT,
    ) -> None:
        self.id = schedule_id
        self.conversation_id = conversation_id
        self.name = name
        self.interval_minutes = interval_minutes
        self.status = status
        self.next_run_at = next_run_at


class _FakeService:
    """A schedule service that keeps its records in memory."""

    def __init__(self, schedules: list[_FakeSchedule] | None = None) -> None:
        self.schedules = list(schedules or [])
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[uuid.UUID] = []
        self.failure: Exception | None = None

    def _check(self) -> None:
        if self.failure is not None:
            raise self.failure

    def list_schedules(self, auth: Any) -> list[_FakeSchedule]:
        self._check()
        return list(self.schedules)

    def get_schedule(self, auth: Any, schedule_id: uuid.UUID) -> _FakeSchedule:
        self._check()
        for schedule in self.schedules:
            if schedule.id == schedule_id:
                return schedule
        raise schedule_tools.ScheduleNotFoundError(schedule_tools.SCHEDULE_NOT_FOUND_MESSAGE)

    def create_schedule(
        self,
        auth: Any,
        conversation_id: uuid.UUID,
        *,
        name: str,
        prompt: str,
        interval_minutes: int,
    ) -> _FakeSchedule:
        self._check()
        self.created.append(
            {
                "conversation_id": conversation_id,
                "name": name,
                "prompt": prompt,
                "interval_minutes": interval_minutes,
            }
        )
        created = _FakeSchedule(
            schedule_id=CREATED_ID,
            conversation_id=conversation_id,
            name=name,
            interval_minutes=interval_minutes,
        )
        self.schedules.append(created)
        return created

    def update_schedule(
        self,
        auth: Any,
        schedule_id: uuid.UUID,
        *,
        name: str | None = None,
        prompt: str | None = None,
        interval_minutes: int | None = None,
        status: str | None = None,
    ) -> _FakeSchedule:
        self._check()
        self.updated.append(
            {
                "schedule_id": schedule_id,
                "name": name,
                "prompt": prompt,
                "interval_minutes": interval_minutes,
                "status": status,
            }
        )
        for schedule in self.schedules:
            if schedule.id == schedule_id:
                if name is not None:
                    schedule.name = name
                if interval_minutes is not None:
                    schedule.interval_minutes = interval_minutes
                if status is not None:
                    schedule.status = status
                return schedule
        raise schedule_tools.ScheduleNotFoundError(schedule_tools.SCHEDULE_NOT_FOUND_MESSAGE)

    def delete_schedule(self, auth: Any, schedule_id: uuid.UUID) -> None:
        self._check()
        self.deleted.append(schedule_id)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> _FakeService:
    """Replace the module's schedule service with an in-memory one.

    Args:
        monkeypatch: The fixture that restores the module after the test.

    Returns:
        The service double.
    """
    fake = _FakeService()
    monkeypatch.setattr(schedule_tools, "_schedule_service", lambda context: fake)
    return fake


def _context(**overrides: Any) -> ToolContext:
    """Build the tool context the schedule tools run in.

    Args:
        overrides: The fields to replace.

    Returns:
        The context.
    """
    values: dict[str, Any] = {
        "auth": SimpleNamespace(tenant_id=TENANT_ID, user_id=USER_ID),
        "conversation_id": CONVERSATION_ID,
        "settings": SimpleNamespace(),
        "db": SimpleNamespace(),
        "task_store": SimpleNamespace(),
        "tool_result_store": None,
        "provider_registry": SimpleNamespace(),
    }
    values.update(overrides)
    return ToolContext(**values)  # type: ignore[arg-type]


def _spec(name: str) -> NativeToolSpec:
    """Return the declared spec of one tool.

    Args:
        name: The tool's name.

    Returns:
        The spec.
    """
    for spec in schedule_tools.SCHEDULE_TOOLS:
        if spec.name == name:
            return spec
    raise AssertionError(f"The module declares no tool named {name!r}.")


def _run(name: str, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run one declared tool.

    Args:
        name: The tool's name.
        arguments: The arguments the model supplied.
        context: The calling request's tool context.

    Returns:
        The tool's result.
    """
    return asyncio.run(_spec(name).handler(arguments, context))


def test_the_specs_declare_their_names_and_modes() -> None:
    specs = {spec.name: spec for spec in schedule_tools.SCHEDULE_TOOLS}

    assert list(specs) == [
        "schedule_create",
        "schedule_list",
        "schedule_update",
        "schedule_delete",
    ]
    assert specs["schedule_create"].mode == "write"
    assert specs["schedule_list"].mode == "read"
    assert specs["schedule_update"].mode == "write"
    assert specs["schedule_delete"].mode == "write"


def test_every_definition_names_its_tool_and_refuses_undeclared_arguments() -> None:
    for spec in schedule_tools.SCHEDULE_TOOLS:
        function = spec.definition["function"]
        assert function["name"] == spec.name
        assert function["description"].strip()
        assert function["parameters"]["additionalProperties"] is False


def test_the_definitions_carry_their_required_arguments() -> None:
    definitions = {spec.name: spec.definition["function"] for spec in schedule_tools.SCHEDULE_TOOLS}

    assert definitions["schedule_create"]["parameters"]["required"] == ["name", "prompt"]
    assert definitions["schedule_list"]["parameters"]["required"] == []
    assert definitions["schedule_update"]["parameters"]["required"] == ["schedule_id"]
    assert definitions["schedule_delete"]["parameters"]["required"] == ["schedule_id"]


def test_schedule_create_returns_the_created_schedule(service: _FakeService) -> None:
    result = _run(
        "schedule_create",
        {
            "name": "Deploy check",
            "prompt": "Report the deploy status",
            "interval_minutes": 30,
        },
        _context(),
    )

    assert result == {
        "status": "ok",
        "schedule": {
            "id": str(CREATED_ID),
            "name": "Deploy check",
            "interval_minutes": 30,
            "status": "active",
            "next_run_at": NEXT_RUN_AT.isoformat(),
        },
    }
    assert service.created == [
        {
            "conversation_id": CONVERSATION_ID,
            "name": "Deploy check",
            "prompt": "Report the deploy status",
            "interval_minutes": 30,
        }
    ]


def test_schedule_list_returns_the_schedules_of_the_conversation(
    service: _FakeService,
) -> None:
    service.schedules = [
        _FakeSchedule(schedule_id=SCHEDULE_ID, name="Deploy check"),
        _FakeSchedule(
            schedule_id=OTHER_SCHEDULE_ID,
            conversation_id=OTHER_CONVERSATION_ID,
            name="Another conversation",
        ),
    ]

    result = _run("schedule_list", {}, _context())

    assert result == {
        "status": "ok",
        "schedules": [
            {
                "id": str(SCHEDULE_ID),
                "name": "Deploy check",
                "interval_minutes": 60,
                "status": "active",
                "next_run_at": NEXT_RUN_AT.isoformat(),
            }
        ],
    }


def test_schedule_update_changes_one_schedule(service: _FakeService) -> None:
    service.schedules = [_FakeSchedule()]

    result = _run(
        "schedule_update",
        {"schedule_id": str(SCHEDULE_ID), "interval_minutes": 15, "status": "paused"},
        _context(),
    )

    assert result == {
        "status": "ok",
        "schedule": {
            "id": str(SCHEDULE_ID),
            "name": "Deploy check",
            "interval_minutes": 15,
            "status": "paused",
            "next_run_at": NEXT_RUN_AT.isoformat(),
        },
    }
    assert service.updated == [
        {
            "schedule_id": SCHEDULE_ID,
            "name": None,
            "prompt": None,
            "interval_minutes": 15,
            "status": "paused",
        }
    ]


def test_schedule_delete_removes_one_schedule(service: _FakeService) -> None:
    service.schedules = [_FakeSchedule()]

    result = _run("schedule_delete", {"schedule_id": str(SCHEDULE_ID)}, _context())

    assert result == {"status": "ok", "deleted": True}
    assert service.deleted == [SCHEDULE_ID]


def test_a_schedule_of_another_conversation_is_refused(service: _FakeService) -> None:
    service.schedules = [
        _FakeSchedule(schedule_id=SCHEDULE_ID, conversation_id=OTHER_CONVERSATION_ID)
    ]

    update = _run("schedule_update", {"schedule_id": str(SCHEDULE_ID), "name": "Mine"}, _context())
    delete = _run("schedule_delete", {"schedule_id": str(SCHEDULE_ID)}, _context())

    assert update == NOT_FOUND
    assert delete == NOT_FOUND
    assert service.updated == []
    assert service.deleted == []


def test_a_missing_schedule_is_refused_the_same_way(service: _FakeService) -> None:
    update = _run("schedule_update", {"schedule_id": str(SCHEDULE_ID), "name": "Mine"}, _context())
    delete = _run("schedule_delete", {"schedule_id": str(SCHEDULE_ID)}, _context())

    assert update == NOT_FOUND
    assert delete == NOT_FOUND
    assert update == delete


def test_a_malformed_schedule_id_is_refused(service: _FakeService) -> None:
    result = _run("schedule_delete", {"schedule_id": "not-an-identifier"}, _context())

    assert result == {"status": "error", "message": "The schedule id is not a valid identifier."}
    assert service.deleted == []


def test_a_failure_reports_an_error_instead_of_raising(service: _FakeService) -> None:
    service.failure = RuntimeError("the schedule store is unavailable")

    for name, arguments in (
        ("schedule_create", {"name": "Deploy check", "prompt": "Report the deploy"}),
        ("schedule_list", {}),
        ("schedule_update", {"schedule_id": str(SCHEDULE_ID)}),
        ("schedule_delete", {"schedule_id": str(SCHEDULE_ID)}),
    ):
        result = _run(name, arguments, _context())
        assert result == {
            "status": "error",
            "message": "the schedule store is unavailable",
        }, name


def test_the_tools_report_a_missing_database_session() -> None:
    result = _run("schedule_list", {}, _context(db=None))

    assert result == {
        "status": "error",
        "message": "This tool needs a database session.",
    }


def test_the_argument_limits_match_the_rest_schedule_payloads() -> None:
    from app.deepspace.api.schedules import ScheduleCreate, ScheduleUpdate

    assert (
        ScheduleCreate(name="n", prompt="p", conversation_id=CONVERSATION_ID).interval_minutes == 60
    )
    assert schedule_tools.ScheduleCreateArguments(name="n", prompt="p").interval_minutes == 60

    for arguments in (
        {"name": "n", "prompt": "p", "conversation_id": CONVERSATION_ID, "interval_minutes": 4},
        {
            "name": "n",
            "prompt": "p",
            "conversation_id": CONVERSATION_ID,
            "interval_minutes": 43_201,
        },
        {"name": "", "prompt": "p", "conversation_id": CONVERSATION_ID},
        {"name": "n" * 161, "prompt": "p", "conversation_id": CONVERSATION_ID},
        {"name": "n", "prompt": "", "conversation_id": CONVERSATION_ID},
        {"name": "n", "prompt": "p" * 20_001, "conversation_id": CONVERSATION_ID},
    ):
        with pytest.raises(ValidationError):
            ScheduleCreate(**arguments)

    for arguments in (
        {"name": "n", "prompt": "p", "interval_minutes": 4},
        {"name": "n", "prompt": "p", "interval_minutes": 43_201},
        {"name": "", "prompt": "p"},
        {"name": "n" * 161, "prompt": "p"},
        {"name": "n", "prompt": ""},
        {"name": "n", "prompt": "p" * 20_001},
    ):
        with pytest.raises(ValidationError):
            schedule_tools.ScheduleCreateArguments(**arguments)

    with pytest.raises(ValidationError):
        ScheduleUpdate(status="stopped")
    with pytest.raises(ValidationError):
        schedule_tools.ScheduleUpdateArguments(schedule_id=str(SCHEDULE_ID), status="stopped")


def test_the_update_arguments_refuse_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        schedule_tools.ScheduleUpdateArguments(schedule_id=str(SCHEDULE_ID), unexpected="value")
