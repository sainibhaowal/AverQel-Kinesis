"""Tests for the extensions a deployment installs for every conversation.

The checks cover the directory the deployment names, the default directory,
the precedence of a conversation's own extension, and the way the tool
restrictions of both sides combine.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services.extensions import (
    EXTENSIONS_DIRECTORY,
    deployment_extensions_root,
    extensions_for,
    load_deployment_extensions,
)

pytestmark = pytest.mark.unit_no_db


def _settings(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    """Build the deployment settings one test needs.

    Args:
        tmp_path: The directory the test owns.
        overrides: The settings the test names.

    Returns:
        The settings the extension service reads.
    """
    values: dict[str, object] = {
        "deepspace_extensions_dir": str(tmp_path / "deployment-extensions"),
        "local_storage_path": str(tmp_path / "storage"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _write_manifest(directory: Path, name: str, document: dict[str, object]) -> None:
    """Write one extension manifest into a directory.

    Args:
        directory: The directory that holds the manifests.
        name: The manifest's file name.
        document: The manifest's contents.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")


def test_the_deployment_directory_is_the_one_the_setting_names(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert deployment_extensions_root(settings) == tmp_path / "deployment-extensions"


def test_the_deployment_directory_defaults_to_the_storage_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path, deepspace_extensions_dir=None)
    assert deployment_extensions_root(settings) == tmp_path / "storage" / "extensions"


def test_a_deployment_without_a_storage_path_has_no_deployment_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path, deepspace_extensions_dir=None, local_storage_path="")
    assert deployment_extensions_root(settings) is None
    assert load_deployment_extensions(settings).extensions == ()


def test_the_deployment_extensions_are_read_for_every_conversation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_manifest(
        tmp_path / "deployment-extensions",
        "house-style",
        {"name": "house-style", "guidance": "Answer in the house style."},
    )
    loaded = extensions_for(
        settings,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        root=tmp_path / "workspace",
    )
    assert [extension.name for extension in loaded.extensions] == ["house-style"]
    assert loaded.guidance == "Answer in the house style."


def test_a_conversation_extension_replaces_the_deployment_extension_of_the_same_name(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _write_manifest(
        tmp_path / "deployment-extensions",
        "helper",
        {"name": "helper", "guidance": "The deployment guidance."},
    )
    workspace = tmp_path / "workspace"
    _write_manifest(
        workspace / EXTENSIONS_DIRECTORY,
        "helper",
        {"name": "helper", "guidance": "The conversation guidance."},
    )
    loaded = extensions_for(
        settings,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        root=workspace,
    )
    assert [extension.name for extension in loaded.extensions] == ["helper"]
    assert loaded.guidance == "The conversation guidance."


def test_the_tool_restrictions_of_both_sides_are_combined(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_manifest(
        tmp_path / "deployment-extensions",
        "policy",
        {"name": "policy", "deny_tools": ["bash"], "approval_tools": ["fs_delete"]},
    )
    workspace = tmp_path / "workspace"
    _write_manifest(
        workspace / EXTENSIONS_DIRECTORY,
        "own",
        {"name": "own", "deny_tools": ["pwsh"], "approval_tools": ["fs_write"]},
    )
    loaded = extensions_for(
        settings,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        root=workspace,
    )
    assert loaded.denied_tools == frozenset({"bash", "pwsh"})
    assert loaded.approval_tools == frozenset({"fs_delete", "fs_write"})


def test_an_alias_both_sides_declare_keeps_the_deployment_one(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_manifest(
        tmp_path / "deployment-extensions",
        "policy",
        {"name": "policy", "aliases": {"read_file": {"tool": "fs_read"}}},
    )
    workspace = tmp_path / "workspace"
    _write_manifest(
        workspace / EXTENSIONS_DIRECTORY,
        "own",
        {"name": "own", "aliases": {"read_file": {"tool": "url_read"}}},
    )
    loaded = extensions_for(
        settings,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        root=workspace,
    )
    assert loaded.aliases["read_file"].target == "fs_read"


def test_a_manifest_that_cannot_be_parsed_is_skipped(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    directory = tmp_path / "deployment-extensions"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "broken.json").write_text("{not json", encoding="utf-8")
    _write_manifest(directory, "good", {"name": "good"})
    loaded = load_deployment_extensions(settings)
    assert [extension.name for extension in loaded.extensions] == ["good"]
