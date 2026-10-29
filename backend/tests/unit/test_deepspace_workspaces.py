"""Tests for the DeepSpace workspaces and the workspace a conversation picks.

The checks cover the catalogue one account owns, the rules a workspace name
must follow, the selection a conversation records, and the tools that write
in the workspace the conversation works in.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services.chat_service import DeepSpaceChatService
from app.deepspace.services.plugins import conversation_plugin_scope
from app.deepspace.services.session_events import WORKSPACE_SELECTED
from app.deepspace.services.terminals import TerminalManager
from app.deepspace.services.tools import dispatch_native_tool
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.workspace_root import (
    named_workspaces_root,
    workspace_root_for,
)
from app.deepspace.services.workspaces import (
    DEFAULT_WORKSPACE_ID,
    DuplicateWorkspaceError,
    InvalidWorkspaceNameError,
    WorkspaceSelectionService,
    create_workspace,
    find_workspace,
    list_workspaces,
)
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite

pytestmark = pytest.mark.unit_no_db


@pytest.fixture(scope="module")
def _schema(tmp_path_factory: Any) -> Any:
    """Build the database schema once for the module."""
    root = tmp_path_factory.mktemp("workspace-schema")
    engine = create_engine(f"sqlite:///{root / 'schema.db'}")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def account(tmp_path: Path, _schema: Any) -> Any:
    """Build one account, its database session, and its settings."""
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
            title="Workspace test",
        )
    )
    session.commit()
    yield {
        "session": session,
        "settings": settings,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }
    session.close()


def _context(account: dict[str, Any], workspace_root: Path | None = None) -> ToolContext:
    """Build a tool context for one account.

    Args:
        account: The account the context belongs to.
        workspace_root: The workspace the conversation works in, when it has
            one.

    Returns:
        The tool context.
    """
    return ToolContext(
        auth=SimpleNamespace(
            tenant_id=account["tenant_id"],
            user_id=account["user_id"],
        ),
        conversation_id=account["conversation_id"],
        settings=account["settings"],
        db=account["session"],
        task_store=SimpleNamespace(),
        tool_result_store=SimpleNamespace(),
        provider_registry=SimpleNamespace(),
        workspace_root=workspace_root,
    )


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------


def test_the_default_workspace_is_the_account_root(account: dict[str, Any]) -> None:
    workspaces = list_workspaces(account["settings"], account["tenant_id"], account["user_id"])

    assert len(workspaces) == 1
    default = workspaces[0]
    assert default.id == DEFAULT_WORKSPACE_ID
    assert default.is_default is True
    assert default.path == workspace_root_for(
        account["settings"], account["tenant_id"], account["user_id"]
    )


def test_listing_finds_the_default_and_the_created_workspaces(account: dict[str, Any]) -> None:
    create_workspace(account["settings"], account["tenant_id"], account["user_id"], "Reports")
    create_workspace(account["settings"], account["tenant_id"], account["user_id"], "Notes")

    workspaces = list_workspaces(account["settings"], account["tenant_id"], account["user_id"])

    assert [workspace.id for workspace in workspaces] == [
        DEFAULT_WORKSPACE_ID,
        "notes",
        "reports",
    ]
    assert [workspace.name for workspace in workspaces] == ["Default", "Notes", "Reports"]
    assert [workspace.is_default for workspace in workspaces] == [True, False, False]


def test_a_created_workspace_has_the_expected_path(account: dict[str, Any]) -> None:
    created = create_workspace(
        account["settings"], account["tenant_id"], account["user_id"], "My Project"
    )

    named = named_workspaces_root(account["settings"], account["tenant_id"], account["user_id"])
    assert created.id == "my-project"
    assert created.name == "My Project"
    assert created.is_default is False
    assert created.path == (named / "my-project").resolve()
    assert created.path.is_dir()


def test_a_named_workspace_sits_beside_the_default_one(account: dict[str, Any]) -> None:
    """A workspace never appears inside another workspace's file tree."""
    created = create_workspace(
        account["settings"], account["tenant_id"], account["user_id"], "My Project"
    )

    root = workspace_root_for(account["settings"], account["tenant_id"], account["user_id"])

    assert not created.path.is_relative_to(root)
    assert root not in created.path.parents


def test_a_directory_that_is_not_a_workspace_is_ignored(account: dict[str, Any]) -> None:
    named = named_workspaces_root(account["settings"], account["tenant_id"], account["user_id"])
    named.mkdir(parents=True, exist_ok=True)
    (named / "not a workspace").mkdir()
    (named / "notes.txt").write_text("a file, not a workspace\n", encoding="utf-8")

    workspaces = list_workspaces(account["settings"], account["tenant_id"], account["user_id"])

    assert [workspace.id for workspace in workspaces] == [DEFAULT_WORKSPACE_ID]


# --------------------------------------------------------------------------
# The name rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["", "   ", "..", "a/b", "a\\b", "with:colon", "x" * 65],
)
def test_an_unsafe_name_is_refused(account: dict[str, Any], name: str) -> None:
    with pytest.raises(InvalidWorkspaceNameError):
        create_workspace(account["settings"], account["tenant_id"], account["user_id"], name)


def test_a_duplicate_name_is_refused(account: dict[str, Any]) -> None:
    create_workspace(account["settings"], account["tenant_id"], account["user_id"], "Docs")

    with pytest.raises(DuplicateWorkspaceError):
        create_workspace(account["settings"], account["tenant_id"], account["user_id"], "Docs")


def test_a_duplicate_name_is_refused_in_another_case(account: dict[str, Any]) -> None:
    create_workspace(account["settings"], account["tenant_id"], account["user_id"], "Docs")

    with pytest.raises(DuplicateWorkspaceError):
        create_workspace(account["settings"], account["tenant_id"], account["user_id"], "docs")


# --------------------------------------------------------------------------
# The selection
# --------------------------------------------------------------------------


def test_the_selection_service_records_and_reads_back_a_selection(
    account: dict[str, Any],
) -> None:
    service = WorkspaceSelectionService(account["session"])

    recorded = service.set_workspace(
        tenant_id=account["tenant_id"],
        user_id=account["user_id"],
        conversation_id=account["conversation_id"],
        workspace="my-project",
    )

    assert recorded == {"workspace": "my-project"}
    assert (
        service.recorded_workspace(
            tenant_id=account["tenant_id"],
            user_id=account["user_id"],
            conversation_id=account["conversation_id"],
        )
        == "my-project"
    )
    assert (
        service.effective_workspace(
            tenant_id=account["tenant_id"],
            user_id=account["user_id"],
            conversation_id=account["conversation_id"],
        )
        == "my-project"
    )


def test_a_conversation_with_no_selection_uses_the_default_workspace(
    account: dict[str, Any],
) -> None:
    service = WorkspaceSelectionService(account["session"])

    assert (
        service.recorded_workspace(
            tenant_id=account["tenant_id"],
            user_id=account["user_id"],
            conversation_id=account["conversation_id"],
        )
        is None
    )
    assert (
        service.effective_workspace(
            tenant_id=account["tenant_id"],
            user_id=account["user_id"],
            conversation_id=account["conversation_id"],
        )
        == DEFAULT_WORKSPACE_ID
    )


def test_the_selection_is_recorded_in_the_typed_session_log(account: dict[str, Any]) -> None:
    WorkspaceSelectionService(account["session"]).set_workspace(
        tenant_id=account["tenant_id"],
        user_id=account["user_id"],
        conversation_id=account["conversation_id"],
        workspace="my-project",
    )

    events = (
        account["session"]
        .execute(
            select(DeepSpaceSessionEvent.event_type)
            .where(DeepSpaceSessionEvent.conversation_id == account["conversation_id"])
            .order_by(DeepSpaceSessionEvent.seq.asc())
        )
        .all()
    )

    assert WORKSPACE_SELECTED in [row[0] for row in events]


def test_another_tenants_workspace_is_not_found(account: dict[str, Any]) -> None:
    created = create_workspace(
        account["settings"], account["tenant_id"], account["user_id"], "Private"
    )
    other_tenant = uuid.uuid4()
    other_user = uuid.uuid4()

    assert find_workspace(account["settings"], other_tenant, other_user, created.id) is None
    assert (
        find_workspace(account["settings"], other_tenant, other_user, "no-such-workspace") is None
    )


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------


async def test_the_file_tools_write_in_the_contexts_workspace(account: dict[str, Any]) -> None:
    created = create_workspace(
        account["settings"], account["tenant_id"], account["user_id"], "Notes"
    )
    context = _context(account, workspace_root=created.path)

    result = await dispatch_native_tool(
        "fs_write", {"path": "note.md", "content": "hello\n"}, context
    )

    assert result is not None
    assert result["status"] == "ok"
    assert (created.path / "note.md").read_text(encoding="utf-8") == "hello\n"
    default = workspace_root_for(account["settings"], account["tenant_id"], account["user_id"])
    assert not (default / "note.md").exists()


async def test_the_file_tools_use_the_default_workspace_without_one(
    account: dict[str, Any],
) -> None:
    context = _context(account)

    result = await dispatch_native_tool(
        "fs_write", {"path": "note.md", "content": "hello\n"}, context
    )

    assert result is not None
    assert result["status"] == "ok"
    default = workspace_root_for(account["settings"], account["tenant_id"], account["user_id"])
    assert (default / "note.md").read_text(encoding="utf-8") == "hello\n"


async def test_the_search_tools_look_in_the_contexts_workspace(account: dict[str, Any]) -> None:
    """A conversation that works elsewhere does not find the default's files."""
    created = create_workspace(
        account["settings"], account["tenant_id"], account["user_id"], "Notes"
    )
    default = workspace_root_for(account["settings"], account["tenant_id"], account["user_id"])
    (default / "only-here.md").write_text("a private note\n", encoding="utf-8")
    (created.path / "there.md").write_text("a workspace note\n", encoding="utf-8")
    context = _context(account, workspace_root=created.path)

    found = await dispatch_native_tool("fs_glob", {"pattern": "**/*.md"}, context)

    assert found is not None
    assert "there.md" in str(found)
    assert "only-here.md" not in str(found)

    matched = await dispatch_native_tool("fs_grep", {"pattern": "workspace note"}, context)

    assert matched is not None
    assert "there.md" in str(matched)
    assert "only-here.md" not in str(matched)


def test_the_workspace_plugins_come_from_the_contexts_workspace(
    account: dict[str, Any],
) -> None:
    """A conversation runs the plugins its own workspace holds."""
    created = create_workspace(
        account["settings"], account["tenant_id"], account["user_id"], "Notes"
    )
    plugins = created.path / ".deepspace" / "plugins" / "greeter"
    plugins.mkdir(parents=True)
    (plugins / "deepspace-plugin.json").write_text(
        json.dumps(
            {
                "id": "greeter",
                "name": "Greeter",
                "version": "1.0.0",
                "description": "Greets.",
                "source": "workspace",
                "tools": [{"name": "greet", "description": "Greets somebody.", "mode": "read"}],
            }
        ),
        encoding="utf-8",
    )

    scope = conversation_plugin_scope(
        account["settings"],
        tenant_id=account["tenant_id"],
        user_id=account["user_id"],
        root=created.path,
    )

    assert [manifest.id for manifest in scope.workspace] == ["greeter"]


async def test_the_shell_runs_in_the_contexts_workspace(account: dict[str, Any]) -> None:
    created = create_workspace(
        account["settings"], account["tenant_id"], account["user_id"], "Notes"
    )
    context = _context(account, workspace_root=created.path)

    result = await dispatch_native_tool("bash", {"command": "pwd"}, context)

    assert result is not None
    if result["status"] == "unavailable":
        pytest.skip("This host has no shell to run the command in.")
    assert str(created.path) in str(result["output"])


async def test_a_terminal_session_starts_in_the_given_workspace(
    account: dict[str, Any],
) -> None:
    created = create_workspace(
        account["settings"], account["tenant_id"], account["user_id"], "Notes"
    )
    manager = TerminalManager()

    session = await manager.open(
        tenant_id=account["tenant_id"],
        user_id=account["user_id"],
        conversation_id=account["conversation_id"],
        settings=account["settings"],
        shell="bash",
        command="printf ready",
        workspace_root=created.path,
    )
    try:
        assert session.cwd == created.path
    finally:
        await manager.close(
            session.id,
            tenant_id=account["tenant_id"],
            user_id=account["user_id"],
            conversation_id=account["conversation_id"],
        )


async def test_the_chat_service_puts_the_conversations_workspace_on_the_context(
    account: dict[str, Any],
) -> None:
    created = create_workspace(
        account["settings"], account["tenant_id"], account["user_id"], "Notes"
    )
    WorkspaceSelectionService(account["session"]).set_workspace(
        tenant_id=account["tenant_id"],
        user_id=account["user_id"],
        conversation_id=account["conversation_id"],
        workspace=created.id,
    )
    service = DeepSpaceChatService(db=account["session"], settings=account["settings"])
    auth = SimpleNamespace(
        tenant_id=account["tenant_id"],
        user_id=account["user_id"],
    )

    result = await service._dispatch_package_tool(
        tool_name="fs_write",
        arguments={"path": "note.md", "content": "hello\n"},
        auth=auth,
        conversation_id=account["conversation_id"],
        web_provider=None,
        web_candidate=None,
        request=None,
    )

    assert result is not None
    assert result["status"] == "ok"
    assert (created.path / "note.md").read_text(encoding="utf-8") == "hello\n"
    default = workspace_root_for(account["settings"], account["tenant_id"], account["user_id"])
    assert not (default / "note.md").exists()


def test_default_workspace_resolves_to_home_deepspace_project(account: dict[str, Any]) -> None:
    empty_settings = SimpleNamespace(deepspace_workspace_root=None)
    root = workspace_root_for(empty_settings, account["tenant_id"], account["user_id"])
    expected = (Path.home() / "DeepSpace" / "Project").resolve()
    assert root == expected
    assert root.exists()


def test_create_workspace_with_custom_path(account: dict[str, Any], tmp_path: Path) -> None:
    custom_dir = tmp_path / "my_custom_code"
    custom_dir.mkdir(parents=True, exist_ok=True)
    created = create_workspace(
        account["settings"],
        account["tenant_id"],
        account["user_id"],
        "External Workspace",
        path=str(custom_dir),
    )
    assert created.name == "External Workspace"
    assert created.path == custom_dir.resolve()

    # Re-resolving through workspace_root_for returns the custom directory
    resolved = workspace_root_for(
        account["settings"],
        account["tenant_id"],
        account["user_id"],
        workspace_id=created.id,
    )
    assert resolved == custom_dir.resolve()
