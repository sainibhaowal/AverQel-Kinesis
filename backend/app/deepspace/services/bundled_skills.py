"""Bundled skills for DeepSpace.

Provides built-in skill providers for Office (docx/xlsx/pptx) workflows and badges.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings
from app.deepspace.services.skill_constants import SKILLS_DIRECTORY

logger = logging.getLogger(__name__)

#: The packaged asset root for the Office bundles.  Each child directory is a
#: complete skill bundle: ``SKILL.md`` plus any ``references/`` files.
DEFAULT_OFFICE_ASSET_ROOT = Path(__file__).resolve().parents[1] / "skill_assets" / "office"

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
    asset_root: Path | None = None


# ──────────────────────────────────────────────────────────────────────
# Built-in Skill Definitions
# ──────────────────────────────────────────────────────────────────────

OFFICE_SKILLS: dict[str, str] = {
    "office": """---
name: office
description: Choose and safely complete a Word, spreadsheet, or presentation task.
when-to-use: Office documents, workbooks, presentations, or PDF export.
model-invocable: true
user-invocable: true
---
# Office workflow

Inspect the requested file and task first. Load `office-docx`, `office-xlsx`, or
`office-pptx` for the document type. Keep the source file, validate the saved
structure with the matching Python library. Use `office_process` for PDF export,
workbook recalculation, and page rendering; it operates on a private copy and
keeps outputs in the active workspace.
Never report a conversion or recalculation as successful until the output exists
and has been checked.
""",
    "office-docx": """---
name: office-docx
description: Create, inspect, and edit DOCX files with Python.
when-to-use: Word documents, DOCX templates, or document structure edits.
model-invocable: true
user-invocable: true
---
# Word documents

Use `python-docx` to inspect paragraphs, tables, sections, and styles before
editing. Preserve existing content and formatting where possible. Save to a new
file when replacing the source would risk data loss. Reopen the result and
validate paragraph/table counts and required text. For visual review, use
`office_process` with `render_pages` and inspect its output images.
""",
    "office-pptx": """---
name: office-pptx
description: Create, inspect, and edit PowerPoint presentations with Python.
when-to-use: Slide decks, PPTX templates, or presentation structure edits.
model-invocable: true
user-invocable: true
---
# PowerPoint presentations

Use `python-pptx` to inspect slide order, titles, shapes, and notes before
editing. Preserve the template's dimensions and theme. Reopen the saved deck and
verify slide count, text, and image presence. For visual review, use
`office_process` with `render_pages` and inspect its output images.
""",
    "office-xlsx": """---
name: office-xlsx
description: Create, inspect, and edit Excel workbooks with Python.
when-to-use: Spreadsheets, workbook templates, formulas, or tabular analysis.
model-invocable: true
user-invocable: true
---
# Excel workbooks

Use `openpyxl` to inspect sheets, dimensions, formulas, merged cells, and styles
before editing. Preserve formulas and workbook structure. Reopen the saved file
with formulas visible and verify the changed cells. `openpyxl` does not calculate
formulas; use `office_process` with `recalculate_workbook` and verify the
recalculated workbook separately.
""",
    "office-render": """---
name: office-render
description: Render Office files to inspect their visual layout.
when-to-use: The user asks for visual review or a layout check.
model-invocable: true
user-invocable: true
---
# Render Office files

Use `office_process` with `render_pages` and a page limit no higher than needed.
If the packaged LibreOffice or `pdftoppm` runtime is unavailable, report that
visual rendering cannot run in this environment. Inspect the returned page
images and keep the original Office file unchanged.
""",
    "office-recalculate": """---
name: office-recalculate
description: Recalculate workbook formulas with LibreOffice when available.
when-to-use: Formula results are stale and recalculation is requested.
model-invocable: true
user-invocable: true
---
# Recalculate workbook formulas

Use `office_process` with `recalculate_workbook`. It works on a private copy;
open the returned workbook and confirm the saved values. Never claim formulas
were recalculated when the runtime is unavailable or the output was not
validated.
""",
    "office-export-pdf": """---
name: office-export-pdf
description: Export a DOCX, XLSX, or PPTX file to PDF with LibreOffice.
when-to-use: The user requests a PDF copy of an Office document.
model-invocable: true
user-invocable: true
---
# Export Office files to PDF

Use `office_process` with `export_pdf`, then verify the PDF exists and is
nonempty. Preserve the source and report a clear limitation if the packaged
runtime is unavailable or conversion fails. Use `office-render` only if the
user also asks to inspect page images.
""",
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
    "badge_python": """---
name: badge-python
description: Verify Python code quality and best practices.
when-to-use: A task changes Python source and needs a quality pass.
model-invocable: true
user-invocable: true
---
# Python Badge Skill

## Purpose
Verify Python code quality and best practices.

## Checks
- Run ruff for linting
- Run mypy for type checking
- Run pytest for tests
- Check code coverage
""",
    "badge_security": """---
name: badge-security
description: Scan code for security vulnerabilities before delivery.
when-to-use: A task touches authentication, input handling, dependencies, or secrets.
model-invocable: true
user-invocable: true
---
# Security Badge Skill

## Purpose
Scan code for security vulnerabilities.

## Checks
- Run bandit for Python security issues
- Check dependencies for vulnerabilities (pip-audit)
- Scan for secrets (truffleHog)
""",
    "badge_documentation": """---
name: badge-documentation
description: Verify documentation completeness for a changed area.
when-to-use: A behavior or configuration change needs its docs reviewed.
model-invocable: true
user-invocable: true
---
# Documentation Badge Skill

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
        self.asset_root = config.asset_root or DEFAULT_OFFICE_ASSET_ROOT
        self._installed: set[str] = set()

    def get_bundled_skills(self) -> dict[str, str]:
        """Get all bundled skill definitions."""
        skills = {}
        if self.config.office_enabled:
            skills.update(OFFICE_SKILLS)
        if self.config.badge_enabled:
            skills.update(BADGE_SKILLS)
        return skills

    def asset_directory(self, skill_id: str) -> Path | None:
        """Return the packaged asset bundle for one skill, when it has one."""
        if not self.config.office_enabled:
            return None
        candidate = self.asset_root / skill_id
        if (candidate / "SKILL.md").is_file():
            return candidate
        return None

    def get_bundled_skill_entries(self) -> dict[str, tuple[str, Path | None]]:
        """Return each bundled skill's document and optional resource directory.

        A packaged asset bundle supplies both the document and its
        ``references/`` files, so ``skill_resource`` can read them.  A skill
        without an asset bundle keeps its inline document and no resource
        directory.
        """
        entries: dict[str, tuple[str, Path | None]] = {}
        for skill_id, content in self.get_bundled_skills().items():
            asset = self.asset_directory(skill_id)
            if asset is None:
                entries[skill_id] = (content, None)
                continue
            try:
                document = (asset / "SKILL.md").read_text(encoding="utf-8")
            except OSError:
                logger.warning("Could not read bundled asset for %s", skill_id, exc_info=True)
                entries[skill_id] = (content, None)
                continue
            entries[skill_id] = (document, asset)
        return entries

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
        asset = self.asset_directory(skill_id)
        if asset is not None:
            skill_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(asset, skill_dir, dirs_exist_ok=True)
        else:
            skill_dir.mkdir(parents=True, exist_ok=True)
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
