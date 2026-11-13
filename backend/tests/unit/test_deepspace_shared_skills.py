from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services.tools.skill import _skill_load, workspace_skills

pytestmark = pytest.mark.unit_no_db


def _skill(root: Path, folder: str, text: str) -> None:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")


def _settings() -> SimpleNamespace:
    """Disable built-ins so these tests isolate shared-root behavior."""
    return SimpleNamespace(
        deepspace_bundled_skills_enabled=False,
        deepspace_skill_custom_roots="",
    )


def test_desktop_skill_listing_includes_shared_and_workspace_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    shared = tmp_path / "agents"
    _skill(workspace / ".agents" / "skills", "shared-name", "# Local\nlocal guidance\n")
    _skill(shared / "skills", "shared-name", "# Shared\nshared guidance\n")
    _skill(shared / "skills", "global-only", "# Global\nshared-only guidance\n")
    monkeypatch.setenv("AVERQEL_AGENTS_HOME", str(shared))

    result = workspace_skills(
        _settings(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        workspace_root=workspace,
        include_shared=True,
    )

    skills = {item["name"]: item for item in result}
    assert set(skills) == {"shared-name", "global-only"}
    assert skills["shared-name"]["description"] == "Local"
    assert skills["shared-name"]["path"] == ".agents/skills/shared-name/SKILL.md"
    assert skills["global-only"]["path"] == str(shared / "skills" / "global-only" / "SKILL.md")


def test_shared_skills_are_not_visible_unless_explicitly_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "agents"
    _skill(shared / "skills", "only-global", "global instructions\n")
    monkeypatch.setenv("AVERQEL_AGENTS_HOME", str(shared))
    result = workspace_skills(
        _settings(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        workspace_root=tmp_path / "empty-workspace",
        include_shared=False,
    )
    assert result == []


def test_shared_skills_are_listed_when_workspace_has_no_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "agents"
    _skill(shared / "skills", "only-global", "global instructions\n")
    monkeypatch.setenv("AVERQEL_AGENTS_HOME", str(shared))
    result = workspace_skills(
        _settings(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        workspace_root=tmp_path / "empty-workspace",
        include_shared=True,
    )
    assert [item["name"] for item in result] == ["only-global"]


@pytest.mark.asyncio
async def test_skill_load_falls_back_to_shared_root_only_in_desktop_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "agents"
    _skill(shared / "skills", "global-only", "Use the shared procedure.\n")
    monkeypatch.setenv("AVERQEL_DESKTOP_LOCAL_RUNTIME", "1")
    monkeypatch.setenv("AVERQEL_AGENTS_HOME", str(shared))
    context = SimpleNamespace(
        workspace_root=workspace, settings=_settings(), auth=SimpleNamespace()
    )

    result = await _skill_load({"name": "global-only"}, context)

    assert result["status"] == "ok"
    assert result["instructions"] == "Use the shared procedure.\n"
    assert result["path"] == str(shared / "skills" / "global-only" / "SKILL.md")
