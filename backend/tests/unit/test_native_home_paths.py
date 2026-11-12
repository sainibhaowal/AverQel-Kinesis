from __future__ import annotations

from pathlib import Path

import pytest

from app.core.home_paths import (
    canonicalize_watch_path,
    display_path,
    expand_user_path,
    join_home_path,
    resolve_agents_home,
    resolve_averqel_home,
)

pytestmark = pytest.mark.unit_no_db


def test_configured_roots_are_separate_expandable_and_absolute(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    app_home = resolve_averqel_home(environ={}, home=user_home)
    agents_home = resolve_agents_home(environ={}, home=user_home)

    assert app_home == user_home / ".averqel-harness"
    assert agents_home == user_home / ".agents"
    assert app_home != agents_home
    assert expand_user_path("~", home=user_home) == user_home
    assert (
        resolve_averqel_home(environ={"AVERQEL_HOME": "~/custom-app"}, home=user_home)
        == user_home / "custom-app"
    )
    assert (
        resolve_agents_home(
            environ={"AVERQEL_AGENTS_HOME": str(tmp_path / "shared")}, home=user_home
        )
        == tmp_path / "shared"
    )


def test_path_helpers_label_home_and_reject_escape(tmp_path: Path) -> None:
    home = tmp_path / "user"
    assert expand_user_path("~/Kinesis", home=home) == home / "Kinesis"
    assert expand_user_path("~\\Kinesis", home=home) == home / "Kinesis"
    assert join_home_path(home, "projects", "alpha") == home / "projects" / "alpha"
    with pytest.raises(ValueError, match="stay relative"):
        join_home_path(home, "projects", "../outside")
    assert display_path(home / "Kinesis" / "src", home=home) == "~/Kinesis/src"
    assert display_path("~/Kinesis/src", home=home) == "~/Kinesis/src"
    assert display_path("~", home=home) == "~"
    assert display_path(tmp_path / "elsewhere", home=home) == str(tmp_path / "elsewhere")


def test_watch_path_resolves_symlink_alias_and_missing_suffix(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "plugin.py").write_text("value = 1\n", encoding="utf-8")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("The current host does not permit symlink creation.")

    assert canonicalize_watch_path(alias / "plugin.py") == canonicalize_watch_path(
        real / "plugin.py"
    )
    assert canonicalize_watch_path(alias / "new.py") == canonicalize_watch_path(real / "new.py")


def test_watch_path_handles_missing_ancestor_chain_to_filesystem_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)

    assert canonicalize_watch_path(tmp_path / "missing") == (tmp_path / "missing").resolve(
        strict=False
    )


def test_empty_home_paths_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        expand_user_path("  ")
