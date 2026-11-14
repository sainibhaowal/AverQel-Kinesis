"""Tests for the routes that manage one workspace's workflow documents.

Most tests call the routes directly with a temporary workspace as the root,
so they cover the behaviour a caller sees: what is listed, what is refused,
and what a write changes on disk.  The last test reaches the same routes
over HTTP, so the route paths, methods, and dependencies are proven to be
wired into the application.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth.dependencies import AuthContext
from app.deepspace.api import workspace as workspace_api

pytestmark = pytest.mark.unit_no_db


def _auth() -> AuthContext:
    """Build the caller the routes run under."""
    return AuthContext(
        tenant_id=uuid4(),
        user_id=uuid4(),
        roles=frozenset({"user"}),
        token_id="test",
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the conversation's workspace at a temporary directory."""
    monkeypatch.setattr(
        workspace_api,
        "_conversation_workspace",
        lambda *_args, **_kwargs: tmp_path,
    )
    monkeypatch.setattr(workspace_api, "_require_workspace_write", lambda *_args, **_kwargs: None)
    return tmp_path


def test_the_workflow_directory_is_inside_the_workspace(tmp_path: Path) -> None:
    """Workflow documents live under the workspace's own directory."""
    directory = workspace_api._workflow_directory(tmp_path)
    assert directory == tmp_path / ".averqel" / "workflows"


@pytest.mark.parametrize(
    "name",
    ["../escape", "nested/name", "with space", "", ".hidden", "x" * 121],
)
def test_an_unsafe_workflow_name_is_refused(tmp_path: Path, name: str) -> None:
    """A name that could leave the workflow directory is refused."""
    with pytest.raises(HTTPException) as caught:
        workspace_api._workflow_document_path(tmp_path, name)
    assert caught.value.status_code == 400


def test_a_safe_workflow_name_resolves_inside_the_directory(tmp_path: Path) -> None:
    """A safe name resolves to the document inside the workflow directory."""
    target = workspace_api._workflow_document_path(tmp_path, "review-files")
    assert target == tmp_path / ".averqel" / "workflows" / "review-files.json"


def test_listing_reports_the_installed_workflows(workspace: Path) -> None:
    """The listing names each document and counts its steps."""
    directory = workspace / ".averqel" / "workflows"
    directory.mkdir(parents=True)
    (directory / "alpha.json").write_text(
        json.dumps({"steps": [{"id": "a", "task": "one"}, {"id": "b", "task": "two"}]}),
        encoding="utf-8",
    )
    (directory / "broken.json").write_text("not json", encoding="utf-8")

    payload = workspace_api.list_workspace_workflows(
        uuid4(), _auth(), db=SimpleNamespace(), settings=SimpleNamespace()
    )

    by_name = {item["name"]: item for item in payload["workflows"]}
    assert set(by_name) == {"alpha", "broken"}
    assert by_name["alpha"]["valid"] is True
    assert by_name["alpha"]["step_count"] == 2
    assert by_name["broken"]["valid"] is False


def test_reading_a_workflow_returns_its_document(workspace: Path) -> None:
    """The read route returns the document and its source."""
    directory = workspace / ".averqel" / "workflows"
    directory.mkdir(parents=True)
    (directory / "alpha.json").write_text(
        json.dumps({"steps": [{"id": "a", "task": "one"}]}),
        encoding="utf-8",
    )

    payload = workspace_api.read_workspace_workflow(
        uuid4(), "alpha", _auth(), db=SimpleNamespace(), settings=SimpleNamespace()
    )

    assert payload["name"] == "alpha"
    assert payload["document"]["steps"][0]["id"] == "a"
    assert '"steps"' in payload["content"]


def test_reading_a_missing_workflow_is_a_not_found(workspace: Path) -> None:
    """A workflow that was never installed is reported as missing."""
    with pytest.raises(HTTPException) as caught:
        workspace_api.read_workspace_workflow(
            uuid4(), "absent", _auth(), db=SimpleNamespace(), settings=SimpleNamespace()
        )
    assert caught.value.status_code == 404


def test_writing_a_workflow_stores_it_in_the_workspace(workspace: Path) -> None:
    """A write creates the document and reports its step count."""
    payload = workspace_api.write_workspace_workflow(
        uuid4(),
        "alpha",
        {"document": {"steps": [{"id": "a", "task": "one"}]}},
        _auth(),
        db=SimpleNamespace(),
        settings=SimpleNamespace(),
    )

    assert payload["name"] == "alpha"
    assert payload["step_count"] == 1
    stored = json.loads((workspace / ".averqel" / "workflows" / "alpha.json").read_text())
    assert stored["steps"][0]["task"] == "one"


def test_writing_an_invalid_workflow_is_refused(workspace: Path) -> None:
    """A document with no usable step is refused and nothing is written."""
    with pytest.raises(HTTPException) as caught:
        workspace_api.write_workspace_workflow(
            uuid4(),
            "broken",
            {"document": {"steps": []}},
            _auth(),
            db=SimpleNamespace(),
            settings=SimpleNamespace(),
        )

    assert caught.value.status_code == 422
    assert not (workspace / ".averqel" / "workflows" / "broken.json").exists()


def test_writing_a_document_that_names_an_unknown_dependency_is_refused(
    workspace: Path,
) -> None:
    """A step that depends on a step nobody declared is refused."""
    with pytest.raises(HTTPException) as caught:
        workspace_api.write_workspace_workflow(
            uuid4(),
            "broken",
            {"document": {"steps": [{"id": "a", "task": "one", "depends_on": ["ghost"]}]}},
            _auth(),
            db=SimpleNamespace(),
            settings=SimpleNamespace(),
        )
    assert caught.value.status_code == 422


def test_deleting_a_workflow_removes_it(workspace: Path) -> None:
    """A delete removes the document from the workspace."""
    directory = workspace / ".averqel" / "workflows"
    directory.mkdir(parents=True)
    (directory / "alpha.json").write_text(
        json.dumps({"steps": [{"id": "a", "task": "one"}]}), encoding="utf-8"
    )

    payload = workspace_api.delete_workspace_workflow(
        uuid4(), "alpha", _auth(), db=SimpleNamespace(), settings=SimpleNamespace()
    )

    assert payload["deleted"] is True
    assert not (directory / "alpha.json").exists()


def test_deleting_a_missing_workflow_is_a_not_found(workspace: Path) -> None:
    """A workflow that was never installed is reported as missing."""
    with pytest.raises(HTTPException) as caught:
        workspace_api.delete_workspace_workflow(
            uuid4(), "absent", _auth(), db=SimpleNamespace(), settings=SimpleNamespace()
        )
    assert caught.value.status_code == 404


def test_a_read_only_conversation_may_not_save_a_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation in read-only mode may run a workflow but not install one."""
    monkeypatch.setattr(
        workspace_api.SandboxModeService,
        "effective_mode",
        lambda *_args, **_kwargs: "read-only",
    )
    with pytest.raises(HTTPException) as caught:
        workspace_api._require_workspace_write(
            SimpleNamespace(), _auth(), uuid4(), SimpleNamespace()
        )
    assert caught.value.status_code == 403


def test_a_writable_conversation_may_save_a_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation that may write is allowed to install a workflow."""
    monkeypatch.setattr(
        workspace_api.SandboxModeService,
        "effective_mode",
        lambda *_args, **_kwargs: "workspace-write",
    )
    workspace_api._require_workspace_write(SimpleNamespace(), _auth(), uuid4(), SimpleNamespace())


def test_the_routes_are_reachable_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow-document routes answer on their own paths and methods.

    The other tests call the route functions directly.  This one reaches
    them through the application, so a route that was never registered, or
    registered under the wrong path or method, is caught here.
    """
    monkeypatch.setattr(
        workspace_api,
        "_conversation_workspace",
        lambda *_args, **_kwargs: tmp_path,
    )
    monkeypatch.setattr(workspace_api, "_require_workspace_write", lambda *_args, **_kwargs: None)

    app = FastAPI()
    app.dependency_overrides[workspace_api.get_auth_context] = _auth
    app.dependency_overrides[workspace_api.get_db] = lambda: SimpleNamespace()
    app.dependency_overrides[workspace_api.get_settings] = lambda: SimpleNamespace()
    app.include_router(workspace_api.router, prefix="/api/v1")

    conversation_id = uuid4()
    base = f"/api/v1/deepspace/chats/{conversation_id}/workspace/workflows"
    with TestClient(app) as client:
        created = client.put(
            f"{base}/http-audit",
            json={"document": {"steps": [{"id": "read", "task": "Read the files."}]}},
        )
        assert created.status_code == 200
        assert created.json()["step_count"] == 1

        listed = client.get(base)
        assert listed.status_code == 200
        assert [item["name"] for item in listed.json()["workflows"]] == ["http-audit"]

        read = client.get(f"{base}/http-audit")
        assert read.status_code == 200
        assert read.json()["document"]["steps"][0]["id"] == "read"

        deleted = client.delete(f"{base}/http-audit")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        missing = client.get(f"{base}/http-audit")
        assert missing.status_code == 404
