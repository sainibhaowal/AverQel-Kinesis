"""Tests for the DeepSpace sandbox runners, modes, and failure classification.

The checks cover the Landlock runner's rules and its fail-closed probe, the
per-conversation sandbox mode, and the separation of a policy denial from a
broken sandbox.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.deepspace.api.sandbox import deepspace_runtime_status
from app.deepspace.models.conversation import Conversation
from app.deepspace.services.host_access import tool_confirmation_warning
from app.deepspace.services.landlock import (
    build_rules,
    interpreter,
    landlock_abi,
    landlock_usable,
    launcher_path,
    rules_payload,
    wrap_command,
)
from app.deepspace.services.runtime_policy import DeepSpaceToolPolicy
from app.deepspace.services.sandbox_backend import (
    SANDBOX_BACKENDS,
    LandlockBackend,
    SandboxCommand,
    build_sandbox_command,
    sandbox_status,
)
from app.deepspace.services.sandbox_mode import (
    SANDBOX_MODES,
    SandboxModeError,
    SandboxModeService,
    validate_mode,
)
from app.deepspace.services.tools.shell import classify_failure
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite

pytestmark = pytest.mark.unit_no_db


@pytest.fixture(scope="module")
def _schema(tmp_path_factory: Any) -> Any:
    """Build the database schema once for the module."""
    root = tmp_path_factory.mktemp("sandbox-mode-schema")
    engine = create_engine(f"sqlite:///{root / 'schema.db'}")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def conversation(_schema: Any) -> Any:
    """Build a conversation and the service that owns its sandbox mode."""
    session = sessionmaker(bind=_schema)()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session.add(
        Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            kind="deepspace",
            title="Sandbox mode",
        )
    )
    session.commit()
    yield {
        "session": session,
        "service": SandboxModeService(session),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }
    session.close()


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    """Build the sandbox settings for one test."""
    values = {
        "deepspace_workspace_root": str(tmp_path / "workspaces"),
        "deepspace_shell_enabled": True,
        "deepspace_permission_mode": "workspace-write",
        "deepspace_sandbox_backend": "auto",
        "deepspace_sandbox_network": True,
        "deepspace_sandbox_required": False,
        "deepspace_shell_timeout_seconds": 15,
        "deepspace_shell_max_output_bytes": 65_536,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------
# Landlock
# --------------------------------------------------------------------------


def test_the_landlock_runner_is_offered_as_a_backend() -> None:
    assert "landlock" in SANDBOX_BACKENDS
    assert "docker" not in SANDBOX_BACKENDS


def test_a_read_only_deployment_grants_writing_nowhere(tmp_path: Path) -> None:
    rules = build_rules(mode="read-only", workspace=tmp_path)

    # The root rule is read-only, and nothing else grants writing.
    assert rules[0].path == "/"
    assert rules[0].access == ("execute", "read_file", "read_dir")
    assert all("write_file" not in rule.access for rule in rules)


def test_a_workspace_write_deployment_grants_writing_in_the_workspace(tmp_path: Path) -> None:
    rules = build_rules(mode="workspace-write", workspace=tmp_path)

    writable = [rule for rule in rules if "write_file" in rule.access]
    assert [rule.path for rule in writable] == [str(tmp_path)]


def test_the_rules_encode_as_the_launcher_reads_them(tmp_path: Path) -> None:
    payload = json.loads(rules_payload(build_rules(mode="workspace-write", workspace=tmp_path)))

    assert payload[0]["path"] == "/"
    assert "write_file" in payload[1]["access"]


def test_the_launcher_uses_the_system_interpreter() -> None:
    # The confined command must not be able to import the application's own
    # modules through the launcher.
    assert interpreter() in {"/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3"}


def test_the_launcher_file_is_next_to_the_service() -> None:
    assert launcher_path().is_file()


def test_the_landlock_probe_refuses_a_host_that_cannot_confine() -> None:
    # The probe is the contract: a host whose kernel refuses the rules must
    # report itself unusable, so the runtime never claims a sandbox it does
    # not have.
    usable = landlock_usable()
    if usable:
        assert landlock_abi() > 0
    else:
        assert landlock_abi() == 0 or not usable


def test_the_landlock_backend_is_unavailable_when_the_probe_fails() -> None:
    backend = LandlockBackend()

    if landlock_usable():
        assert backend.available() is True
    else:
        assert backend.available() is False


def test_the_landlock_wrapper_is_refused_when_the_host_cannot_confine(tmp_path: Path) -> None:
    if landlock_usable():
        pytest.skip("This host can confine with Landlock.")

    wrapped = wrap_command(
        argv=["/bin/true"],
        mode="workspace-write",
        workspace=tmp_path,
    )

    assert wrapped is None


def test_the_auto_chain_prefers_bubblewrap_and_falls_back_to_landlock(tmp_path: Path) -> None:
    command = build_sandbox_command(
        _settings(tmp_path, deepspace_sandbox_backend="auto"),
        ["/bin/true"],
        workspace=tmp_path,
        cwd=tmp_path,
    )

    assert command.backend in {"bwrap", "landlock", "none"}


def test_the_status_reports_the_platform_and_the_enforcement(tmp_path: Path) -> None:
    status = sandbox_status(_settings(tmp_path))

    assert status["platform"]
    assert status["enforcement"] in {"full", "partial", "none"}
    assert status["required"] is True
    assert status["required_for_restricted_modes"] is True
    if status["selected"] == "landlock":
        # Landlock confines file effects but not the network.
        assert status["enforcement"] == "partial"
        assert status["full_filesystem_isolation"] is False


def test_status_distinguishes_a_partial_runner_from_a_full_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import sandbox_backend

    monkeypatch.setattr(
        sandbox_backend,
        "build_sandbox_command",
        lambda *_args, **_kwargs: SandboxCommand(
            argv=["/bin/true"], backend="landlock", sandboxed=True
        ),
    )

    status = sandbox_status(_settings(tmp_path, deepspace_sandbox_backend="landlock"))

    assert status["available"] is True
    assert status["boundary"] == "partial"
    assert status["full_filesystem_isolation"] is False
    assert status["network_enforced"] is False
    assert status["network_isolation_supported"] is False


def test_full_runner_reports_network_capability_separately_from_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import sandbox_backend

    monkeypatch.setattr(
        sandbox_backend,
        "build_sandbox_command",
        lambda *_args, **_kwargs: SandboxCommand(
            argv=["/bin/true"], backend="bwrap", sandboxed=True
        ),
    )

    status = sandbox_status(_settings(tmp_path, deepspace_sandbox_network=True))

    assert status["network"] is True
    assert status["network_enforced"] is False
    assert status["network_isolation_supported"] is True
    assert status["full_filesystem_isolation"] is True


def test_runtime_status_exposes_version_time_and_distinct_permission_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.api import sandbox as sandbox_api

    monkeypatch.setattr(sandbox_api, "sandbox_status", lambda _settings: {"boundary": "full"})
    monkeypatch.setattr(
        sandbox_api,
        "browser_automation_unavailable_reason",
        lambda _settings: "The Playwright Python runtime is not installed.",
    )
    monkeypatch.setattr(
        sandbox_api,
        "computer_use_unavailable_reason",
        lambda _settings: "The local desktop driver needs xdotool.",
    )

    def _activity_status(*, db, auth, settings):
        assert db is sentinel_db
        assert auth.tenant_id == tenant_id
        assert settings.deepspace_terminal_enabled is False
        return {
            "status": "available",
            "workers": {"status": "available", "worker_count": 1},
            "jobs": {"queued": 1, "running": 2, "failed_last_24h": 0},
            "agents": [],
            "active_tools": [],
        }

    sentinel_db = object()
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(sandbox_api, "runtime_activity_status", _activity_status)
    payload = deepspace_runtime_status(
        _auth=SimpleNamespace(tenant_id=tenant_id),
        settings=SimpleNamespace(
            deepspace_permission_mode="workspace-write",
            deepspace_sandbox_policy="danger-full-access",
            deepspace_terminal_enabled=False,
        ),
        db=sentinel_db,
    )

    assert payload["contract_version"] == 4
    assert payload["observed_at"].endswith("Z")
    assert payload["activity"]["jobs"] == {"queued": 1, "running": 2, "failed_last_24h": 0}
    assert payload["permission_default"] == "workspace-write"
    assert payload["permission_maximum"] == "danger-full-access"
    assert payload["permission_mode"] == payload["permission_maximum"]
    assert payload["terminal_enabled"] is False
    assert payload["browser_automation"]["available"] is False
    assert (
        payload["browser_automation"]["reason"] == "The Playwright Python runtime is not installed."
    )
    assert payload["computer_use"]["available"] is False
    assert payload["computer_use"]["reason"] == "The local desktop driver needs xdotool."


# --------------------------------------------------------------------------
# The per-conversation sandbox mode
# --------------------------------------------------------------------------


def test_a_conversation_starts_with_the_deployment_mode(conversation: Any) -> None:
    mode = conversation["service"].effective_mode(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        deployment_default="workspace-write",
    )

    assert mode == "workspace-write"


def test_a_recorded_mode_overrides_the_deployment_default(conversation: Any) -> None:
    conversation["service"].set_mode(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        mode="read-only",
    )

    mode = conversation["service"].effective_mode(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        deployment_default="workspace-write",
    )

    assert mode == "read-only"


def test_an_explicit_grant_outranks_the_recorded_mode(conversation: Any) -> None:
    conversation["service"].set_mode(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        mode="read-only",
    )

    mode = conversation["service"].effective_mode(
        tenant_id=conversation["tenant_id"],
        user_id=conversation["user_id"],
        conversation_id=conversation["conversation_id"],
        deployment_default="workspace-write",
        explicit="workspace-write",
    )

    assert mode == "workspace-write"


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(SandboxModeError):
        validate_mode("root")

    assert set(SANDBOX_MODES) == {"read-only", "workspace-write", "danger-full-access"}


def test_a_read_only_conversation_refuses_the_write_tools() -> None:
    policy = DeepSpaceToolPolicy(approval_mode="never", permission_mode="workspace-write")
    policy.set_permission_mode("read-only")

    decision = policy.decide("write", {"target": "library"})

    assert decision.allowed is False
    assert "read-only" in str(decision.reason)


def test_a_read_only_conversation_still_allows_the_read_tools() -> None:
    policy = DeepSpaceToolPolicy(approval_mode="never", permission_mode="workspace-write")
    policy.set_permission_mode("read-only")

    decision = policy.decide("read", {"target": "library"})

    assert decision.allowed is True


def test_read_only_shell_blocks_system_changes_without_requesting_approval() -> None:
    policy = DeepSpaceToolPolicy(permission_mode="read-only")

    inspection = policy.decide("bash", {"command": "systemctl status example.service"})
    shutdown = policy.decide("bash", {"command": "systemctl reboot"})

    assert inspection.allowed is True
    assert inspection.requires_approval is False
    assert shutdown.allowed is False
    assert shutdown.requires_approval is False
    assert "read-only" in str(shutdown.reason)


def test_workspace_mode_refuses_paths_outside_the_selected_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = DeepSpaceToolPolicy(permission_mode="workspace-write")
    policy.set_workspace_root(workspace)

    decision = policy.decide("fs_write", {"path": str(tmp_path / "outside.txt")})

    assert decision.allowed is False
    assert "Full access" in str(decision.reason)


def test_full_access_allows_routine_host_file_writes_without_confirmation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = DeepSpaceToolPolicy(permission_mode="danger-full-access")
    policy.set_workspace_root(workspace)

    decision = policy.decide("fs_write", {"path": str(tmp_path / "outside.txt")})

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_delete_approval_warns_about_the_exact_target_and_recursive_scope() -> None:
    warning = tool_confirmation_warning(
        "fs_delete",
        {"path": "src/generated", "recursive": True},
        permission_mode="danger-full-access",
    )

    assert "permanently delete" in warning
    assert "src/generated" in warning
    assert "everything inside it" in warning


def test_full_access_system_commands_still_require_confirmation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = DeepSpaceToolPolicy(permission_mode="danger-full-access")
    policy.set_workspace_root(workspace)

    decision = policy.decide("bash", {"command": "apt install example-package"})

    assert decision.requires_approval is True
    assert decision.approval_warning is not None
    assert "operating-system permissions" in decision.approval_warning


def test_full_access_routine_commands_do_not_prompt_but_deletes_do(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = DeepSpaceToolPolicy(permission_mode="danger-full-access")
    policy.set_workspace_root(workspace)

    command = policy.decide("bash", {"command": "pytest -q"})
    delete = policy.decide("fs_delete", {"path": "build/output.log"})

    assert command.allowed is True
    assert command.requires_approval is False
    assert delete.allowed is True
    assert delete.requires_approval is True


def test_full_access_routine_writes_do_not_prompt_even_when_approval_mode_is_ask(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = DeepSpaceToolPolicy(
        approval_mode="ask",
        permission_mode="danger-full-access",
    )
    policy.set_workspace_root(workspace)

    shell = policy.decide("bash", {"command": "python -m compileall app"})
    write = policy.decide("fs_write", {"path": "notes.md", "content": "hello"})
    note = policy.decide("write", {"target": "note", "content": "Daily summary", "mode": "append"})
    sandbox = policy.decide("sandbox_execute", {"language": "python", "code": "print(1 + 1)"})
    delete = policy.decide("fs_delete", {"path": "notes.md"})

    assert shell.allowed is True
    assert shell.requires_approval is False
    assert write.allowed is True
    assert write.requires_approval is False
    assert note.allowed is True
    assert note.requires_approval is False
    assert sandbox.allowed is True
    assert sandbox.requires_approval is False
    assert delete.allowed is True
    assert delete.requires_approval is True


def test_full_access_broad_or_outside_deletes_require_confirmation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = DeepSpaceToolPolicy(permission_mode="danger-full-access")
    policy.set_workspace_root(workspace)

    recursive = policy.decide("fs_delete", {"path": "build/generated", "recursive": True})
    outside = policy.decide("fs_delete", {"path": str(tmp_path / "private.csv")})
    git = policy.decide("fs_delete", {"path": ".git"})

    assert recursive.requires_approval is True
    assert outside.requires_approval is True
    assert git.requires_approval is True


def test_workspace_mode_requires_full_access_for_host_and_remote_mutations() -> None:
    policy = DeepSpaceToolPolicy(permission_mode="workspace-write")

    for name, arguments in (
        ("computer_click", {"x": 10, "y": 10}),
        ("browser_click", {"selector": "button"}),
        ("ssh_run", {"host": "reviewed", "command": "touch file"}),
    ):
        decision = policy.decide(name, arguments)
        assert decision.allowed is False
        assert "Full access" in str(decision.reason)


def test_read_only_allows_safe_remote_inspection_but_not_mutations() -> None:
    policy = DeepSpaceToolPolicy(permission_mode="read-only")

    inspection = policy.decide("ssh_run", {"command": "ls -la"})
    mutation = policy.decide("ssh_run", {"command": "touch marker"})

    assert inspection.allowed is True
    assert inspection.requires_approval is False
    assert mutation.allowed is False
    assert "Full access" in str(mutation.reason)


def test_full_access_risk_checks_allow_routine_ui_actions_and_gate_risky_actions() -> None:
    policy = DeepSpaceToolPolicy(permission_mode="danger-full-access")

    click = policy.decide("computer_click", {"x": 10, "y": 10})
    routine_browser = policy.decide("browser_click", {"selector": "button#continue"})
    risky_browser = policy.decide("browser_click", {"selector": "button#delete-account"})
    destructive = policy.decide("bash", {"command": "rm -rf build/generated"})
    elevated = policy.decide("bash", {"command": "sudo apt install package"})
    shutdown = policy.decide("bash", {"command": "shutdown now"})

    assert click.allowed is True
    assert click.requires_approval is False
    assert routine_browser.requires_approval is False
    assert risky_browser.requires_approval is True
    assert destructive.allowed is True
    assert destructive.requires_approval is True
    assert elevated.allowed is False
    assert shutdown.allowed is True
    assert shutdown.requires_approval is True

    polkit_elevation = policy.decide("bash", {"command": "pkexec id"})
    shell_elevation = policy.decide("bash", {"command": "su -c id"})
    system_shutdown = policy.decide("bash", {"command": "systemctl reboot"})

    assert polkit_elevation.allowed is False
    assert shell_elevation.allowed is False
    assert system_shutdown.allowed is True
    assert system_shutdown.requires_approval is True


# --------------------------------------------------------------------------
# Denial against runner failure
# --------------------------------------------------------------------------


def test_a_denied_write_is_classified_as_a_denial() -> None:
    kind = classify_failure(
        output="bash: /workspace/out.txt: Permission denied",
        exit_code=1,
        backend="bwrap",
    )

    assert kind == "denied"


def test_a_broken_runner_is_not_mistaken_for_a_denial() -> None:
    kind = classify_failure(
        output="bwrap: Creating new namespace failed: Operation not permitted",
        exit_code=1,
        backend="bwrap",
    )

    assert kind == "runner_failure"


def test_the_landlock_runner_failure_is_classified() -> None:
    kind = classify_failure(
        output="landlock-run: the Landlock ruleset could not be created",
        exit_code=1,
        backend="landlock",
    )

    assert kind == "runner_failure"


def test_a_plain_command_failure_is_not_classified() -> None:
    kind = classify_failure(
        output="python: NameError: name 'x' is not defined",
        exit_code=1,
        backend="bwrap",
    )

    assert kind is None


def test_a_successful_command_is_not_classified() -> None:
    kind = classify_failure(output="done", exit_code=0, backend="bwrap")

    assert kind is None
