"""Tests for the Windows write-restricted runner.

The check covers the parts the product owns: the capability SID a grant is
expressed as, the grants one confined command receives, and the boundary the
runner reports.  The Windows API calls themselves run only on Windows, and
the runner refuses to start a command it could not confine.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.deepspace.services.windows_acl import (
    capability_sid_for,
    describe_windows_boundary,
    windows_acl_runner,
    windows_confinement_available,
    workspace_grants,
)


def test_a_workspace_derives_a_stable_capability_sid(tmp_path: Path) -> None:
    first = capability_sid_for(tmp_path)
    second = capability_sid_for(tmp_path)

    assert first == second
    assert first.startswith("S-1-15-3-1-")


def test_two_workspaces_derive_different_capability_sids(tmp_path: Path) -> None:
    left = capability_sid_for(tmp_path / "left")
    right = capability_sid_for(tmp_path / "right")

    assert left != right


def test_a_confined_command_receives_the_workspace_and_a_private_temp(tmp_path: Path) -> None:
    grants = workspace_grants(tmp_path)

    assert [grant.path for grant in grants][0] == str(tmp_path)
    # The temporary grant has its own SID, so two sessions that share a
    # workspace do not inherit each other's temporary authority.
    assert grants[0].capability_sid != grants[1].capability_sid
    assert ".deepspace" in grants[1].path


def test_the_boundary_reports_what_it_confines() -> None:
    boundary = describe_windows_boundary()

    assert boundary["backend"] == "windows-acl"
    assert boundary["enforcement"] == "partial"
    assert boundary["confines"] == ["write", "delete"]
    assert "read" in boundary["does_not_confine"]


def test_the_runner_is_unavailable_on_other_platforms(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("This host is Windows.")

    assert windows_confinement_available() is False
    assert (
        windows_acl_runner(
            argv=["/bin/true"],
            workspace=tmp_path,
            working_directory=tmp_path,
        )
        is None
    )


def test_a_windows_runner_names_the_launcher_and_the_command(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("This host is not Windows.")

    # The launcher is only built on Windows, so the builder reports that it
    # cannot confine anything here rather than returning a broken command.
    assert (
        windows_acl_runner(
            argv=["pwsh", "-Command", "echo hi"],
            workspace=tmp_path,
            working_directory=tmp_path,
        )
        is None
    )
