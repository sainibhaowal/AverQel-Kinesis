"""Authoring checks for a skill document.

Discovery is intentionally forgiving: one malformed entry must not hide the
rest of the catalog.  That makes a silently ignored skill hard to diagnose,
so this module offers the same rules as a validator that reports every
problem with a stable code.  The ``skill_validate`` tool and the API surface
use it; discovery keeps its per-entry skip behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.deepspace.services.skill_registry import (
    parse_skill_document,
)

#: The largest skill document discovery accepts.
MAX_SKILL_BYTES = 160_000

#: The largest skill document body discovery keeps.
MAX_SKILL_CONTENT_CHARS = 40_000


@dataclass(frozen=True, slots=True)
class SkillValidationIssue:
    """One validator finding."""

    code: str
    message: str
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        """Return the issue as the tool and API consume it."""
        return {"code": self.code, "message": self.message, "severity": self.severity}


def _has_explicit_description(raw: str) -> bool:
    """Whether frontmatter supplies a nonempty description value."""
    if not (raw.startswith("---\n") or raw.startswith("---\r\n")):
        return False
    lines = raw.splitlines()
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return False
    value: str | None = None
    for line in lines[1:end]:
        key, separator, candidate = line.partition(":")
        if separator and key.strip().replace("_", "-").casefold() == "description":
            value = candidate.strip()
    if value is None:
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return bool(value.strip())


def validate_skill_document(
    raw: str,
    *,
    fallback_name: str = "",
    strict_names: bool = False,
    existing_names: frozenset[str] | None = None,
    resource_paths: tuple[str, ...] = (),
    resource_base: Path | None = None,
) -> list[SkillValidationIssue]:
    """Validate one skill document against the discovery contract.

    Args:
        raw: The full Markdown document, including optional frontmatter.
        fallback_name: The directory or file name used when frontmatter omits
            ``name``.
        strict_names: Require kebab case without underscores.
        existing_names: Names already installed, for duplicate detection.
        resource_paths: Relative resource paths the document references.
        resource_base: The directory those resources should resolve under.

    Returns:
        Every finding.  An empty list means discovery would accept the skill.
    """
    issues: list[SkillValidationIssue] = []
    size = len(raw.encode("utf-8", errors="replace"))
    if size > MAX_SKILL_BYTES:
        issues.append(
            SkillValidationIssue(
                "skill_too_large",
                f"The document is {size} bytes; discovery accepts at most {MAX_SKILL_BYTES}.",
            )
        )
    if not raw.strip():
        issues.append(SkillValidationIssue("skill_empty", "The document is empty."))
        return issues
    if raw.startswith("---\n") or raw.startswith("---\r\n"):
        lines = raw.splitlines()
        if not any(line.strip() == "---" for line in lines[1:]):
            issues.append(
                SkillValidationIssue(
                    "frontmatter_unclosed",
                    "The frontmatter starts with --- but has no closing ---.",
                )
            )
    parsed = None
    try:
        parsed = parse_skill_document(
            raw,
            fallback_name=fallback_name or "unvalidated",
            source="validation",
            provider="validation",
            rank=0,
            strict_names=strict_names,
        )
    except ValueError as exc:
        message = str(exc)
        if message.startswith("Skill names must"):
            issues.append(
                SkillValidationIssue(
                    "name_invalid",
                    "The name must use lowercase words separated by hyphens"
                    + ("." if strict_names else " or underscores."),
                )
            )
        else:
            issues.append(SkillValidationIssue("skill_invalid", message))
    if parsed is not None:
        if not _has_explicit_description(raw):
            issues.append(
                SkillValidationIssue(
                    "description_missing",
                    "No description was found; the model cannot tell when to load the skill.",
                    "warning",
                )
            )
        if len(parsed.content) < 20:
            issues.append(
                SkillValidationIssue(
                    "instructions_short",
                    "The instruction body is very short.",
                    "warning",
                )
            )
        if parsed.content_truncated:
            issues.append(
                SkillValidationIssue(
                    "instructions_truncated",
                    f"Discovery keeps only {MAX_SKILL_CONTENT_CHARS} characters of the body.",
                    "warning",
                )
            )
        if existing_names and parsed.name.casefold() in existing_names:
            issues.append(
                SkillValidationIssue(
                    "name_duplicate",
                    f"Another installed skill already uses the name {parsed.name!r}.",
                    "warning",
                )
            )
    for relative in resource_paths:
        if resource_base is None:
            issues.append(
                SkillValidationIssue(
                    "resource_unchecked",
                    f"Cannot check {relative!r} without a resource directory.",
                    "warning",
                )
            )
            continue
        try:
            target = (resource_base / relative).resolve(strict=True)
        except OSError:
            issues.append(
                SkillValidationIssue(
                    "resource_missing",
                    f"The referenced resource {relative!r} does not exist.",
                )
            )
            continue
        if not target.is_relative_to(resource_base.resolve()) or target.is_symlink():
            issues.append(
                SkillValidationIssue(
                    "resource_escape",
                    f"The referenced resource {relative!r} leaves the skill directory.",
                )
            )
    return issues


__all__ = [
    "MAX_SKILL_BYTES",
    "MAX_SKILL_CONTENT_CHARS",
    "SkillValidationIssue",
    "validate_skill_document",
]
