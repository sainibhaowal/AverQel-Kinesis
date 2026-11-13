"""Tests for the skill authoring validator."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.deepspace.services.skill_validation import (
    MAX_SKILL_BYTES,
    validate_skill_document,
)

pytestmark = pytest.mark.unit_no_db


VALID = """---
name: demo-skill
description: A valid skill.
when-to-use: Some task needs it.
model-invocable: true
user-invocable: true
---
# Instructions

Follow every step in order.
"""


def _codes(issues) -> list[str]:
    return [issue.code for issue in issues]


def test_valid_document_has_no_findings() -> None:
    assert validate_skill_document(VALID) == []


def test_empty_document_reports_a_single_error() -> None:
    issues = validate_skill_document("   \n")
    assert _codes(issues) == ["skill_empty"]
    assert issues[0].severity == "error"


def test_unclosed_frontmatter_is_reported() -> None:
    issues = validate_skill_document("---\nname: demo\n")
    codes = _codes(issues)
    assert "frontmatter_unclosed" in codes
    assert any(issue.severity == "error" for issue in issues)


def test_invalid_name_and_missing_description_fallback() -> None:
    issues = validate_skill_document("---\nname: Bad Name\n---\nBody text long enough.")
    assert "name_invalid" in _codes(issues)


def test_description_fallback_does_not_hide_missing_frontmatter_description() -> None:
    document = "---\nname: demo-skill\n---\nA useful-looking first line.\nMore instructions."

    issue = next(
        issue for issue in validate_skill_document(document) if issue.code == "description_missing"
    )
    assert issue.severity == "warning"


def test_empty_quoted_frontmatter_description_is_missing() -> None:
    document = VALID.replace("description: A valid skill.", 'description: "   "')

    assert "description_missing" in _codes(validate_skill_document(document))


def test_strict_names_reject_underscores() -> None:
    document = VALID.replace("demo-skill", "demo_skill")
    assert validate_skill_document(document) == []
    strict = validate_skill_document(document, strict_names=True)
    assert "name_invalid" in _codes(strict)


def test_short_instructions_are_a_warning() -> None:
    document = "---\nname: demo\ndescription: d\n---\nhi"
    issues = validate_skill_document(document)
    warning = next(issue for issue in issues if issue.code == "instructions_short")
    assert warning.severity == "warning"


def test_duplicate_name_is_a_warning() -> None:
    issues = validate_skill_document(VALID, existing_names=frozenset({"demo-skill"}))
    duplicate = next(issue for issue in issues if issue.code == "name_duplicate")
    assert duplicate.severity == "warning"
    assert "demo-skill" in duplicate.message


def test_oversized_document_is_an_error() -> None:
    document = "---\nname: demo\ndescription: d\n---\n" + "x" * MAX_SKILL_BYTES
    assert "skill_too_large" in _codes(validate_skill_document(document))
    assert "instructions_truncated" in _codes(validate_skill_document(document))


def test_resource_reachability(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "ref.md").write_text("reference", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    assert validate_skill_document(VALID, resource_paths=("ref.md",), resource_base=skill_dir) == []

    missing = validate_skill_document(
        VALID, resource_paths=("missing.md",), resource_base=skill_dir
    )
    assert _codes(missing) == ["resource_missing"]

    escaped = validate_skill_document(
        VALID, resource_paths=("../outside.md",), resource_base=skill_dir
    )
    assert _codes(escaped) == ["resource_escape"]

    unchecked = validate_skill_document(VALID, resource_paths=("ref.md",))
    assert _codes(unchecked) == ["resource_unchecked"]


def test_issues_serialize_for_tools() -> None:
    issue = validate_skill_document("")[0]
    assert issue.to_dict() == {
        "code": "skill_empty",
        "message": issue.message,
        "severity": "error",
    }
