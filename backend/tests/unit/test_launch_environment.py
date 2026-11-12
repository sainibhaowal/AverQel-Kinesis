from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core import launch_environment
from app.core.launch_environment import EnvironmentSource, LaunchEnvironment

pytestmark = pytest.mark.unit_no_db


def test_environment_uses_process_project_user_precedence_and_is_immutable(tmp_path: Path) -> None:
    user_file = tmp_path / "user.env"
    project_file = tmp_path / "project.env"
    user_file.write_text(
        "# ignored comment\n\nexport VALUE=user\nUSER_ONLY='user value'\n"
        "INLINE=value # trailing comment\nBAD-KEY=ignored\nMISSING_SEPARATOR\n",
        encoding="utf-8",
    )
    project_file.write_text('VALUE=project\nPROJECT_ONLY="project value"\n', encoding="utf-8")
    snapshot = LaunchEnvironment(
        process={"value": "process", "PROCESS_ONLY": "yes"},
        project_env=project_file,
        user_env=user_file,
    )

    assert snapshot.get("VALUE") == "process"
    assert snapshot.get("user_only") == "user value"
    assert snapshot.get("PROJECT_ONLY") == "project value"
    assert snapshot.get("INLINE") == "value"
    assert snapshot.get("BAD-KEY") is None
    assert snapshot.get("MISSING_SEPARATOR") is None
    assert snapshot.source_for("value") is EnvironmentSource.PROCESS
    assert snapshot.source_for("PROJECT_ONLY") is EnvironmentSource.PROJECT_ENV
    assert snapshot.get("USER_ONLY", allowed_sources={EnvironmentSource.PROCESS}) is None
    copied = snapshot.as_dict()
    copied["VALUE"] = "changed"
    assert snapshot.get("value") == "process"
    with pytest.raises(TypeError):
        snapshot.sources["value"] = EnvironmentSource.USER_ENV  # type: ignore[index]
    assert snapshot.as_dict(allowed_sources={EnvironmentSource.PROJECT_ENV}) == {
        "PROJECT_ONLY": "project value"
    }
    assert snapshot.source_for("missing") is None


def test_dotenv_is_data_not_shell_and_ssh_uses_process_only(tmp_path: Path) -> None:
    user_file = tmp_path / "user.env"
    user_file.write_text(
        "SSH_CONNECTION=dotenv-value\nINJECT=$(touch /tmp/unsafe)\n", encoding="utf-8"
    )
    snapshot = LaunchEnvironment(process={}, project_env=tmp_path / "missing", user_env=user_file)

    assert snapshot.get("INJECT") == "$(touch /tmp/unsafe)"
    assert snapshot.launched_through_ssh is False
    assert not (tmp_path / "unsafe").exists()
    ssh_snapshot = LaunchEnvironment(
        process={"ssh_tty": "/dev/pts/4"},
        project_env=tmp_path / "missing",
        user_env=tmp_path / "missing-user",
    )
    assert ssh_snapshot.launched_through_ssh is True


def test_process_snapshot_is_created_once_and_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch_environment, "_launch_snapshot", None)
    monkeypatch.setenv("SNAPSHOT_VALUE", "first")
    first = launch_environment.get_launch_environment()
    monkeypatch.setenv("SNAPSHOT_VALUE", "second")

    assert launch_environment.get_launch_environment() is first
    assert first.get("SNAPSHOT_VALUE") == "first"


def test_launch_snapshot_double_checks_under_concurrent_first_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    class RendezvousLock:
        def __enter__(self) -> None:
            barrier.wait(timeout=2)
            lock.acquire()

        def __exit__(self, *_args: object) -> None:
            lock.release()

    monkeypatch.setattr(launch_environment, "_launch_snapshot", None)
    monkeypatch.setattr(launch_environment, "_snapshot_lock", RendezvousLock())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _index: launch_environment.get_launch_environment(), range(2))
        )

    assert results[0] is results[1]


def test_invalid_utf8_dotenv_is_ignored(tmp_path: Path) -> None:
    user_file = tmp_path / "invalid.env"
    user_file.write_bytes(b"INVALID=\xff")
    snapshot = LaunchEnvironment(
        process={},
        project_env=tmp_path / "missing-project",
        user_env=user_file,
    )

    assert snapshot.get("INVALID") is None
