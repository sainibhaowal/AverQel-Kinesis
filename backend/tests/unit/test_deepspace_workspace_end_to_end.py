"""End-to-end tests for the DeepSpace workspace tools and the typed log.

The check runs against a self-contained SQLite database and a temporary
workspace root, so it exercises the real service objects without the
database bootstrap.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.services.session_events import (
    TOOL_CALL,
    TOOL_RESULT,
    TURN_START,
    SessionEventStore,
)
from app.deepspace.services.session_projections import (
    delegation_tree,
    tool_timeline,
    turn_boundary,
)
from app.deepspace.services.tools import dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite

pytestmark = pytest.mark.unit_no_db


@pytest.fixture
def workspace(tmp_path: Path) -> Any:
    """Build a workspace, a database, and a tool context for the file tools."""
    settings = Settings(deepspace_workspace_root=str(tmp_path / "workspaces"))
    engine = create_engine(f"sqlite:///{tmp_path / 'workspace.db'}")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session.add(
        Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            kind="deepspace",
            title="Workspace test",
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
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }


def test_a_file_survives_a_write_a_read_an_edit_and_a_delete(workspace: Any) -> None:
    context = workspace["context"]

    written = asyncio.run(
        dispatch_native_tool(
            "fs_write", {"path": "src/parser.py", "content": "return 42\n"}, context
        )
    )
    read = asyncio.run(dispatch_native_tool("fs_read", {"path": "src/parser.py"}, context))
    replaced = asyncio.run(
        dispatch_native_tool(
            "fs_str_replace",
            {"path": "src/parser.py", "old_string": "42", "new_string": "7"},
            context,
        )
    )
    after = asyncio.run(dispatch_native_tool("fs_read", {"path": "src/parser.py"}, context))
    deleted = asyncio.run(dispatch_native_tool("fs_delete", {"path": "src/parser.py"}, context))

    assert written is not None and written["status"] == "ok"
    assert read is not None and "return 42" in str(read["content"])
    assert replaced is not None and replaced["status"] == "ok"
    assert after is not None and "return 7" in str(after["content"])
    assert deleted is not None and deleted["status"] == "ok"


def test_a_path_that_leaves_the_workspace_is_refused(workspace: Any) -> None:
    context = workspace["context"]

    result = asyncio.run(dispatch_native_tool("fs_read", {"path": "../../../etc/passwd"}, context))

    assert result is not None
    assert result["status"] == "error"
    assert ".." in str(result["message"])


def test_the_search_tools_find_a_file_and_its_content(workspace: Any) -> None:
    context = workspace["context"]
    asyncio.run(
        dispatch_native_tool(
            "fs_write", {"path": "code.py", "content": "needle here\nhay\n"}, context
        )
    )

    grep = asyncio.run(dispatch_native_tool("fs_grep", {"pattern": "needle"}, context))
    glob = asyncio.run(dispatch_native_tool("fs_glob", {"pattern": "**/*.py"}, context))

    assert grep is not None and grep["match_count"] == 1
    assert grep["matches"][0]["path"] == "code.py"
    assert glob is not None and "code.py" in glob["matches"]


def test_a_goal_is_recorded_and_read_back(workspace: Any) -> None:
    context = workspace["context"]

    written = asyncio.run(
        dispatch_native_tool(
            "goal_set",
            {"name": "ship-parity", "content": "Give DeepSpace the NeoSIS tool surface."},
            context,
        )
    )
    read = asyncio.run(dispatch_native_tool("goal_read", {"name": "ship-parity"}, context))

    assert written is not None and written["status"] == "ok"
    assert read is not None and read["goal_count"] == 1
    assert read["goals"][0]["content"].endswith("tool surface.")


def test_the_typed_log_projects_a_turn_and_a_tool_timeline(workspace: Any) -> None:
    session = workspace["session"]
    store = SessionEventStore(session)
    store.append(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        event_type=TURN_START,
        turn=1,
        payload={},
    )
    store.append(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        event_type=TOOL_CALL,
        tool_name="fs_read",
        turn=1,
        payload={"arguments": {"path": "a.md"}},
    )
    store.append(
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
        event_type=TOOL_RESULT,
        tool_name="fs_read",
        turn=1,
        payload={"status": "completed"},
    )
    session.commit()

    boundary = turn_boundary(
        session,
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
    )
    timeline = tool_timeline(
        session,
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
    )

    assert boundary.open_turn == 1
    assert boundary.last_turn == 1
    assert len(timeline) == 2
    assert timeline[0].status == "call"
    assert timeline[1].status == "result"


def test_the_delegation_tree_projection_is_empty_for_a_fresh_conversation(
    workspace: Any,
) -> None:
    tree = delegation_tree(
        workspace["session"],
        tenant_id=workspace["tenant_id"],
        user_id=workspace["user_id"],
        conversation_id=workspace["conversation_id"],
    )

    assert tree == []
