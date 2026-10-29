"""The skill loader for DeepSpace.

A skill is a Markdown file in the project workspace that teaches an agent a
procedure it does not otherwise know.  The loader reads the file and returns
it as the skill's instructions, so the agent can follow it for the rest of
the turn.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from pydantic import Field

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    string_property,
)
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    resolve_within_root,
    workspace_root_for,
)

#: The directory under the workspace root that holds the skills.
SKILLS_DIRECTORY = ".agents/skills"

#: The largest skill document one call may return, in characters.
MAX_SKILL_CHARS = 40_000

#: The largest number of skills one listing may report.
MAX_LISTED_SKILLS = 100


class SkillLoadArguments(ToolArguments):
    """Load one skill from the project workspace."""

    name: str = Field(min_length=1, max_length=120)


class SkillListArguments(ToolArguments):
    """List the skills in the project workspace."""

    query: str | None = Field(default=None, max_length=200)


def _skills_root(context: ToolContext) -> Path:
    """Return the directory that holds the workspace's skills."""
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    return root / SKILLS_DIRECTORY


def _skill_document(skill_dir: Path) -> Path | None:
    """Find the document that carries a skill's instructions.

    Args:
        skill_dir: The skill's directory.

    Returns:
        The skill document, or None when the skill has none.
    """
    for candidate in ("SKILL.md", "skill.md", "README.md"):
        path = skill_dir / candidate
        if path.is_file():
            return path
    return None


def _skill_summary(skill_dir: Path) -> dict[str, Any]:
    """Summarize one skill for a listing.

    Args:
        skill_dir: The skill's directory.

    Returns:
        The skill's name and its leading description.
    """
    document = _skill_document(skill_dir)
    description = ""
    if document is not None:
        for line in document.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                description = stripped[:300]
                break
    return {"name": skill_dir.name, "description": description}


async def _skill_load(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Load one skill's instructions."""
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    name = str(arguments["name"]).strip()
    try:
        resolved = resolve_within_root(root, f"{SKILLS_DIRECTORY}/{name}")
    except WorkspacePathError as exc:
        return {"status": "error", "message": str(exc)}
    skill_dir = resolved.absolute
    if not skill_dir.is_dir():
        return {
            "status": "not_found",
            "name": name,
            "message": f"No skill named '{name}' is installed in this workspace.",
        }
    document = _skill_document(skill_dir)
    if document is None:
        return {
            "status": "not_found",
            "name": name,
            "message": f"The skill '{name}' has no SKILL.md document.",
        }
    text = document.read_text(encoding="utf-8", errors="replace")[:MAX_SKILL_CHARS]
    return {
        "status": "ok",
        "name": name,
        "path": resolved.relative,
        "instructions": text,
        "truncated": len(text) >= MAX_SKILL_CHARS,
    }


def workspace_skills(
    settings: Any,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_root: Path | None = None,
    workspace_id: str | None = None,
    query: str | None = None,
    limit: int = MAX_LISTED_SKILLS,
) -> list[dict[str, Any]]:
    """Read the skills a project workspace installs.

    The same rule the ``skill_list`` tool applies is applied here, so the
    composer's reference and the tool report the same skills.  One skill is a
    directory under ``.agents/skills`` that holds a ``SKILL.md``, a
    ``skill.md``, or a ``README.md``.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.
        workspace_root: The workspace root to read, when the caller resolved
            one already.  It wins over the configured workspace path.
        workspace_id: The named workspace to resolve, or None for the
            default.
        query: Only report skills whose name or description contains this
            text.
        limit: The largest number of skills to report.

    Returns:
        One summary per skill, ordered by name.
    """
    root = workspace_root or workspace_root_for(settings, tenant_id, user_id, workspace_id)
    skills_root = root / SKILLS_DIRECTORY
    if not skills_root.is_dir():
        return []
    needle = str(query or "").strip().casefold()
    found: list[dict[str, Any]] = []
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        summary = _skill_summary(skill_dir)
        if (
            needle
            and needle not in summary["name"].casefold()
            and needle not in summary["description"].casefold()
        ):
            continue
        found.append(summary)
        if len(found) >= max(1, min(int(limit), MAX_LISTED_SKILLS)):
            break
    return found


async def _skill_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the skills installed in the project workspace."""
    skills = workspace_skills(
        context.settings,
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        workspace_root=context.workspace_root,
        query=arguments.get("query"),
    )
    if not skills:
        return {
            "status": "ok",
            "skill_count": 0,
            "skills": [],
            "message": f"No skills are installed under {SKILLS_DIRECTORY}.",
        }
    return {"status": "ok", "skill_count": len(skills), "skills": skills}


SKILL_DESCRIPTION = (
    "Skill tools. A skill is a procedure installed in the project workspace. "
    "Use skill_list to see what is installed and skill_load to read one before "
    "following it."
)

SKILL_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="skill_load",
        description=SKILL_DESCRIPTION,
        properties={
            "name": string_property("The name of the skill to load.", min_length=1, max_length=120)
        },
        required=["name"],
        arguments_model=SkillLoadArguments,
        mode="read",
        handler=_skill_load,
    ),
    define_native_tool(
        name="skill_list",
        description="List the skills installed in the project workspace.",
        properties={
            "query": string_property(
                "Only report skills whose name or description contains this text.",
                max_length=200,
            )
        },
        arguments_model=SkillListArguments,
        mode="read",
        handler=_skill_list,
    ),
]

__all__ = [
    "SKILL_TOOLS",
    "SkillListArguments",
    "SkillLoadArguments",
    "workspace_skills",
]
