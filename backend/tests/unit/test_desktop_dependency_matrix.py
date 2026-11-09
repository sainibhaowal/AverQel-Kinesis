from __future__ import annotations

from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from app.providers.services.builtin_local_models import (
    local_model_inference_unsupported_reason,
)

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "requirements.txt"
pytestmark = pytest.mark.unit_no_db


def _selected_pins(*, sys_platform: str, platform_machine: str) -> dict[str, list[str]]:
    environment = default_environment()
    environment.update(
        {
            "sys_platform": sys_platform,
            "platform_machine": platform_machine,
            "python_version": "3.12",
            "python_full_version": "3.12.14",
        }
    )
    selected: dict[str, list[str]] = {}

    for raw_line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue
        selected.setdefault(canonicalize_name(requirement.name), []).append(
            str(requirement.specifier)
        )
    return selected


def _only_pin(selected: dict[str, list[str]], package: str) -> str:
    pins = selected[canonicalize_name(package)]
    assert len(pins) == 1
    return pins[0]


def test_intel_macos_omits_the_local_ml_stack() -> None:
    selected = _selected_pins(sys_platform="darwin", platform_machine="x86_64")

    for package in ("torch", "transformers", "tokenizers", "sentence-transformers", "onnxruntime"):
        assert canonicalize_name(package) not in selected
    assert _only_pin(selected, "huggingface-hub") == "==1.9.0"
    assert _only_pin(selected, "cryptography") == "==50.0.2"
    assert local_model_inference_unsupported_reason(
        sys_platform="darwin", platform_machine="x86_64"
    )
    assert local_model_inference_unsupported_reason(sys_platform="darwin", platform_machine="AMD64")


@pytest.mark.parametrize(
    ("sys_platform", "platform_machine"),
    [
        ("darwin", "arm64"),
        ("linux", "x86_64"),
        ("linux", "aarch64"),
        ("win32", "AMD64"),
    ],
)
def test_other_desktop_targets_keep_the_current_ai_runtime(
    sys_platform: str, platform_machine: str
) -> None:
    selected = _selected_pins(
        sys_platform=sys_platform,
        platform_machine=platform_machine,
    )

    assert _only_pin(selected, "torch") == "==2.13.0"
    assert _only_pin(selected, "transformers") == "==5.10.4"
    assert _only_pin(selected, "sentence-transformers") == "==5.6.0"
    assert _only_pin(selected, "cryptography") == "==50.0.2"


def test_windows_arm64_build_uses_the_x64_python_runtime_for_full_ai_support() -> None:
    # The ARM64 Electron shell bundles this x64 backend under Windows 11 emulation.
    selected = _selected_pins(sys_platform="win32", platform_machine="AMD64")

    assert _only_pin(selected, "torch") == "==2.13.0"
    assert _only_pin(selected, "transformers") == "==5.10.4"
    assert _only_pin(selected, "sentence-transformers") == "==5.6.0"
    assert _only_pin(selected, "onnxruntime") == "==1.27.0"
    workflow = (ROOT.parent / ".github/workflows/desktop-artifacts.yml").read_text(encoding="utf-8")
    assert "Set up x64 Python for the Windows ARM64 AI backend" in workflow
    assert "architecture: x64" in workflow


def test_intel_macos_omits_onnxruntime() -> None:
    selected = _selected_pins(sys_platform="darwin", platform_machine="x86_64")

    assert canonicalize_name("onnxruntime") not in selected
    assert (
        local_model_inference_unsupported_reason(sys_platform="darwin", platform_machine="x86_64")
        is not None
    )


@pytest.mark.parametrize(
    ("sys_platform", "platform_machine"),
    [("darwin", "arm64"), ("linux", "x86_64"), ("win32", "AMD64")],
)
def test_other_targets_keep_local_model_inference_enabled(
    sys_platform: str, platform_machine: str
) -> None:
    assert (
        local_model_inference_unsupported_reason(
            sys_platform=sys_platform, platform_machine=platform_machine
        )
        is None
    )
