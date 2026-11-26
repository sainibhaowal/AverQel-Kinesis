from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit_no_db


def test_desktop_workflow_builds_artifacts_manually_and_releases_only_from_tags() -> None:
    workflow = (ROOT / ".github/workflows/desktop-packaging.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "push:\n    tags:" in workflow
    assert "\n  pull_request:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "uses: actions/upload-artifact@v4" in workflow
    assert "uses: softprops/action-gh-release@v2" in workflow
    assert "if: github.event_name == 'push'" in workflow
    assert "Stamp desktop package version from release tag" in workflow
    assert 'node .github/scripts/stamp-desktop-version.mjs "${GITHUB_REF_NAME#v}"' in workflow
    assert "|| true" not in workflow
    assert "deploy-vps" not in workflow
    assert not (ROOT / ".github/workflows/desktop-artifacts.yml").exists()


def test_pull_request_ci_has_one_required_aggregate_gate() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert '      - "codex/ci/**"' not in workflow
    assert "name: CI Passed" in workflow
    assert "name: Backend quality" in workflow
    assert "name: Frontend quality" in workflow
    assert "name: Browser E2E" in workflow
    assert "name: Desktop Linux packaged runtime" in workflow
    assert "name: DeepSpace Windows native runtime" in workflow
    assert "deepspace-windows-native:$DEEPSPACE_WINDOWS_NATIVE" in workflow
    assert "needs:" in workflow
    assert "DEPENDENCY_REVIEW: ${{ needs.dependency-review.result }}" in workflow


def test_native_runtime_workflow_is_reusable_and_manually_dispatchable() -> None:
    workflow = (ROOT / ".github/workflows/deepspace-native-runtime-tests.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "workflow_call:" in workflow
    assert "persist-credentials: false" in workflow


def test_packaged_backend_smoke_runs_before_artifact_upload() -> None:
    workflow = (ROOT / ".github/workflows/desktop-packaging.yml").read_text(encoding="utf-8")

    smoke_index = workflow.index("Smoke-test packaged local backend")
    upload_steps = (
        "Upload Linux DEB installer",
        "Upload Linux RPM installer",
        "Upload Linux AppImage installer",
        "Upload Windows installer",
        "Upload macOS disk image",
    )
    assert all(smoke_index < workflow.index(step) for step in upload_steps)


def test_desktop_release_requires_signing_and_native_target_validation() -> None:
    workflow = (ROOT / ".github/workflows/desktop-packaging.yml").read_text(encoding="utf-8")

    for secret in (
        "MACOS_CSC_LINK",
        "MACOS_CSC_KEY_PASSWORD",
        "WINDOWS_CSC_LINK",
        "WINDOWS_CSC_KEY_PASSWORD",
        "APPLE_APP_SPECIFIC_PASSWORD",
    ):
        assert secret in workflow
    assert "Get-AuthenticodeSignature" in workflow
    assert "xcrun stapler validate" in workflow
    assert "create-release-manifest.py" in workflow
    assert (
        '--base-url "https://github.com/${GITHUB_REPOSITORY}/releases/latest/download"' in workflow
    )
    assert "contents: write" in workflow
    assert "id-token: write" not in workflow


def test_desktop_matrix_selection_covers_each_native_platform_target() -> None:
    import importlib.util

    selector_path = ROOT / ".github/scripts/select-desktop-build-matrix.py"
    spec = importlib.util.spec_from_file_location("desktop_build_matrix", selector_path)
    assert spec is not None and spec.loader is not None
    selector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selector)

    targets = selector.select_targets("push", "", "")
    assert {(item["platform"], item["arch"]) for item in targets} == {
        ("linux", "x64"),
        ("linux", "arm64"),
        ("windows", "x64"),
        ("windows", "arm64"),
        ("macos", "x64"),
        ("macos", "arm64"),
    }
    linux_arm = selector.select_targets("workflow_dispatch", "linux", "arm64")
    assert [(item["platform"], item["arch"]) for item in linux_arm] == [("linux", "arm64")]
