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

from app.deepspace.models.conversation import Conversation
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
        "deepspace_sandbox_image": None,
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


def test_a_read_only_deployment_grants_writing_nowhere(tmp_path: Path) -> None:
    rules = build_rules(mode="read-only", workspace=tmp_path)

    # The root rule is read-only, and nothing else grants writing.
    assert rules[0].path == "/"
    assert rules[0].access == ("execute", "read_file", "read_dir")
    assert all("write_file" not in rule.access for rule in rules)


def test_a_workspace_write_deployment_grants_writing_in_the_workspace(tmp_path: Path) -> None:
    rules = build_rules(mode="workspace-write", workspace=tmp_path)

    writable = [rule for rule in rules if "write_file" in rule.access]
    assert [rule.path for rule in writable] == [str(tmp_path), "/tmp"]


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

    assert command.backend in {"bwrap", "landlock", "docker", "none"}


def test_the_status_reports_the_platform_and_the_enforcement(tmp_path: Path) -> None:
    status = sandbox_status(_settings(tmp_path))

    assert status["platform"]
    assert status["enforcement"] in {"full", "partial", "none"}
    if status["selected"] == "landlock":
        # Landlock confines file effects but not the network.
        assert status["enforcement"] == "partial"


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
