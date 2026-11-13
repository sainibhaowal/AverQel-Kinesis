"""Tests for the packaged runtime dependency payload."""

from __future__ import annotations

import json
import platform
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services.runtime_manifest import RuntimeManifestError
from app.deepspace.services.tools.dependencies import (
    ensure_runtime_payload,
    workspace_dependencies,
)

pytestmark = pytest.mark.unit_no_db


def _settings(source: Path | None = None, target: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        deepspace_runtime_payload_source=str(source) if source else "",
        deepspace_runtime_payload_root=str(target) if target else "",
        deepspace_runtime_manifest_path="",
    )


def test_no_configured_payload_is_a_no_op() -> None:
    assert ensure_runtime_payload(_settings()) is None


def test_payload_installs_atomically_and_reports_present(tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    (source / "runtime.json").write_text("{}", encoding="utf-8")
    (source / "lib").mkdir()
    (source / "lib" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    target = tmp_path / "runtime" / "dependencies"
    settings = _settings(source, target)

    installed = ensure_runtime_payload(settings)
    assert installed == {
        "status": "installed",
        "path": str(target),
        "source": str(source),
    }
    assert (target / "runtime.json").is_file()
    assert (target / "lib" / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    again = ensure_runtime_payload(settings)
    assert again["status"] == "present"


def test_interrupted_payload_is_repaired_without_deleting_user_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    (source / "runtime.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "dependencies"
    target.mkdir()
    (target / "keep.txt").write_text("user data", encoding="utf-8")
    settings = _settings(source, target)

    repaired = ensure_runtime_payload(settings)
    assert repaired["status"] == "repaired"
    assert (target / "keep.txt").read_text(encoding="utf-8") == "user data"
    assert (target / "runtime.json").is_file()


def test_missing_source_and_missing_manifest_are_reported(tmp_path: Path) -> None:
    with pytest.raises(RuntimeManifestError, match="not a directory"):
        ensure_runtime_payload(_settings(tmp_path / "missing", tmp_path / "target"))

    source = tmp_path / "bundle"
    source.mkdir()
    (source / "file.txt").write_text("no manifest", encoding="utf-8")
    with pytest.raises(RuntimeManifestError, match="runtime.json"):
        ensure_runtime_payload(_settings(source, tmp_path / "target"))


def test_dependency_report_uses_the_installed_manifest(tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    (source / "python").write_text("", encoding="utf-8")
    system = platform.system().casefold()
    platform_name = "linux" if system == "linux" else "darwin" if system == "darwin" else "win32"
    (source / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "platform": platform_name,
                "architecture": platform.machine(),
                "python": {"version": "3.12.0", "path": "python"},
                "packages": {},
                "executables": {},
            }
        ),
        encoding="utf-8",
    )
    target = tmp_path / "dependencies"
    report = workspace_dependencies(_settings(source, target))

    assert report["payload"]["status"] == "installed"
    assert report["runtime"]["source"] == "manifest"
    assert report["runtime"]["platform"] == platform_name
    assert report["runtime"]["python_version"] == "3.12.0"
    assert report["python"] == str((target / "python").resolve())
    assert report["office_runtime_sources"]["soffice"] == "unavailable"
