"""Skill visibility for one agent scope.

A conversation may delegate to a subagent.  The child must see the skills its
parent granted it and nothing else, even though both resolve the same
workspace roots.  A scope is an immutable node in a chain: each node may
narrow the set it inherited, and a revoked name stays revoked.

The chain is deliberately explicit.  A missing grant means "everything the
parent sees", which keeps the main agent behavior unchanged; an empty grant
means "nothing", which is how a child starts before its parent hands it a
skill.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.deepspace.services.skill_registry import SkillDefinition


class SkillScopeError(ValueError):
    """A scope chain is malformed, cyclic, or names an unknown scope."""


@dataclass(frozen=True, slots=True)
class SkillScope:
    """One node in a chain of skill-visibility grants."""

    key: str
    parent: SkillScope | None = None
    #: Names this node grants, or None to inherit the parent's whole set.
    granted: frozenset[str] | None = None
    #: Names this node removes even when an ancestor granted them.
    revoked: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not str(self.key or "").strip():
            raise SkillScopeError("A skill scope needs a key.")
        seen = {self.key}
        node = self.parent
        depth = 0
        while node is not None:
            depth += 1
            if depth > 64:
                raise SkillScopeError("The skill scope chain is too deep.")
            if node.key in seen:
                raise SkillScopeError("The skill scope chain contains a cycle.")
            seen.add(node.key)
            node = node.parent


def root_scope(key: str = "root") -> SkillScope:
    """Return a scope that inherits every skill in the workspace."""
    return SkillScope(key=key)


def child_scope(
    parent: SkillScope,
    key: str,
    *,
    granted: Iterable[str] | None = None,
    revoked: Iterable[str] = (),
) -> SkillScope:
    """Return a child scope that narrows what the parent may see.

    Args:
        parent: The scope that owns the child.
        key: A stable child identifier (for example a subagent id).
        granted: The names the child may see, or None to inherit them all.
        revoked: Names removed even if an ancestor granted them.

    Returns:
        The child scope.
    """
    return SkillScope(
        key=key,
        parent=parent,
        granted=(
            frozenset(str(name).casefold() for name in granted) if granted is not None else None
        ),
        revoked=frozenset(str(name).casefold() for name in revoked),
    )


def scope_chain(scope: SkillScope | None) -> tuple[SkillScope, ...]:
    """Return the chain from the root to the given scope."""
    if scope is None:
        return ()
    chain: list[SkillScope] = []
    node: SkillScope | None = scope
    while node is not None:
        chain.append(node)
        node = node.parent
    chain.reverse()
    return tuple(chain)


def revoked_names(scope: SkillScope | None) -> frozenset[str]:
    """Return every name removed anywhere in the chain.

    Revocation is absolute: a descendant cannot re-grant a name that an
    ancestor revoked, which keeps a parent's removal meaningful.
    """
    revoked: set[str] = set()
    for node in scope_chain(scope):
        revoked.update(node.revoked)
    return frozenset(revoked)


def visible_names(scope: SkillScope | None) -> frozenset[str] | None:
    """Return the names a scope may see, or None when it inherits everything.

    Args:
        scope: The scope to resolve, or None for the unscoped main agent.

    Returns:
        The visible names, or None when the chain never narrowed the set to an
        explicit grant.  A chain that only revokes names returns None here;
        callers filter with :func:`revoked_names` in that case.
    """
    allowed: frozenset[str] | None = None
    for node in scope_chain(scope):
        if node.granted is not None:
            allowed = (
                node.granted
                if allowed is None
                else frozenset(name for name in allowed if name in node.granted)
            )
    if allowed is None:
        return None
    return allowed - revoked_names(scope)


def filter_skills_for_scope(
    skills: Iterable[SkillDefinition], scope: SkillScope | None
) -> list[SkillDefinition]:
    """Filter a resolved catalog to the names one scope may see."""
    allowed = visible_names(scope)
    if allowed is None:
        revoked = revoked_names(scope)
        if not revoked:
            return list(skills)
        return [skill for skill in skills if skill.name.casefold() not in revoked]
    return [skill for skill in skills if skill.name.casefold() in allowed]


def scope_to_payload(scope: SkillScope | None) -> dict[str, object] | None:
    """Serialize the effective visibility of one scope.

    A durable child is resumed in another process, so its grants cannot ride
    on an in-memory parent reference.  The payload stores the resolved set
    rather than the chain: it is the exact catalog the parent allowed, which
    is what the child must keep after the original parent is gone.

    Args:
        scope: The scope to serialize, or None for an unrestricted agent.

    Returns:
        A JSON-compatible payload, or None when the scope inherits everything.
    """
    if scope is None:
        return None
    allowed = visible_names(scope)
    revoked = revoked_names(scope)
    if allowed is None and not revoked:
        return None
    return {
        "granted": sorted(allowed) if allowed is not None else None,
        "revoked": sorted(revoked),
    }


def scope_from_payload(payload: object, *, key: str = "restored") -> SkillScope | None:
    """Rebuild a scope from a persisted payload, failing closed on bad data.

    A malformed payload never widens visibility: a caller that cannot prove
    which names a child held receives an empty grant.  A missing payload keeps
    the historical behaviour, where the child inherits the workspace catalog.

    Args:
        payload: The value persisted with the child descriptor.
        key: A stable key for the rebuilt scope node.

    Returns:
        The rebuilt scope, or None when the payload records no restriction.
    """
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return SkillScope(key=key, granted=frozenset())
    if "granted" not in payload:
        return SkillScope(key=key, granted=frozenset())
    raw_granted = payload.get("granted")
    raw_revoked = payload.get("revoked", [])
    if raw_granted is not None and not isinstance(raw_granted, list):
        return SkillScope(key=key, granted=frozenset())
    if not isinstance(raw_revoked, list):
        return SkillScope(key=key, granted=frozenset())
    names = [*(raw_granted or []), *raw_revoked]
    if any(not isinstance(name, str) for name in names):
        return SkillScope(key=key, granted=frozenset())
    granted = (
        frozenset(name.strip().casefold() for name in raw_granted if name.strip())
        if raw_granted is not None
        else None
    )
    revoked = frozenset(name.strip().casefold() for name in raw_revoked if name.strip())
    if granted is None and not revoked:
        return None
    return SkillScope(key=key, granted=granted, revoked=revoked)


__all__ = [
    "SkillScope",
    "SkillScopeError",
    "child_scope",
    "filter_skills_for_scope",
    "revoked_names",
    "root_scope",
    "scope_chain",
    "scope_from_payload",
    "scope_to_payload",
    "visible_names",
]
