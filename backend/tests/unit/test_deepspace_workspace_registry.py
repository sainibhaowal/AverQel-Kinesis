"""Tests for the durable workspace registry.

The registry keeps the user's project order, stable identities, session
positions, and crash recovery without taking ownership of the project
directories themselves.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.deepspace.services.workspace_registry import (
    QUARANTINE_DIRNAME,
    REGISTRY_FILENAME,
    WORKSPACE_MARKER_FILENAME,
    WorkspaceRegistry,
    WorkspaceUnavailableError,
    atomic_write_json,
)
from app.deepspace.services.workspace_root import workspace_root_for

pytestmark = pytest.mark.unit_no_db


@pytest.fixture
def account(tmp_path: Path) -> tuple[Settings, uuid.UUID, uuid.UUID]:
    """Build one account's settings and identifiers."""
    settings = Settings(
        deepspace_workspace_root=str(tmp_path / "workspaces"),
        local_storage_path=str(tmp_path / "storage"),
    )
    return settings, uuid.uuid4(), uuid.uuid4()


def _registry(account: tuple[Settings, uuid.UUID, uuid.UUID]) -> WorkspaceRegistry:
    settings, tenant_id, user_id = account
    return WorkspaceRegistry(settings, tenant_id, user_id)


def test_create_writes_a_marker_and_the_manifest(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)

    record = registry.create("alpha", "Alpha")

    assert record.id == "alpha"
    marker = registry.named_root / "alpha" / WORKSPACE_MARKER_FILENAME
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["id"] == "alpha"
    assert payload["name"] == "Alpha"
    manifest = json.loads((registry.named_root / REGISTRY_FILENAME).read_text(encoding="utf-8"))
    assert manifest["version"] == 2
    assert manifest["order"] == ["alpha"]


def test_rename_keeps_identity_and_directory(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    registry.create("alpha", "Alpha")

    renamed = registry.rename("alpha", "Reports")

    assert renamed.id == "alpha"
    assert renamed.name == "Reports"
    assert renamed.effective_path(registry.named_root) == registry.named_root / "alpha"
    assert registry.catalogue()[0].name == "Reports"
    assert not (registry.named_root / "reports").exists()


def test_rename_refuses_another_registrations_name(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    registry.create("alpha", "Alpha")
    registry.create("beta", "Beta")

    with pytest.raises(Exception, match="already exists"):
        registry.rename("alpha", "beta")


def test_move_orders_projects_before_another_and_at_the_end(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    registry.create("alpha", "Alpha")
    registry.create("beta", "Beta")
    registry.create("gamma", "Gamma")

    assert registry.move("gamma", "alpha") == ["gamma", "alpha", "beta"]
    assert registry.move("gamma", None) == ["alpha", "beta", "gamma"]
    assert [record.id for record in registry.catalogue()] == ["alpha", "beta", "gamma"]


def test_delete_quarantines_internal_data_instead_of_destroying_it(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    registry.create("alpha", "Alpha")
    (registry.named_root / "alpha" / "notes.txt").write_text("keep me", encoding="utf-8")

    assert registry.delete("alpha") is True

    assert registry.directory_for("alpha") is None
    assert registry.catalogue() == []
    quarantined = list((registry.named_root / QUARANTINE_DIRNAME).iterdir())
    assert len(quarantined) == 1
    assert (quarantined[0] / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_delete_preserves_an_external_project_directory(
    account: tuple[Settings, uuid.UUID, uuid.UUID], tmp_path: Path
) -> None:
    registry = _registry(account)
    external = tmp_path / "external-project"
    external.mkdir()
    (external / "main.py").write_text("print('hello')", encoding="utf-8")
    registry.create("external", "External", external)

    assert registry.delete("external") is True

    assert (external / "main.py").read_text(encoding="utf-8") == "print('hello')"
    assert not (registry.named_root / "external").exists()


def test_a_missing_directory_is_reported_and_not_recreated(
    account: tuple[Settings, uuid.UUID, uuid.UUID], tmp_path: Path
) -> None:
    settings, tenant_id, user_id = account
    registry = _registry(account)
    registry.create("alpha", "Alpha")
    moved = tmp_path / "moved-away"
    (registry.named_root / "alpha").rename(moved)

    record = registry.catalogue()[0]

    assert record.status(registry.named_root) == "missing-dir"
    assert not (registry.named_root / "alpha").exists()
    with pytest.raises(WorkspaceUnavailableError, match="missing"):
        workspace_root_for(settings, tenant_id, user_id, "alpha")


def test_a_corrupt_manifest_is_quarantined_and_rebuilt_from_markers(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    registry.create("alpha", "Alpha")
    manifest_path = registry.named_root / REGISTRY_FILENAME
    manifest_path.write_text("{not json", encoding="utf-8")

    records = registry.catalogue()

    assert [record.id for record in records] == ["alpha"]
    assert any(
        path.name.startswith(f"{REGISTRY_FILENAME}.corrupt-")
        for path in registry.named_root.iterdir()
    )
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["order"] == ["alpha"]


def test_a_tombstone_finishes_a_crash_interrupted_delete(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    registry.create("alpha", "Alpha")
    (registry.named_root / "alpha" / "notes.txt").write_text("keep me", encoding="utf-8")
    manifest_path = registry.named_root / REGISTRY_FILENAME
    manifest_path.write_text(
        json.dumps(
            {
                "version": 2,
                "workspaces": [],
                "order": [],
                "positions": {},
                "tombstones": ["alpha"],
            }
        ),
        encoding="utf-8",
    )

    assert registry.catalogue() == []

    assert not (registry.named_root / "alpha").exists()
    assert (list((registry.named_root / QUARANTINE_DIRNAME).iterdir())[0] / "notes.txt").read_text(
        encoding="utf-8"
    ) == "keep me"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tombstones"] == []


def test_a_legacy_marker_without_an_identifier_is_imported(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    legacy = registry.named_root / "legacy-project"
    legacy.mkdir(parents=True)
    (legacy / WORKSPACE_MARKER_FILENAME).write_text(
        json.dumps({"name": "Legacy Project"}), encoding="utf-8"
    )

    records = registry.catalogue()

    assert [record.id for record in records] == ["legacy-project"]
    assert records[0].name == "Legacy Project"


def test_a_newer_manifest_is_not_overwritten(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    registry.named_root.mkdir(parents=True, exist_ok=True)
    (registry.named_root / REGISTRY_FILENAME).write_text(
        json.dumps({"version": 99, "workspaces": [], "order": []}), encoding="utf-8"
    )

    with pytest.raises(WorkspaceUnavailableError, match="newer"):
        registry.catalogue()


def test_atomic_write_replaces_the_document_without_a_temporary_leftover(
    tmp_path: Path,
) -> None:
    target = tmp_path / "registry.json"

    atomic_write_json(target, {"value": 1})
    atomic_write_json(target, {"value": 2})

    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 2}
    assert list(tmp_path.glob("*.tmp")) == []


def test_session_order_is_stored_deduplicated_and_inserted(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    first, second = uuid.uuid4(), uuid.uuid4()

    stored = registry.order_sessions("default", [str(first), str(first), str(second)])
    assert stored == [str(first), str(second)]
    assert registry.session_order("default") == [str(first), str(second)]

    moved = registry.insert_session("default", str(second), str(first))
    assert moved == [str(second), str(first)]

    registry.prune_sessions({str(first)})
    assert registry.session_order("default") == [str(first)]


def test_a_missing_directory_does_not_lose_the_registration(
    account: tuple[Settings, uuid.UUID, uuid.UUID], tmp_path: Path
) -> None:
    registry = _registry(account)
    registry.create("alpha", "Alpha")
    (registry.named_root / "alpha").rename(tmp_path / "elsewhere")

    records = registry.catalogue()

    assert len(records) == 1
    assert records[0].id == "alpha"
    assert records[0].status(registry.named_root) == "missing-dir"


def test_an_unknown_identifier_is_not_found(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)

    assert registry.directory_for("absent") is None
    assert registry.delete("absent") is False


def test_names_are_not_accepted_as_paths(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)

    with pytest.raises(Exception):  # noqa: B017 - the registry rejects bad ids
        registry.create("../escape", "Escape")


def test_positions_for_other_workspaces_are_preserved(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    conversation = str(uuid.uuid4())
    registry.order_sessions("alpha", [conversation])

    registry.create("alpha", "Alpha")
    registry.prune_sessions({conversation})

    assert registry.session_order("alpha") == [conversation]


def test_manifest_records_every_project_without_directories(
    account: tuple[Settings, uuid.UUID, uuid.UUID], tmp_path: Path
) -> None:
    registry = _registry(account)
    registry.create("alpha", "Alpha")
    (registry.named_root / "alpha").rename(tmp_path / "gone")

    payload = json.loads((registry.named_root / REGISTRY_FILENAME).read_text(encoding="utf-8"))
    assert [item["id"] for item in payload["workspaces"]] == ["alpha"]


def test_the_registry_is_created_under_the_account_root(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    settings, tenant_id, user_id = account
    registry = _registry(account)
    registry.create("alpha", "Alpha")

    expected = (
        Path(str(settings.deepspace_workspace_root)) / str(tenant_id) / f"{user_id}.workspaces"
    )
    assert registry.named_root == expected.resolve()


def test_unicode_names_round_trip(account: tuple[Settings, uuid.UUID, uuid.UUID]) -> None:
    registry = _registry(account)

    registry.create("alpha", "Rapports été")

    assert registry.catalogue()[0].name == "Rapports été"
    marker = json.loads(
        (registry.named_root / "alpha" / WORKSPACE_MARKER_FILENAME).read_text(encoding="utf-8")
    )
    assert marker["name"] == "Rapports été"


def test_a_directory_that_is_not_a_registration_is_ignored(
    account: tuple[Settings, uuid.UUID, uuid.UUID],
) -> None:
    registry = _registry(account)
    (registry.named_root / "not a workspace!").mkdir(parents=True)

    assert registry.catalogue() == []
