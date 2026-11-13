"""The skill loader for DeepSpace.

A skill is a Markdown file in the project workspace that teaches an agent a
procedure it does not otherwise know.  The loader reads the file and returns
it as the skill's instructions, so the agent can follow it for the rest of
the turn.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import Field

from app.core.averqel_home import is_averqel_desktop_runtime
from app.core.home_paths import display_path as format_path_for_display
from app.deepspace.services.skill_constants import SKILLS_DIRECTORY
from app.deepspace.services.skill_registry import skill_registry
from app.deepspace.services.skill_telemetry import SkillTelemetryEvent, record_skill_event
from app.deepspace.services.skill_validation import validate_skill_document
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    string_property,
)
from app.deepspace.services.workspace_root import workspace_root_for

#: The directory under the workspace root that holds the skills.
#: The largest skill document one call may return, in characters.
MAX_SKILL_CHARS = 40_000

#: The largest number of skills one listing may report.
MAX_LISTED_SKILLS = 100

logger = logging.getLogger(__name__)


def _shared_skill_display_path(shared_root: Path, relative: Path) -> str:
    """Render a shared skill path without assuming the configured root."""
    return format_path_for_display(shared_root / relative)


class SkillLoadArguments(ToolArguments):
    """Load one skill from the project workspace."""

    name: str = Field(min_length=1, max_length=120)


class SkillListArguments(ToolArguments):
    """List the skills in the project workspace."""

    query: str | None = Field(default=None, max_length=200)


class SkillResourceArguments(ToolArguments):
    """Read one skill asset relative to its declared resource directory."""

    name: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=500)


class SkillValidateArguments(ToolArguments):
    """Validate a draft skill document."""

    content: str = Field(min_length=1, max_length=200_000)
    name: str | None = Field(default=None, max_length=120)


def _skills_root(context: ToolContext) -> Path:
    """Return the directory that holds the workspace's skills."""
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    return root / SKILLS_DIRECTORY


def _is_admin(context: ToolContext) -> bool:
    """Return whether the calling principal may use admin-only skills."""
    roles = {str(role).casefold() for role in getattr(context.auth, "roles", frozenset())}
    permissions = {
        str(item).casefold() for item in getattr(context.auth, "permissions", frozenset())
    }
    return bool(
        roles & {"admin", "owner", "superuser"} or permissions & {"*", "admin", "skills:admin"}
    )


def _skill_event(
    context: ToolContext,
    *,
    action: str,
    name: str,
    provider: str = "",
    source: str = "",
    outcome: str = "ok",
    started: float | None = None,
) -> None:
    """Record one skill action without letting telemetry break the call."""
    try:
        record_skill_event(
            SkillTelemetryEvent(
                action=action,
                name=name,
                provider=provider,
                source=source,
                outcome=outcome,
                duration_ms=(time.monotonic() - started) * 1000 if started is not None else 0.0,
            ),
            conversation_id=getattr(context, "conversation_id", None),
            settings=context.settings,
        )
    except Exception:  # noqa: BLE001 - telemetry must not break a skill operation.
        logger.debug("Skill telemetry recording failed", exc_info=True)


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
    started = time.monotonic()
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    name = str(arguments["name"]).strip()
    registry = skill_registry(context.settings, root)
    skill = registry.get(
        name,
        for_admin=_is_admin(context),
        scope=getattr(context, "skill_scope", None),
    )
    if skill is None:
        _skill_event(
            context,
            action="load",
            name=name,
            outcome="not_found",
            started=started,
        )
        return {
            "status": "not_found",
            "name": name,
            "message": f"No model-invocable skill named '{name}' is available.",
        }
    text = skill.content[:MAX_SKILL_CHARS]
    display_path = str(skill.path) if skill.path else f"bundled:{skill.name}"
    if skill.path is not None:
        try:
            display_path = skill.path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            display_path = format_path_for_display(skill.path)
    _skill_event(
        context,
        action="load",
        name=skill.name,
        provider=skill.provider,
        source=skill.source,
        started=started,
    )
    return {
        "status": "ok",
        "name": skill.name,
        "description": skill.description,
        "when_to_use": skill.when_to_use,
        "source": skill.source,
        "provider": skill.provider,
        "rank": skill.rank,
        "content_sha256": skill.content_sha256,
        "invocation": {
            "model_invocable": skill.invocation.model_invocable,
            "user_invocable": skill.invocation.user_invocable,
            "admin_only": skill.invocation.admin_only,
        },
        "path": display_path,
        "resource_base": str(skill.resource_base) if skill.resource_base else None,
        "instructions": text,
        "truncated": skill.content_truncated or len(skill.content) > MAX_SKILL_CHARS,
    }


async def _skill_resource(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read a bounded resource file within the loaded skill's directory."""
    started = time.monotonic()
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    name = str(arguments["name"]).strip()
    relative_path = str(arguments["path"]).strip()
    skill = skill_registry(context.settings, root).get(
        name,
        for_admin=_is_admin(context),
        scope=getattr(context, "skill_scope", None),
    )
    if skill is None:
        _skill_event(context, action="resource", name=name, outcome="not_found", started=started)
        return {
            "status": "not_found",
            "name": name,
            "message": "No model-invocable skill was found.",
        }
    try:
        content = skill_registry(context.settings, root).read_resource(skill, relative_path)
    except (OSError, ValueError, PermissionError) as exc:
        _skill_event(
            context,
            action="resource",
            name=skill.name,
            provider=skill.provider,
            source=skill.source,
            outcome="error",
            started=started,
        )
        return {"status": "error", "name": name, "message": str(exc)}
    _skill_event(
        context,
        action="resource",
        name=skill.name,
        provider=skill.provider,
        source=skill.source,
        started=started,
    )

    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "status": "ok",
            "name": name,
            "path": relative_path,
            "size_bytes": len(content),
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }
    return {
        "status": "ok",
        "name": name,
        "path": relative_path,
        "size_bytes": len(content),
        "encoding": "utf-8",
        "content": decoded,
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
    include_shared: bool | None = None,
    for_user: bool = False,
    for_admin: bool = False,
    scope: Any = None,
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
    needle = str(query or "").strip().casefold()
    found: list[dict[str, Any]] = []
    use_shared = is_averqel_desktop_runtime() if include_shared is None else include_shared
    registry = skill_registry(settings, root, include_shared=use_shared)
    candidates = registry.list_for_scope(scope) if scope is not None else registry.list()
    for skill in candidates:
        if skill.invocation.admin_only and not for_admin:
            continue
        if for_user and not skill.invocation.user_invocable:
            continue
        if not for_user and not skill.invocation.model_invocable:
            continue
        if (
            needle
            and needle not in skill.name.casefold()
            and needle not in skill.description.casefold()
            and needle not in skill.when_to_use.casefold()
        ):
            continue
        summary: dict[str, Any] = {
            "name": skill.name,
            "description": skill.description,
            "when_to_use": skill.when_to_use,
            "source": skill.source,
            "provider": skill.provider,
            "rank": skill.rank,
            "content_sha256": skill.content_sha256,
            "invocation": {
                "model_invocable": skill.invocation.model_invocable,
                "user_invocable": skill.invocation.user_invocable,
                "admin_only": skill.invocation.admin_only,
            },
        }

        if skill.path is not None:
            try:
                summary["path"] = skill.path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                summary["path"] = format_path_for_display(skill.path)
        found.append(summary)
        if len(found) >= max(1, min(int(limit), MAX_LISTED_SKILLS)):
            break
    return found


async def _skill_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """List the skills installed in the project workspace."""
    started = time.monotonic()
    admin = _is_admin(context)
    skills = workspace_skills(
        context.settings,
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        workspace_root=context.workspace_root,
        query=arguments.get("query"),
        for_admin=admin,
        scope=getattr(context, "skill_scope", None),
    )
    _skill_event(
        context,
        action="list",
        name="",
        outcome="ok",
        started=started,
    )
    if not skills:
        return {
            "status": "ok",
            "skill_count": 0,
            "skills": [],
            "message": f"No skills are installed under {SKILLS_DIRECTORY}.",
        }
    return {"status": "ok", "skill_count": len(skills), "skills": skills}


async def _skill_validate(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Validate a draft skill document before it is installed."""
    started = time.monotonic()
    content = str(arguments.get("content") or "")
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    names = frozenset(
        skill.name.casefold() for skill in skill_registry(context.settings, root).list()
    )
    issues = validate_skill_document(
        content,
        fallback_name=str(arguments.get("name") or ""),
        strict_names=bool(getattr(context.settings, "deepspace_skill_strict_names", False)),
        existing_names=names,
    )
    _skill_event(
        context,
        action="validate",
        name=str(arguments.get("name") or ""),
        outcome="ok" if not any(issue.severity == "error" for issue in issues) else "invalid",
        started=started,
    )
    errors = [issue for issue in issues if issue.severity == "error"]
    return {
        "status": "ok" if not errors else "invalid",
        "valid": not errors,
        "issues": [issue.to_dict() for issue in issues],
    }


SKILL_DESCRIPTION = (
    "Skill tools. A skill is a procedure installed in the project workspace. "
    "Use skill_list to see what is installed and skill_load to read one before "
    "following it."
)
SKILL_RESOURCE_DESCRIPTION = (
    "Read a linked file from a skill's resource directory. The path must stay within that skill. "
    "Binary resources are returned as base64."
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
    define_native_tool(
        name="skill_resource",
        description=SKILL_RESOURCE_DESCRIPTION,
        properties={
            "name": string_property(
                "The model-invocable skill name.", min_length=1, max_length=120
            ),
            "path": string_property(
                "A path relative to the skill resource directory.", min_length=1, max_length=500
            ),
        },
        required=["name", "path"],
        arguments_model=SkillResourceArguments,
        mode="read",
        handler=_skill_resource,
    ),
    define_native_tool(
        name="skill_validate",
        description=(
            "Check a draft skill document before installing it. Reports missing "
            "frontmatter, invalid names, duplicate names, oversized content, and "
            "broken resource references."
        ),
        properties={
            "content": string_property(
                "The complete skill Markdown, including optional frontmatter.",
                min_length=1,
                max_length=200_000,
            ),
            "name": string_property(
                "The intended skill name, when the frontmatter omits it.",
                min_length=1,
                max_length=120,
            ),
        },
        required=["content"],
        arguments_model=SkillValidateArguments,
        mode="read",
        handler=_skill_validate,
    ),
]

__all__ = [
    "SKILL_TOOLS",
    "SkillListArguments",
    "SkillLoadArguments",
    "workspace_skills",
]
