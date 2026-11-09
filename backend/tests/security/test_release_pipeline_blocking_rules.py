from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit_no_db


def test_desktop_artifact_workflow_is_scoped_to_builds() -> None:
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert '      - "codex/ci/**"' in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "uses: actions/upload-artifact@v4" in workflow
    assert "actions/create-release" not in workflow
    assert "deploy-vps" not in workflow


def test_packaged_backend_smoke_runs_before_artifact_upload() -> None:
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")

    assert workflow.index("Smoke-test packaged local backend") < workflow.index(
        "Upload installer artifacts"
    )
