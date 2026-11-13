"""Tests for the packaged Office skill asset bundles."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services import skill_registry as registry_module
from app.deepspace.services.bundled_skills import (
    BADGE_SKILLS,
    DEFAULT_OFFICE_ASSET_ROOT,
    OFFICE_SKILLS,
    BundledSkillsConfig,
    BundledSkillsManager,
    get_bundled_skills_manager,
    init_bundled_skills_manager,
)
from app.deepspace.services.skill_registry import (
    BundledSkillProvider,
    SkillRegistry,
)

pytestmark = pytest.mark.unit_no_db

CORE_OFFICE_SKILLS = (
    "office",
    "office-docx",
    "office-xlsx",
    "office-pptx",
    "office-render",
    "office-recalculate",
    "office-export-pdf",
)


def test_every_core_office_skill_ships_a_document_and_reference() -> None:
    for name in CORE_OFFICE_SKILLS:
        bundle = DEFAULT_OFFICE_ASSET_ROOT / name
        assert (bundle / "SKILL.md").is_file(), name
        references = sorted((bundle / "references").glob("*.md"))
        assert references, name
        document = (bundle / "SKILL.md").read_text(encoding="utf-8")
        assert document.startswith("---\nname: ")
        assert f"name: {name}\n" in document


def test_manager_uses_asset_documents_and_keeps_badges_inline() -> None:
    manager = BundledSkillsManager(BundledSkillsConfig(), SimpleNamespace())
    entries = manager.get_bundled_skill_entries()

    for name in CORE_OFFICE_SKILLS:
        document, resource_base = entries[name]
        assert resource_base == DEFAULT_OFFICE_ASSET_ROOT / name
        assert "name: " in document
    for name in BADGE_SKILLS:
        _document, resource_base = entries[name]
        assert resource_base is None


def test_installing_an_office_skill_copies_its_references(tmp_path: Path) -> None:
    manager = BundledSkillsManager(BundledSkillsConfig(), SimpleNamespace())
    assert manager.install_skill("office", tmp_path) is True
    assert (tmp_path / ".agents" / "skills" / "office" / "SKILL.md").is_file()
    assert (tmp_path / ".agents" / "skills" / "office" / "references" / "sidecar.md").is_file()


def test_bundled_registry_serves_office_reference_resources(monkeypatch) -> None:
    manager = BundledSkillsManager(BundledSkillsConfig(), SimpleNamespace())
    monkeypatch.setattr(registry_module, "get_bundled_skills_manager", lambda _settings: manager)
    registry = SkillRegistry([BundledSkillProvider(SimpleNamespace())])

    skill = registry.get("office")
    assert skill is not None
    assert skill.resource_base == DEFAULT_OFFICE_ASSET_ROOT / "office"
    assert "Office sidecar" in registry.read_resource(skill, "references/sidecar.md").decode(
        "utf-8"
    )

    spreadsheet = registry.get("office-xlsx")
    assert spreadsheet is not None
    assert "Formulas and recalculation" in registry.read_resource(
        spreadsheet, "references/formulas-and-recalculation.md"
    ).decode("utf-8")

    with pytest.raises((FileNotFoundError, PermissionError)):
        registry.read_resource(skill, "../../outside.md")


def test_manager_respects_individual_bundle_switches_and_unknown_names(tmp_path: Path) -> None:
    manager = BundledSkillsManager(
        BundledSkillsConfig(office_enabled=False, badge_enabled=False),
        SimpleNamespace(),
    )

    assert manager.get_bundled_skills() == {}
    assert manager.get_bundled_skill_entries() == {}
    assert manager.asset_directory("office") is None
    assert manager.get_skill_content("office") is None
    assert manager.install_skill("office", tmp_path) is False
    assert manager.install_all(tmp_path) == {}
    assert manager.list_available() == []
    assert manager.is_installed("office", tmp_path) is False


def test_manager_falls_back_to_inline_text_if_a_packaged_asset_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_root = tmp_path / "assets"
    asset = asset_root / "office"
    asset.mkdir(parents=True)
    document = asset / "SKILL.md"
    document.write_text("placeholder", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_for_asset(path: Path, *args, **kwargs):
        if path == document:
            raise OSError("simulated missing packaged asset")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_for_asset)
    manager = BundledSkillsManager(BundledSkillsConfig(asset_root=asset_root), SimpleNamespace())

    entries = manager.get_bundled_skill_entries()

    assert entries["office"] == (OFFICE_SKILLS["office"], None)


def test_inline_badge_install_and_available_skill_descriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = BundledSkillsManager(
        BundledSkillsConfig(office_enabled=False, badge_enabled=True),
        SimpleNamespace(),
    )
    badge = next(iter(BADGE_SKILLS))

    assert manager.install_skill(badge, tmp_path) is True
    assert manager.is_installed(badge, tmp_path) is True
    assert (tmp_path / ".agents" / "skills" / badge / "SKILL.md").read_text(
        encoding="utf-8"
    ) == BADGE_SKILLS[badge]
    assert manager.install_all(tmp_path) == {name: True for name in BADGE_SKILLS}

    monkeypatch.setattr(manager, "get_bundled_skills", lambda: {"short": "one line"})
    assert manager.list_available() == [{"id": "short", "name": "Short", "description": ""}]


def test_bundled_manager_factories_cache_and_replace_the_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.deepspace.services.bundled_skills as bundled_module

    settings = SimpleNamespace(
        deepspace_bundled_skills_enabled=False,
        deepspace_bundled_office_enabled=False,
        deepspace_bundled_badge_enabled=True,
        deepspace_bundled_auto_install=False,
        deepspace_bundled_skill_dir="custom/skills",
    )
    monkeypatch.setattr(bundled_module, "_BUNDLED_SKILLS", None)

    first = get_bundled_skills_manager(settings)
    assert first.config.enabled is False
    assert first.config.office_enabled is False
    assert first.config.badge_enabled is True
    assert first.config.auto_install is False
    assert first.config.skill_dir == "custom/skills"
    assert get_bundled_skills_manager(SimpleNamespace()) is first

    replacement = init_bundled_skills_manager(settings)
    assert replacement is not first
    assert get_bundled_skills_manager() is replacement
