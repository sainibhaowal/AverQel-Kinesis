"""Tests for the remote command runner.

The remote boundary is safe because of two rules: the host key is checked
against the reviewed entry, and the private key never leaves the server.
These tests cover those rules and the failure modes around them.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.deepspace.services.ssh import (
    MAX_COMMAND_CHARS,
    SshError,
    SshHost,
    SshTrustError,
    build_remote_command,
    build_ssh_argv,
    remote_environment,
    resolve_key_path,
)

pytestmark = pytest.mark.unit_no_db

#: A reviewed host key entry, in known_hosts form.
HOST_KEY = "build.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterial"

#: A second key, for the changed-key case.
OTHER_HOST_KEY = "build.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKeyMaterial"


def _host(**overrides: object) -> SshHost:
    """Build one reviewed host for a test.

    Args:
        overrides: The fields to override.

    Returns:
        The host.
    """
    import uuid

    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "build",
        "hostname": "build.example.com",
        "port": 22,
        "username": "deploy",
        "known_hosts_entry": HOST_KEY,
        "key_secret_ref": None,
        "remote_workspace": "/srv/app",
        "enabled": True,
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return SshHost(**values)  # type: ignore[arg-type]


def test_a_host_files_its_key_under_its_own_name() -> None:
    assert _host().known_hosts_name == "build.example.com"
    assert _host(port=2222).known_hosts_name == "[build.example.com]:2222"


def test_the_connection_checks_the_host_key() -> None:
    argv = build_ssh_argv(
        _host(),
        command="echo hi",
        key_path=None,
        known_hosts_path=Path("/tmp/known_hosts"),
    )

    assert "StrictHostKeyChecking=yes" in argv
    assert "UserKnownHostsFile=/tmp/known_hosts" in argv
    # The connection never asks the user to confirm a key.
    assert "BatchMode=yes" in argv
    assert argv[-2:] == ["deploy@build.example.com", "echo hi"]


def test_the_connection_uses_the_named_key_only() -> None:
    argv = build_ssh_argv(
        _host(),
        command="echo hi",
        key_path=Path("/keys/deploy"),
        known_hosts_path=Path("/tmp/known_hosts"),
    )

    assert "IdentitiesOnly=yes" in argv
    assert "/keys/deploy" in argv


def test_a_command_runs_in_the_reviewed_remote_directory() -> None:
    command = build_remote_command(_host(), "ls -la")

    assert command == "cd /srv/app && ls -la"


def test_a_host_without_a_reviewed_directory_runs_the_command_as_it_is() -> None:
    assert build_remote_command(_host(remote_workspace=None), "ls -la") == "ls -la"


def test_an_empty_command_is_refused() -> None:
    with pytest.raises(SshError):
        build_remote_command(_host(), "   ")


def test_an_overlong_command_is_refused() -> None:
    with pytest.raises(SshError):
        build_remote_command(_host(), "x" * (MAX_COMMAND_CHARS + 1))


def test_a_key_that_other_users_can_read_is_refused(tmp_path: Path) -> None:
    key = tmp_path / "deploy"
    key.write_text("PRIVATE KEY", encoding="utf-8")
    key.chmod(0o644)

    with pytest.raises(SshTrustError):
        resolve_key_path("deploy", key_directory=tmp_path)


def test_a_private_key_is_accepted(tmp_path: Path) -> None:
    key = tmp_path / "deploy"
    key.write_text("PRIVATE KEY", encoding="utf-8")
    key.chmod(0o600)

    assert resolve_key_path("deploy", key_directory=tmp_path) == key


def test_a_missing_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SshTrustError):
        resolve_key_path("not-there", key_directory=tmp_path)


def test_a_host_without_a_key_uses_the_agents_own_keys(tmp_path: Path) -> None:
    assert resolve_key_path(None, key_directory=tmp_path) is None
    assert resolve_key_path("", key_directory=tmp_path) is None


def test_the_remote_environment_carries_no_deployment_secret(monkeypatch) -> None:
    monkeypatch.setenv("AKS_PROVIDER_SECRET_KEYRING_JSON", "super-secret")
    monkeypatch.setenv("AKS_JWT_SECRET", "super-secret")

    environment = remote_environment()

    assert "AKS_PROVIDER_SECRET_KEYRING_JSON" not in environment
    assert "AKS_JWT_SECRET" not in environment
    assert environment["DEEPSPACE_SANDBOX_BACKEND"] == "ssh"


def test_the_key_permissions_are_checked_by_the_operating_system(tmp_path: Path) -> None:
    key = tmp_path / "deploy"
    key.write_text("PRIVATE KEY", encoding="utf-8")
    key.chmod(0o600)

    mode = key.stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)
    assert os.path.exists(key)
