"""Bundled skills for DeepSpace.

Provides built-in skill providers for Office (docx/xlsx/pptx) workflows and badges.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings
from app.deepspace.services.tools.skill import (
    SKILLS_DIRECTORY,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BundledSkillsConfig:
    """Bundled skills configuration."""

    enabled: bool = True
    office_enabled: bool = True
    badge_enabled: bool = True
    auto_install: bool = True
    skill_dir: str = ".agents/skills"


# ──────────────────────────────────────────────────────────────────────
# Built-in Skill Definitions
# ──────────────────────────────────────────────────────────────────────

OFFICE_SKILLS: dict[str, str] = {
    "office_word": """# Office Word Skill

## Purpose
Create, edit, and format Microsoft Word documents (.docx).

## Procedures
### Create Document
1. Use `fs_write` to create a new .docx file with python-docx
2. Add headings, paragraphs, tables as needed
3. Save to workspace

### Edit Document
1. Read existing .docx with python-docx
2. Modify content, styles, tables
3. Save changes

### Convert to PDF
1. Use LibreOffice headless or python-docx2pdf
2. Save PDF alongside .docx

## Tools Available
- python-docx for .docx manipulation
- LibreOffice headless for conversion
- python-docx2pdf for PDF export
""",
    "office_excel": """# Office Excel Skill

## Purpose
Create, edit, and analyze Microsoft Excel spreadsheets (.xlsx).

## Procedures
### Create Spreadsheet
1. Use openpyxl to create workbook
2. Add sheets, data, formulas, charts
3. Save to workspace

### Analyze Data
1. Read existing .xlsx with openpyxl
2. Extract data, compute statistics
3. Generate summary

### Export to CSV
1. Read workbook
2. Write sheets as CSV files
""",
    "office_powerpoint": """# Office PowerPoint Skill

## Purpose
Create and edit Microsoft PowerPoint presentations (.pptx).

## Procedures
### Create Presentation
1. Use python-pptx to create presentation
2. Add slides, shapes, text, images
3. Save to workspace

### Edit Presentation
1. Read existing .pptx
2. Modify slides, add content
3. Save changes
""",
}

BADGE_SKILLS: dict[str, str] = {
    "badge_python": """# Python Badge Skill

## Purpose
Verify Python code quality and best practices.

## Checks
- Run ruff for linting
- Run mypy for type checking
- Run pytest for tests
- Check code coverage
""",
    "badge_security": """# Security Badge Skill

## Purpose
Scan code for security vulnerabilities.

## Checks
- Run bandit for Python security issues
- Check dependencies for vulnerabilities (pip-audit)
- Scan for secrets (truffleHog)
""",
    "badge_documentation": """# Documentation Badge Skill

## Purpose
Verify documentation completeness.

## Checks
- Check docstring coverage
- Verify README exists
- Check API documentation
""",
}

# ──────────────────────────────────────────────────────────────────────
# Bundled Skills Manager
# ──────────────────────────────────────────────────────────────────────


class BundledSkillsManager:
    """Manages built-in skill bundles."""

    def __init__(self, config: BundledSkillsConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self._installed: set[str] = set()

    def get_bundled_skills(self) -> dict[str, str]:
        """Get all bundled skill definitions."""
        skills = {}
        if self.config.office_enabled:
            skills.update(OFFICE_SKILLS)
        if self.config.badge_enabled:
            skills.update(BADGE_SKILLS)
        return skills

    def list_available(self) -> list[dict[str, str]]:
        """List available bundled skills."""
        return [
            {
                "id": sid,
                "name": sid.replace("_", " ").title(),
                "description": content.split("\n")[2] if len(content.split("\n")) > 2 else "",
            }
            for sid, content in self.get_bundled_skills().items()
        ]

    def install_skill(self, skill_id: str, workspace_root: Path) -> bool:
        """Install a bundled skill into a workspace."""
        skills = self.get_bundled_skills()
        if skill_id not in skills:
            return False

        skill_dir = workspace_root / SKILLS_DIRECTORY / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Write SKILL.md
        (skill_dir / "SKILL.md").write_text(skills[skill_id], encoding="utf-8")

        self._installed.add(skill_id)
        logger.info("Installed bundled skill: %s", skill_id)
        return True

    def install_all(self, workspace_root: Path) -> dict[str, bool]:
        """Install all bundled skills into a workspace."""
        results = {}
        for skill_id in self.get_bundled_skills():
            results[skill_id] = self.install_skill(skill_id, workspace_root)
        return results

    def is_installed(self, skill_id: str, workspace_root: Path) -> bool:
        """Check if a skill is installed in a workspace."""
        skill_dir = workspace_root / SKILLS_DIRECTORY / skill_id
        return skill_dir.exists() and (skill_dir / "SKILL.md").exists()

    def get_skill_content(self, skill_id: str) -> str | None:
        """Get the content of a bundled skill."""
        return self.get_bundled_skills().get(skill_id)


# ──────────────────────────────────────────────────────────────────────
# Service Factory
# ──────────────────────────────────────────────────────────────────────

_BUNDLED_SKILLS: BundledSkillsManager | None = None


def get_bundled_skills_manager(settings: Settings | None = None) -> BundledSkillsManager:
    """Get or create the global bundled skills manager."""
    global _BUNDLED_SKILLS
    if _BUNDLED_SKILLS is None:
        cfg = BundledSkillsConfig(
            enabled=getattr(settings or get_settings(), "deepspace_bundled_skills_enabled", True),
            office_enabled=getattr(
                settings or get_settings(), "deepspace_bundled_office_enabled", True
            ),
            badge_enabled=getattr(
                settings or get_settings(), "deepspace_bundled_badge_enabled", True
            ),
            auto_install=getattr(
                settings or get_settings(), "deepspace_bundled_auto_install", True
            ),
            skill_dir=getattr(
                settings or get_settings(), "deepspace_bundled_skill_dir", ".agents/skills"
            ),
        )
        _BUNDLED_SKILLS = BundledSkillsManager(cfg, settings or get_settings())
    return _BUNDLED_SKILLS


def init_bundled_skills_manager(settings: Settings) -> BundledSkillsManager:
    """Initialize the bundled skills manager."""
    global _BUNDLED_SKILLS
    cfg = BundledSkillsConfig(
        enabled=getattr(settings, "deepspace_bundled_skills_enabled", True),
        office_enabled=getattr(settings, "deepspace_bundled_office_enabled", True),
        badge_enabled=getattr(settings, "deepspace_bundled_badge_enabled", True),
        auto_install=getattr(settings, "deepspace_bundled_auto_install", True),
        skill_dir=getattr(settings, "deepspace_bundled_skill_dir", ".agents/skills"),
    )
    _BUNDLED_SKILLS = BundledSkillsManager(cfg, settings)
    return _BUNDLED_SKILLS


__all__ = [
    "BundledSkillsConfig",
    "BundledSkillsManager",
    "get_bundled_skills_manager",
    "init_bundled_skills_manager",
    "OFFICE_SKILLS",
    "BADGE_SKILLS",
]
