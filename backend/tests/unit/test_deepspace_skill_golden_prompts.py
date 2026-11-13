"""Golden-prompt validation tests for bundled skills.

Each bundled skill should have a stable, testable prompt that verifies
its instructions are well-formed and loadable.  This catches regressions
in the bundled skill definitions before release.
"""

from __future__ import annotations

import pytest

from app.deepspace.services.bundled_skills import (
    BADGE_SKILLS,
    DEFAULT_OFFICE_ASSET_ROOT,
    OFFICE_SKILLS,
)
from app.deepspace.services.skill_registry import BundledSkillProvider
from app.deepspace.services.skill_validation import validate_skill_document

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

BADGE_SKILL_NAMES = (
    "badge-python",
    "badge-security",
    "badge-documentation",
)

CORE_BUNDLED_SKILLS = (*CORE_OFFICE_SKILLS, *BADGE_SKILL_NAMES)


def test_every_core_bundled_skill_has_valid_document() -> None:
    """Every core bundled skill passes the authoring validator."""
    for name in CORE_BUNDLED_SKILLS:
        if name in OFFICE_SKILLS:
            document = OFFICE_SKILLS[name]
        else:
            # Map frontmatter name to dict key
            key_map = {
                "badge-python": "badge_python",
                "badge-security": "badge_security",
                "badge-documentation": "badge_documentation",
            }
            document = BADGE_SKILLS[key_map[name]]

        issues = validate_skill_document(document, fallback_name=name)
        errors = [i for i in issues if i.severity == "error"]
        assert not errors, f"{name}: {errors}"


def test_office_skill_asset_bundles_exist() -> None:
    """Every office skill has its asset directory with SKILL.md and references."""
    for name in CORE_OFFICE_SKILLS:
        bundle_dir = DEFAULT_OFFICE_ASSET_ROOT / name
        assert bundle_dir.is_dir(), f"Missing asset directory for {name}"

        skill_file = bundle_dir / "SKILL.md"
        assert skill_file.is_file(), f"Missing SKILL.md for {name}"

        refs_dir = bundle_dir / "references"
        assert refs_dir.is_dir(), f"Missing references/ for {name}"

        # At least one reference file
        refs = list(refs_dir.glob("*.md"))
        assert refs, f"No reference files for {name}"


def test_bundled_provider_serves_all_core_skills(monkeypatch) -> None:
    """BundledSkillProvider discovers all core skills with correct metadata."""

    from app.deepspace.services import skill_registry as reg_module
    from app.deepspace.services.bundled_skills import get_bundled_skills_manager

    class MockSettings:
        deepspace_bundled_skills_enabled = True
        deepspace_bundled_office_enabled = True
        deepspace_bundled_badge_enabled = True
        deepspace_skill_custom_roots = ""
        deepspace_skill_enabled_names = ""
        deepspace_skill_disabled_names = ""
        deepspace_skill_strict_names = False

    manager = get_bundled_skills_manager(MockSettings())
    monkeypatch.setattr(reg_module, "get_bundled_skills_manager", lambda _s: manager)

    provider = BundledSkillProvider(MockSettings())
    skills = {s.name: s for s in provider.discover()}

    for name in CORE_BUNDLED_SKILLS:
        assert name in skills, f"Provider missing {name}"
        skill = skills[name]
        assert skill.source == "bundled"
        assert skill.provider == "bundled"
        assert skill.content_sha256, f"{name} missing content_sha256"
        assert skill.invocation.model_invocable, f"{name} not model_invocable"


def test_office_skill_references_loadable() -> None:
    """Each office skill's reference files are readable via skill_resource logic."""
    from types import SimpleNamespace

    from app.deepspace.services.bundled_skills import get_bundled_skills_manager

    manager = get_bundled_skills_manager(SimpleNamespace())
    entries = manager.get_bundled_skill_entries()

    for name in CORE_OFFICE_SKILLS:
        document, resource_base = entries[name]
        assert resource_base is not None, f"{name} missing resource_base"
        assert resource_base.is_dir(), f"{name} resource_base not a directory"

        # The document should parse to a valid SkillDefinition
        # (We rely on the validator test above; here we just check the asset structure)
        refs = list((resource_base / "references").glob("*.md"))
        for ref in refs:
            content = ref.read_text(encoding="utf-8")
            assert content.strip(), f"{name} reference {ref.name} is empty"


# Golden-prompt style: expected key phrases each skill's instructions should contain.
# These act as smoke tests that the skill's intent hasn't drifted.
SKILL_GOLDEN_PHRASES: dict[str, list[str]] = {
    "office": ["workflow", "office-docx", "office-xlsx", "office-pptx"],
    "office-docx": ["python-docx", "paragraph", "table", "style"],
    "office-xlsx": ["openpyxl", "sheet", "formula", "workbook"],
    "office-pptx": ["python-pptx", "slide", "shape", "title"],
    "office-render": ["render_pages", "page", "image", "libreoffice"],
    "office-recalculate": ["recalculate_workbook", "formula", "recalculated"],
    "office-export-pdf": ["export_pdf", "pdf", "runtime"],
    "badge-python": ["ruff", "mypy", "pytest", "coverage"],
    "badge-security": ["bandit", "pip-audit", "truffleHog", "secret"],
    "badge-documentation": ["docstring", "readme", "api documentation"],
}


def test_bundled_skill_instructions_contain_golden_phrases() -> None:
    """Each bundled skill's instructions contain its expected key phrases."""
    key_map = {
        "badge-python": "badge_python",
        "badge-security": "badge_security",
        "badge-documentation": "badge_documentation",
    }
    for name, phrases in SKILL_GOLDEN_PHRASES.items():
        if name in OFFICE_SKILLS:
            document = OFFICE_SKILLS[name]
        else:
            document = BADGE_SKILLS[key_map[name]]

        # Extract the instruction body (after frontmatter)
        parts = document.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]
        else:
            body = document

        for phrase in phrases:
            assert (
                phrase.lower() in body.lower()
            ), f"{name} missing golden phrase '{phrase}' in instructions"


__all__ = [
    "CORE_BUNDLED_SKILLS",
    "SKILL_GOLDEN_PHRASES",
]
