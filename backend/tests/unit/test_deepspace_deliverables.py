"""Tests for the DeepSpace turn-change record, the deliverable store, and the
``present`` tool.

The checks cover the record of the files one turn changed, the bound the store
keeps, the declaration of a deliverable, and the refusals the ``present`` tool
makes for a missing file and for a path outside the workspace.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services import session_surfaces
from app.deepspace.services.deliverables import (
    MAX_TURNS_PER_CONVERSATION,
    DeliverableError,
    DeliverableStore,
    declare_deliverable,
    deliverable_store,
    record_turn_changes,
)
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.present import PRESENT_TOOLS
from app.deepspace.services.workspace_root import workspace_root_for
from app.deepspace.services.workspace_snapshot import collect_snapshot_files

pytestmark = pytest.mark.unit_no_db


def _context(tmp_path: Path) -> ToolContext:
    """Build a tool context whose request has no database session."""
    return ToolContext(
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        settings=SimpleNamespace(deepspace_workspace_root=str(tmp_path / "workspaces")),
        db=None,
        task_store=None,
        tool_result_store=None,
        provider_registry=None,
    )


def _root(context: ToolContext) -> Path:
    """Return the workspace root one context owns."""
    return workspace_root_for(context.settings, context.auth.tenant_id, context.auth.user_id)


def _present() -> object:
    """Return the spec of the present tool."""
    assert len(PRESENT_TOOLS) == 1
    return PRESENT_TOOLS[0]


# --------------------------------------------------------------------------
# The turn-change record
# --------------------------------------------------------------------------


def test_the_record_reports_the_changed_files(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("one\ntwo\n", encoding="utf-8")
    before = collect_snapshot_files(root)
    (root / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (root / "b.py").write_text("new\n", encoding="utf-8")
    after = collect_snapshot_files(root)
    conversation_id = uuid.uuid4()

    record = record_turn_changes(conversation_id, 1, before, after, root=root)

    assert record.conversation_id == conversation_id
    assert record.turn == 1
    assert [item.path for item in record.files] == ["a.py", "b.py"]
    assert record.recorded_at > 0


def test_the_record_counts_the_lines_a_file_gained_and_lost(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    store = DeliverableStore()
    conversation_id = uuid.uuid4()
    store.record_turn_changes(conversation_id, 1, [], collect_snapshot_files(root), root=root)

    before = collect_snapshot_files(root)
    (root / "a.py").write_text("one\nTWO\nthree\n", encoding="utf-8")
    after = collect_snapshot_files(root)
    record = store.record_turn_changes(conversation_id, 2, before, after, root=root)

    assert [item.path for item in record.files] == ["a.py"]
    assert record.files[0].changed_lines == 2


def test_the_record_counts_a_file_the_store_has_never_seen_as_fully_changed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    store = DeliverableStore()
    conversation_id = uuid.uuid4()

    record = store.record_turn_changes(
        conversation_id, 1, [], collect_snapshot_files(root), root=root
    )

    assert record.files[0].changed_lines == 3


def test_the_record_counts_a_removed_file_by_the_lines_it_held(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("one\ntwo\n", encoding="utf-8")
    store = DeliverableStore()
    conversation_id = uuid.uuid4()
    store.record_turn_changes(conversation_id, 1, [], collect_snapshot_files(root), root=root)

    before = collect_snapshot_files(root)
    (root / "a.py").unlink()
    after = collect_snapshot_files(root)
    record = store.record_turn_changes(conversation_id, 2, before, after, root=root)

    assert [item.path for item in record.files] == ["a.py"]
    assert record.files[0].changed_lines == 2


def test_the_store_keeps_only_the_last_turns_of_a_conversation(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    store = DeliverableStore()
    conversation_id = uuid.uuid4()

    for turn in range(MAX_TURNS_PER_CONVERSATION + 5):
        store.record_turn_changes(
            conversation_id, turn, [], collect_snapshot_files(root), root=root
        )

    records = store.turn_changes(conversation_id)
    assert len(records) == MAX_TURNS_PER_CONVERSATION
    assert records[-1].turn == MAX_TURNS_PER_CONVERSATION + 4
    assert records[0].turn == 5


def test_the_store_keeps_the_conversations_apart(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    store = DeliverableStore()
    first = uuid.uuid4()
    second = uuid.uuid4()

    store.record_turn_changes(first, 1, [], collect_snapshot_files(root), root=root)

    assert len(store.turn_changes(first)) == 1
    assert store.turn_changes(second) == ()


def test_the_current_turn_follows_the_recorded_turns(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    store = DeliverableStore()
    conversation_id = uuid.uuid4()

    assert store.current_turn(conversation_id) == 1

    store.record_turn_changes(conversation_id, 1, [], collect_snapshot_files(root), root=root)

    assert store.current_turn(conversation_id) == 2


# --------------------------------------------------------------------------
# Declaring a deliverable
# --------------------------------------------------------------------------


def test_a_declaration_refuses_a_path_outside_the_workspace() -> None:
    conversation_id = uuid.uuid4()

    with pytest.raises(DeliverableError):
        declare_deliverable(conversation_id, 1, "../outside.txt")
    with pytest.raises(DeliverableError):
        declare_deliverable(conversation_id, 1, "/etc/passwd")

    assert deliverable_store().deliverables(conversation_id) == ()


def test_a_declaration_is_recorded_with_its_turn() -> None:
    conversation_id = uuid.uuid4()

    record = declare_deliverable(conversation_id, 3, "notes/report.md")

    assert record.path == "notes/report.md"
    assert record.conversation_id == conversation_id
    assert record.turn == 3
    assert record.declared_at > 0
    assert deliverable_store().deliverables(conversation_id) == (record,)


# --------------------------------------------------------------------------
# The present tool
# --------------------------------------------------------------------------


async def test_present_records_an_existing_file_as_a_deliverable(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "report.md").write_text("the report\n", encoding="utf-8")

    result = await _present().handler({"paths": ["report.md"]}, context)

    assert result["status"] == "ok"
    assert result["deliverables"] == [{"path": "report.md", "size_bytes": 11}]
    recorded = deliverable_store().deliverables(context.conversation_id)
    assert [item.path for item in recorded] == ["report.md"]
    assert recorded[0].turn == deliverable_store().current_turn(context.conversation_id)


async def test_present_refuses_a_missing_file(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _root(context)

    result = await _present().handler({"paths": ["absent.md"]}, context)

    assert result["status"] == "error"
    assert result["refused"][0]["path"] == "absent.md"
    assert deliverable_store().deliverables(context.conversation_id) == ()


async def test_present_refuses_a_path_outside_the_workspace(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _root(context)

    result = await _present().handler({"paths": ["../outside.md"]}, context)

    assert result["status"] == "error"
    assert result["refused"][0]["path"] == "../outside.md"
    assert deliverable_store().deliverables(context.conversation_id) == ()


async def test_present_refuses_the_whole_call_when_one_path_is_refused(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "report.md").write_text("the report\n", encoding="utf-8")

    result = await _present().handler({"paths": ["report.md", "absent.md"]}, context)

    assert result["status"] == "error"
    assert deliverable_store().deliverables(context.conversation_id) == ()


async def test_present_refuses_a_directory(tmp_path: Path) -> None:
    context = _context(tmp_path)
    root = _root(context)
    (root / "notes").mkdir()

    result = await _present().handler({"paths": ["notes"]}, context)

    assert result["status"] == "error"
    assert deliverable_store().deliverables(context.conversation_id) == ()


def test_the_present_tool_is_declared_as_a_write_tool() -> None:
    spec = _present()

    assert spec.name == "present"
    assert spec.mode == "write"
    properties = spec.definition["function"]["parameters"]["properties"]
    assert set(properties) == {"paths"}
    assert spec.definition["function"]["parameters"]["required"] == ["paths"]


def test_projection_reconstructs_worker_declarations_from_session_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    called_at = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "seq": 1,
            "event_type": session_surfaces.TOOL_CALL,
            "turn": 2,
            "step": 1,
            "tool_name": "present",
            "payload": {"arguments": {"paths": ["report.md"]}},
            "created_at": called_at,
        },
        {
            "seq": 2,
            "event_type": session_surfaces.TOOL_RESULT,
            "turn": 2,
            "step": 1,
            "tool_name": "present",
            "payload": {"status": "completed", "duration_ms": 1},
            "created_at": called_at,
        },
    ]
    monkeypatch.setattr(session_surfaces, "_event_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(
        "app.deepspace.services.deliverables.deliverable_store",
        lambda: DeliverableStore(),
    )

    projection = session_surfaces.declared_deliverable_projection(
        conversation_id,
        db=object(),
        tenant_id=tenant_id,
        user_id=user_id,
    )

    assert projection["available"] is True
    assert projection["declared"] == [
        {
            "path": "report.md",
            "conversation_id": str(conversation_id),
            "turn": 2,
            "declared_at": called_at.timestamp(),
        }
    ]
