"""Tests for scope-layered skill visibility."""

from __future__ import annotations

import pytest

from app.deepspace.services.skill_registry import SkillDefinition, SkillInvocationPolicy
from app.deepspace.services.skill_scope import (
    SkillScope,
    SkillScopeError,
    child_scope,
    filter_skills_for_scope,
    revoked_names,
    root_scope,
    scope_chain,
    scope_from_payload,
    scope_to_payload,
    visible_names,
)

pytestmark = pytest.mark.unit_no_db


def _skill(name: str) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=name,
        when_to_use="",
        invocation=SkillInvocationPolicy(),
        source="project",
        provider="filesystem",
        rank=100,
        content=name,
    )


SKILLS = [_skill("alpha"), _skill("beta"), _skill("gamma")]


def test_root_scope_inherits_everything() -> None:
    scope = root_scope()
    assert visible_names(scope) is None
    assert revoked_names(scope) == frozenset()
    assert [skill.name for skill in filter_skills_for_scope(SKILLS, scope)] == [
        "alpha",
        "beta",
        "gamma",
    ]
    assert filter_skills_for_scope(SKILLS, None) == SKILLS


def test_child_grant_narrows_the_parent_set() -> None:
    root = root_scope()
    child = child_scope(root, "subagent-1", granted=["alpha", "BETA"])
    assert [skill.name for skill in filter_skills_for_scope(SKILLS, child)] == [
        "alpha",
        "beta",
    ]
    assert scope_chain(child) == (root, child)


def test_empty_grant_means_no_skills() -> None:
    child = child_scope(root_scope(), "empty", granted=[])
    assert visible_names(child) == frozenset()
    assert filter_skills_for_scope(SKILLS, child) == []


def test_grandchild_intersects_grants() -> None:
    root = root_scope()
    child = child_scope(root, "child", granted=["alpha", "beta"])
    grandchild = child_scope(child, "grandchild", granted=["beta", "gamma"])
    assert visible_names(grandchild) == frozenset({"beta"})
    assert [skill.name for skill in filter_skills_for_scope(SKILLS, grandchild)] == ["beta"]


def test_revocation_applies_without_an_explicit_grant() -> None:
    scope = child_scope(root_scope(), "child", revoked=["beta"])
    assert visible_names(scope) is None
    assert revoked_names(scope) == frozenset({"beta"})
    assert [skill.name for skill in filter_skills_for_scope(SKILLS, scope)] == [
        "alpha",
        "gamma",
    ]


def test_revocation_is_absolute_across_generations() -> None:
    root = root_scope()
    child = child_scope(root, "child", granted=["alpha", "beta"], revoked=["beta"])
    grandchild = child_scope(child, "grandchild", granted=["alpha", "beta"])
    assert visible_names(grandchild) == frozenset({"alpha"})
    assert [skill.name for skill in filter_skills_for_scope(SKILLS, grandchild)] == ["alpha"]


def test_scope_requires_a_key_and_rejects_cycles() -> None:
    with pytest.raises(SkillScopeError):
        SkillScope(key="")
    with pytest.raises(SkillScopeError):
        SkillScope(key="  ")

    root = root_scope("root")
    child = SkillScope(key="child", parent=root)
    with pytest.raises(SkillScopeError):
        SkillScope(key="root", parent=child)


def test_deep_chain_is_rejected() -> None:
    node = root_scope("n0")
    for index in range(1, 65):
        node = SkillScope(key=f"n{index}", parent=node)
    with pytest.raises(SkillScopeError):
        SkillScope(key="overflow", parent=node)


def test_scope_payload_preserves_effective_grants_and_absolute_revocations() -> None:
    root = root_scope()
    child = child_scope(root, "child", granted=["Alpha", "beta"], revoked=["beta"])

    assert scope_to_payload(None) is None
    assert scope_to_payload(root) is None
    assert scope_to_payload(child) == {"granted": ["alpha"], "revoked": ["beta"]}


def test_scope_payload_preserves_revocation_without_a_grant() -> None:
    scope = child_scope(root_scope(), "child", revoked=["Beta"])

    assert scope_to_payload(scope) == {"granted": None, "revoked": ["beta"]}


@pytest.mark.parametrize(
    "payload",
    [
        "invalid",
        7,
        {},
        {"granted": "alpha"},
        {"granted": 3},
        {"granted": ["alpha"], "revoked": "beta"},
        {"granted": None, "revoked": [None]},
    ],
)
def test_malformed_scope_payload_fails_closed(payload: object) -> None:
    restored = scope_from_payload(payload, key="restored-child")

    assert restored is not None
    assert restored.key == "restored-child"
    assert visible_names(restored) == frozenset()
    assert filter_skills_for_scope(SKILLS, restored) == []


def test_scope_payload_ignores_malformed_revocations_and_empty_names() -> None:
    restored = scope_from_payload(
        {"granted": ["alpha", ""], "revoked": []},
        key="restored-child",
    )

    assert restored is not None
    assert visible_names(restored) == frozenset({"alpha"})
    assert scope_from_payload({"granted": None, "revoked": []}) is None
    assert scope_from_payload({"granted": [], "revoked": []}) is not None
    assert scope_from_payload(None) is None
    assert visible_names(scope_from_payload({"granted": ["beta"]})) == frozenset({"beta"})
    assert visible_names(scope_from_payload({"granted": None, "revoked": ["beta"]})) is None
