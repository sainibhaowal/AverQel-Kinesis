from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit_no_db


def test_desktop_artifact_workflow_is_manual_only_and_scoped_to_builds() -> None:
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "\n  push:" not in workflow
    assert "\n  pull_request:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "uses: actions/upload-artifact@v4" in workflow
    assert "actions/create-release" not in workflow
    assert "deploy-vps" not in workflow


def test_pull_request_ci_has_one_required_aggregate_gate() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert '      - "codex/ci/**"' not in workflow
    assert "name: CI Passed" in workflow
    assert "name: Backend quality" in workflow
    assert "name: Frontend quality" in workflow
    assert "name: Browser E2E" in workflow
    assert "name: Desktop Linux packaged runtime" in workflow
    assert "needs:" in workflow
    assert "DEPENDENCY_REVIEW: ${{ needs.dependency-review.result }}" in workflow


def test_native_runtime_workflow_is_manual_only() -> None:
    workflow = (ROOT / ".github/workflows/deepspace-native-runtime-tests.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "\n  push:" not in workflow
    assert "\n  pull_request:" not in workflow


def test_packaged_backend_smoke_runs_before_artifact_upload() -> None:
    workflow = (ROOT / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")

    smoke_index = workflow.index("Smoke-test packaged local backend")
    upload_steps = (
        "Upload Linux DEB installer",
        "Upload Linux RPM installer",
        "Upload Linux AppImage installer",
        "Upload Windows installer",
        "Upload macOS disk image",
    )
    assert all(smoke_index < workflow.index(step) for step in upload_steps)
