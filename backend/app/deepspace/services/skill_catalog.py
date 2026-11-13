"""The model-facing skill catalog.

A skill is only useful when the agent knows it exists.  This module turns the
registry's resolved definitions into one bounded, escaped system instruction:
the names, the one-line descriptions, and where each skill came from.  The
catalog is rendered every turn from the live registry, so a newly created
skill appears on the next turn without any stored state to invalidate.

The rendering rules protect two boundaries:

* **Prompt framing.**  A skill name, description, or source is workspace
  content, not markup.  Everything except the framing tags is escaped, so a
  description containing angle brackets cannot close ``<available_skills>``
  early or inject a new section.
* **Budget.**  A large catalog is a per-turn prompt cost.  Entries are added
  in name order until the character budget is reached, and the snapshot
  records that it was truncated so the model is told the list is partial.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.deepspace.services.skill_registry import SkillDefinition, SkillRegistry

#: The opening tag of the model-visible catalog block.
CATALOG_OPEN = "<available_skills>"

#: The closing tag of the model-visible catalog block.
CATALOG_CLOSE = "</available_skills>"

#: The largest description one catalog entry carries, in characters.
MAX_CATALOG_DESCRIPTION_CHARS = 500

#: The default largest size of the rendered catalog, in characters.
DEFAULT_CATALOG_MAX_CHARS = 12_000

_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def escape_skill_text(value: str) -> str:
    """Escape workspace text for inclusion in the catalog block.

    Args:
        value: The skill-provided text.

    Returns:
        The text with control characters removed and XML-significant
        characters escaped.
    """
    text = _CONTROL.sub("", str(value or ""))
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def normalize_skill_line(value: str, limit: int = MAX_CATALOG_DESCRIPTION_CHARS) -> str:
    """Collapse, escape, and bound one skill-provided line.

    Args:
        value: The skill-provided text.
        limit: The largest number of characters to keep.  Zero keeps all.

    Returns:
        One escaped line, suffixed with an ellipsis when it was shortened.
    """
    text = escape_skill_text(_WHITESPACE.sub(" ", str(value or "")).strip())
    if limit > 0 and len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "\u2026"
    return text


@dataclass(frozen=True, slots=True)
class SkillCatalogEntry:
    """One skill as the catalog presents it."""

    name: str
    description: str
    provider: str = ""
    source: str = ""
    content_sha256: str = ""
    model_invocable: bool = True
    user_invocable: bool = True
    admin_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the entry as the API and UI consume it."""
        return {
            "name": self.name,
            "description": self.description,
            "provider": self.provider,
            "source": self.source,
            "content_sha256": self.content_sha256,
            "model_invocable": self.model_invocable,
            "user_invocable": self.user_invocable,
            "admin_only": self.admin_only,
        }


@dataclass(frozen=True, slots=True)
class SkillCatalogSnapshot:
    """One bounded catalog, its provenance, and whether it is complete."""

    entries: tuple[SkillCatalogEntry, ...]
    incomplete: bool = False
    truncated: bool = False
    digest: str = ""
    char_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return the snapshot as the API and UI consume it."""
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "incomplete": self.incomplete,
            "truncated": self.truncated,
            "digest": self.digest,
            "char_count": self.char_count,
        }


def _entry_for(skill: SkillDefinition) -> SkillCatalogEntry:
    """Project one resolved definition onto its catalog entry."""
    return SkillCatalogEntry(
        name=skill.name,
        description=skill.description[:MAX_CATALOG_DESCRIPTION_CHARS],
        provider=skill.provider,
        source=skill.source,
        content_sha256=skill.content_sha256,
        model_invocable=skill.invocation.model_invocable,
        user_invocable=skill.invocation.user_invocable,
        admin_only=skill.invocation.admin_only,
    )


def _digest_entries(entries: Iterable[SkillCatalogEntry]) -> str:
    """Return a stable digest of the entries, independent of ordering."""
    payload = [
        {
            "name": entry.name,
            "provider": entry.provider,
            "source": entry.source,
            "content_sha256": entry.content_sha256,
        }
        for entry in sorted(entries, key=lambda item: item.name.casefold())
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_skill_catalog(
    registry: SkillRegistry,
    *,
    for_model: bool = True,
    for_admin: bool = False,
    max_chars: int = DEFAULT_CATALOG_MAX_CHARS,
    description_chars: int = MAX_CATALOG_DESCRIPTION_CHARS,
    scope: Any = None,
) -> SkillCatalogSnapshot:
    """Build one bounded catalog from the registry.

    Args:
        registry: The resolved registry for the active workspace.
        for_model: Keep only skills the model may invoke.
        for_admin: Keep admin-only skills as well.  Without it those entries
            are omitted rather than advertised and then refused.
        max_chars: The largest rendered catalog size to target.
        description_chars: The largest description one entry carries.
        scope: The visibility scope of the agent this catalog is for, or None
            for the unrestricted main agent.

    Returns:
        The snapshot, including the digest of every entry that was eligible
        before the budget shortened the rendered list.
    """
    skills, observations = registry.snapshot()
    if scope is not None:
        from app.deepspace.services.skill_scope import filter_skills_for_scope

        skills = filter_skills_for_scope(skills, scope)
    incomplete = any(not observation.ok for observation in observations)
    eligible: list[SkillCatalogEntry] = []
    for skill in skills:
        if for_model and not skill.invocation.model_invocable:
            continue
        if skill.invocation.admin_only and not for_admin:
            continue
        entry = _entry_for(skill)
        if description_chars and len(entry.description) > description_chars:
            entry = SkillCatalogEntry(
                name=entry.name,
                description=entry.description[:description_chars],
                provider=entry.provider,
                source=entry.source,
                content_sha256=entry.content_sha256,
                model_invocable=entry.model_invocable,
                user_invocable=entry.user_invocable,
                admin_only=entry.admin_only,
            )
        eligible.append(entry)
    eligible.sort(key=lambda item: (item.name.casefold(), item.name))
    digest = _digest_entries(eligible)

    budget = max(0, int(max_chars))
    selected: list[SkillCatalogEntry] = []
    used = 0
    for entry in eligible:
        cost = len(entry.name) + len(entry.description) + len(entry.provider) + 8
        if budget and used + cost > budget:
            break
        selected.append(entry)
        used += cost
    truncated = len(selected) < len(eligible)
    snapshot = SkillCatalogSnapshot(
        entries=tuple(selected),
        incomplete=incomplete,
        truncated=truncated,
        digest=digest,
        char_count=used,
    )
    return snapshot


def render_skill_catalog(snapshot: SkillCatalogSnapshot) -> str:
    """Render the catalog as one model-facing system instruction.

    Args:
        snapshot: The bounded catalog to render.

    Returns:
        The framed catalog text.
    """
    lines: list[str] = [
        "<system-reminder>",
        "A skill is a reusable set of task-specific instructions.",
    ]
    if snapshot.entries:
        lines.append("The following skills are available in this session:")
        lines.append("")
        lines.append(CATALOG_OPEN)
        for entry in snapshot.entries:
            provenance = entry.provider or entry.source
            suffix = f" ({escape_skill_text(provenance)})" if provenance else ""
            lines.append(f"- {entry.name}: {normalize_skill_line(entry.description)}{suffix}")
        lines.append(CATALOG_CLOSE)
        lines.append("")
        lines.append(
            "If the user names a skill, or the task clearly matches a skill's "
            "description, call `skill_load` with the exact skill name before "
            "taking task actions. This catalog contains summaries only; load a "
            "skill before following its instructions."
        )
        if snapshot.truncated:
            lines.append(
                "This catalog was shortened to fit the request budget. Call "
                "`skill_list` to see the remaining names."
            )
    elif snapshot.incomplete:
        lines.append(
            "No skills could be confirmed in this discovery attempt. Do not "
            "infer skill instructions from an empty catalog; call `skill_list` "
            "after the provider recovers."
        )
    else:
        lines.append(
            "No skills are installed for this workspace. Do not invent a skill "
            "name or its instructions."
        )
    if snapshot.incomplete:
        lines.append(
            "Skill discovery was incomplete: one or more providers failed. "
            "Treat this catalog as partial."
        )
    lines.append(
        "A user may also invoke a skill directly with `/name` or `@skill:name`; "
        "its instructions then appear in the conversation. Follow them without "
        "loading the same skill again."
    )
    lines.append("</system-reminder>")
    return "\n".join(lines)


def skill_catalog_message(snapshot: SkillCatalogSnapshot) -> dict[str, Any]:
    """Build the durable system message carrying one catalog.

    Args:
        snapshot: The catalog to publish.

    Returns:
        A system message in the shape the turn already uses for dynamic
        instructions, with a source record the API can expose.
    """
    return {
        "role": "system",
        "content": render_skill_catalog(snapshot),
        "source": {
            "kind": "skill-catalog",
            "form": "catalog",
            "digest": snapshot.digest,
            "incomplete": snapshot.incomplete,
            "truncated": snapshot.truncated,
            "entries": [entry.to_dict() for entry in snapshot.entries],
        },
    }


__all__ = [
    "CATALOG_CLOSE",
    "CATALOG_OPEN",
    "DEFAULT_CATALOG_MAX_CHARS",
    "MAX_CATALOG_DESCRIPTION_CHARS",
    "SkillCatalogEntry",
    "SkillCatalogSnapshot",
    "build_skill_catalog",
    "escape_skill_text",
    "normalize_skill_line",
    "render_skill_catalog",
    "skill_catalog_message",
]
