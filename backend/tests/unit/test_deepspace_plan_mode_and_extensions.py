"""Tests for the DeepSpace plan mode, the spill store, and extensions.

The checks run against a self-contained SQLite database and a temporary
workspace root, so they exercise the real service objects without the
database bootstrap.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.plan import (
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_PROPOSED,
    PLAN_STATUS_REJECTED,
    PLAN_STATUS_REVISION_REQUESTED,
)
from app.deepspace.services.extensions import (
    ExtensionError,
    alias_definition,
    load_extensions,
    parse_extension,
    remove_extension,
    resolve_alias,
    write_extension,
)
from app.deepspace.services.plan_mode import PlanModeService
from app.deepspace.services.plan_store import PlanStoreError
from app.deepspace.services.spill_store import SpillStore, SpillStoreError, spill_result
from app.deepspace.services.tools import dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite

pytestmark = pytest.mark.unit_no_db


@pytest.fixture(scope="module")
def _schema(tmp_path_factory: Any) -> Any:
    """Build the database schema once for the module."""
    root = tmp_path_factory.mktemp("plan-mode-schema")
    engine = create_engine(f"sqlite:///{root / 'schema.db'}")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def workspace(tmp_path: Path, _schema: Any) -> Any:
    """Build a workspace, a database, and a tool context."""
    settings = Settings(
        deepspace_workspace_root=str(tmp_path / "workspaces"),
        local_storage_path=str(tmp_path / "storage"),
    )
    session = sessionmaker(bind=_schema)()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session.add(
        Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            kind="deepspace",
            title="Plan mode test",
        )
    )
    session.commit()
    context = ToolContext(
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        conversation_id=conversation_id,
        settings=settings,
        db=session,
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(),
    )
    yield {
        "context": context,
        "session": session,
        "settings": settings,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }
    session.close()


# --------------------------------------------------------------------------
# Plan mode
# --------------------------------------------------------------------------


def test_a_proposed_plan_puts_the_conversation_in_plan_mode(workspace: Any) -> None:
    service = PlanModeService(workspace["session"])

    assert (
        service.active(
            tenant_id=workspace["tenant_id"],
            user_id=workspace["user_id"],
            conversation_id=workspace["conversation_id"],
        )
        is False
    )

    plan = service.submit(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        run_id=None,
        title="Ship the release",
        content="# Plan\n\n1. Build\n2. Test",
    )

    assert plan.status == PLAN_STATUS_PROPOSED
    assert (
        service.active(
            tenant_id=workspace["tenant_id"],
            user_id=workspace["user_id"],
            conversation_id=workspace["conversation_id"],
        )
        is True
    )


def test_approving_a_plan_ends_plan_mode(workspace: Any) -> None:
    service = PlanModeService(workspace["session"])
    plan = service.submit(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        run_id=None,
        title="Ship the release",
        content="# Plan\n\n1. Build",
    )

    reviewed = service.review(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        plan_id=plan.id,
        decision=PLAN_STATUS_APPROVED,
    )

    assert reviewed.status == PLAN_STATUS_APPROVED
    assert (
        service.active(
            tenant_id=workspace["tenant_id"],
            user_id=workspace["user_id"],
            conversation_id=workspace["conversation_id"],
        )
        is False
    )


def test_asking_for_changes_keeps_the_conversation_in_plan_mode(workspace: Any) -> None:
    service = PlanModeService(workspace["session"])
    plan = service.submit(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        run_id=None,
        title="Ship the release",
        content="# Plan\n\n1. Build",
    )

    reviewed = service.review(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        plan_id=plan.id,
        decision=PLAN_STATUS_REVISION_REQUESTED,
        feedback="Add a rollback step.",
    )

    assert reviewed.status == PLAN_STATUS_REVISION_REQUESTED
    assert reviewed.feedback == "Add a rollback step."
    assert (
        service.active(
            tenant_id=workspace["tenant_id"],
            user_id=workspace["user_id"],
            conversation_id=workspace["conversation_id"],
        )
        is True
    )


def test_a_plan_cannot_be_reviewed_twice(workspace: Any) -> None:
    service = PlanModeService(workspace["session"])
    plan = service.submit(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        run_id=None,
        title="Ship the release",
        content="# Plan\n\n1. Build",
    )
    service.review(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        plan_id=plan.id,
        decision=PLAN_STATUS_APPROVED,
    )

    with pytest.raises(PlanStoreError, match="already been reviewed"):
        service.review(
            tenant_id=workspace["tenant_id"],
            user_id=workspace["user_id"],
            conversation_id=workspace["conversation_id"],
            plan_id=plan.id,
            decision=PLAN_STATUS_REJECTED,
        )


def test_turning_plan_mode_off_rejects_a_plan_under_review(workspace: Any) -> None:
    service = PlanModeService(workspace["session"])
    service.submit(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        run_id=None,
        title="Ship the release",
        content="# Plan\n\n1. Build",
    )

    state = service.set_mode(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        active=False,
    )

    assert state["active"] is False
    assert state["plan"] is not None
    assert state["plan"]["status"] == PLAN_STATUS_REJECTED


def test_the_plan_submit_tool_records_the_plan(workspace: Any) -> None:
    result = asyncio.run(
        dispatch_native_tool(
            "plan_submit",
            {"title": "Ship the release", "plan": "# Plan\n\n1. Build"},
            workspace["context"],
        )
    )

    assert result is not None
    assert result["status"] == "proposed"
    assert result["plan"]["title"] == "Ship the release"
    assert result["plan"]["status"] == PLAN_STATUS_PROPOSED


def test_the_plan_read_tool_reports_the_current_plan(workspace: Any) -> None:
    asyncio.run(
        dispatch_native_tool(
            "plan_submit",
            {"title": "Ship the release", "plan": "# Plan\n\n1. Build"},
            workspace["context"],
        )
    )

    result = asyncio.run(dispatch_native_tool("plan_read", {}, workspace["context"]))

    assert result is not None
    assert result["plan_mode_active"] is True
    assert result["plan"]["title"] == "Ship the release"


def test_a_plan_without_a_body_is_refused(workspace: Any) -> None:
    result = asyncio.run(
        dispatch_native_tool(
            "plan_submit",
            {"title": "Ship the release", "plan": "   "},
            workspace["context"],
        )
    )

    assert result is not None
    assert result["status"] == "error"


# --------------------------------------------------------------------------
# Spill
# --------------------------------------------------------------------------


def test_a_small_result_is_not_spilled(workspace: Any) -> None:
    payload, reference = spill_result(
        settings=workspace["settings"],
        tenant_id=workspace["tenant_id"],
        tool_name="web_search",
        payload={"results": ["one"]},
    )

    assert reference is None
    assert payload == {"results": ["one"]}


def test_a_large_result_is_spilled_and_read_back(workspace: Any) -> None:
    payload, reference = spill_result(
        settings=workspace["settings"],
        tenant_id=workspace["tenant_id"],
        tool_name="web_search",
        payload={"results": ["x" * 100_000]},
    )

    assert reference is not None
    assert payload["status"] == "spilled"
    assert payload["spill_ref"] == reference.sha256
    assert payload["preview_truncated"] is True

    store = SpillStore(workspace["settings"])
    restored = store.get(tenant_id=workspace["tenant_id"], sha256=reference.sha256)
    assert "x" * 1000 in restored


def test_a_spill_is_read_through_the_tool(workspace: Any) -> None:
    _payload, reference = spill_result(
        settings=workspace["settings"],
        tenant_id=workspace["tenant_id"],
        tool_name="web_search",
        payload={"results": ["y" * 60_000]},
    )
    assert reference is not None

    result = asyncio.run(
        dispatch_native_tool(
            "spill_read",
            {"spill_ref": reference.sha256},
            workspace["context"],
        )
    )

    assert result is not None
    assert result["status"] == "ok"
    assert "y" * 1000 in result["text"]
    assert result["total_characters"] > 60_000


def test_a_spill_of_another_tenant_is_unreadable(workspace: Any) -> None:
    _payload, reference = spill_result(
        settings=workspace["settings"],
        tenant_id=workspace["tenant_id"],
        tool_name="web_search",
        payload={"results": ["z" * 60_000]},
    )
    assert reference is not None

    store = SpillStore(workspace["settings"])
    with pytest.raises(SpillStoreError):
        store.get(tenant_id=uuid.uuid4(), sha256=reference.sha256)


def test_an_invalid_spill_reference_is_refused(workspace: Any) -> None:
    store = SpillStore(workspace["settings"])

    with pytest.raises(SpillStoreError, match="invalid"):
        store.get(tenant_id=workspace["tenant_id"], sha256="not-a-fingerprint")


# --------------------------------------------------------------------------
# Extensions
# --------------------------------------------------------------------------


def test_an_extension_adds_guidance_and_tool_restrictions(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    (root / ".deepspace" / "extensions").mkdir(parents=True)
    (root / ".deepspace" / "extensions" / "strict.json").write_text(
        json.dumps(
            {
                "name": "strict",
                "description": "Never delete anything.",
                "guidance": "Never call the delete tool.",
                "deny_tools": ["delete"],
                "approval_tools": ["bash"],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_extensions(root)

    assert [extension.name for extension in loaded.extensions] == ["strict"]
    assert "Never call the delete tool." in loaded.guidance
    assert loaded.denied_tools == frozenset({"delete"})
    assert loaded.approval_tools == frozenset({"bash"})


def test_a_broken_extension_manifest_does_not_break_the_load(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    (root / ".deepspace" / "extensions").mkdir(parents=True)
    (root / ".deepspace" / "extensions" / "broken.json").write_text("{not json", encoding="utf-8")

    assert load_extensions(root).extensions == ()


def test_an_extension_manifest_with_a_bad_name_is_refused() -> None:
    with pytest.raises(ExtensionError, match="may only contain"):
        parse_extension({"name": "bad name!"})


def test_an_extension_alias_hides_the_arguments_it_fixes() -> None:
    from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY

    extension = parse_extension(
        {
            "name": "quick",
            "aliases": {
                "find_my_docs": {
                    "tool": "read",
                    "arguments": {"target": "library"},
                }
            },
        }
    )
    alias = extension.aliases[0]

    definition = alias_definition(alias, NATIVE_TOOL_REGISTRY.emit("read"))
    properties = definition["function"]["parameters"]["properties"]

    assert "target" not in properties
    assert resolve_alias(alias, {"file_id": "abc"}) == {
        "file_id": "abc",
        "target": "library",
    }


def test_an_extension_is_written_and_removed(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir(parents=True, exist_ok=True)

    extension = write_extension(
        root,
        {"name": "helper", "guidance": "Prefer the workspace file tools."},
    )

    assert extension.name == "helper"
    assert (root / ".deepspace" / "extensions" / "helper.json").is_file()
    assert remove_extension(root, "helper") is True
    assert remove_extension(root, "helper") is False


def test_the_extension_tools_manage_the_workspace(workspace: Any) -> None:
    written = asyncio.run(
        dispatch_native_tool(
            "extension_write",
            {"name": "helper", "guidance": "Prefer the workspace file tools."},
            workspace["context"],
        )
    )
    assert written is not None
    assert written["status"] == "ok"

    listed = asyncio.run(dispatch_native_tool("extension_list", {}, workspace["context"]))
    assert listed is not None
    assert listed["extension_count"] == 1
    assert "Prefer the workspace file tools." in listed["guidance"]

    removed = asyncio.run(
        dispatch_native_tool("extension_remove", {"name": "helper"}, workspace["context"])
    )
    assert removed is not None
    assert removed["removed"] is True


def test_a_denied_tool_is_refused_by_the_policy(workspace: Any) -> None:
    from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy

    policy = DeepSpaceToolPolicy(approval_mode="never", permission_mode="workspace-write")
    policy.set_extension_rules(denied_tools={"delete"})

    decision = policy.decide("delete", {"target": "library"})

    assert decision.allowed is False
    assert "extension" in str(decision.reason)


def test_an_extension_can_make_a_tool_ask_for_approval(workspace: Any) -> None:
    from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy

    policy = DeepSpaceToolPolicy(approval_mode="never", permission_mode="workspace-write")
    policy.set_extension_rules(approval_tools={"read"})

    decision = policy.decide("read", {"target": "library"})

    assert decision.allowed is True
    assert decision.requires_approval is True
