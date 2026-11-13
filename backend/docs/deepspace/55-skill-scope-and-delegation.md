# 55. Skill scope and delegation

A conversation may delegate to a subagent.  The child must see the skills its
parent granted it and nothing else, even though both resolve the same
workspace roots.  A scope is an immutable node in a chain: each node may
narrow the set it inherited, and a revoked name stays revoked.

## Use case

- A parent agent grants a child access to only the `office` and `office-docx` skills.
- The child cannot see or load `office-xlsx`, `review`, or any other skill, even if it guesses the name.
- The child's per-turn catalog injection shows only the granted skills.
- The child's `skill_list`, `skill_load`, and `skill_resource` calls are filtered by the same scope.

## Why it matters

- Subagents are isolated by default; they only see what the parent explicitly grants.
- Scope chains are explicit: a missing grant means "everything the parent sees" (main agent behavior unchanged); an empty grant means "nothing" (child starts with no skills until parent hands it some).
- Revocation is absolute: a descendant cannot re-grant a name that an ancestor revoked.
- Cold-resumed children restore their exact skill visibility from the persisted descriptor payload.

## How it works

### Scope chain (`skill_scope.py`)

```python
# Root scope (unrestricted main agent)
root = root_scope()

# Child scope: narrows parent's set
child = child_scope(root, "subagent:123", granted=["office", "office-docx"], revoked=[])

# Grandchild: intersects grants
grandchild = child_scope(child, "subagent:456", granted=["office-docx"], revoked=[])

# Revocation without explicit grant
revoked_only = child_scope(root, "child", revoked=["office-xlsx"])
```

- `granted: None` → inherit parent's whole set.
- `granted: []` → empty set (no skills).
- `granted: ["alpha", "beta"]` → only these names.
- `revoked: ["gamma"]` → remove these even if ancestor granted them.

### Filtering

`filter_skills_for_scope(skills, scope)` is called at three boundaries:

1. **Catalog injection** — `build_skill_catalog(registry, scope=child_scope)` filters before budget truncation.
2. **Registry lookup** — `SkillRegistry.get(name, scope=...)` and `list_for_scope(scope)` filter at read time.
3. **Skill tools** — `skill_list`, `skill_load`, `skill_resource` read `context.skill_scope` and filter.

A child that loads a skill or reads a resource is filtered by its scope at lookup time, so it cannot reach an ungranted skill even if it guesses the name.

### Serialization for durable children

When a child is created via `subagent` or `subagent_fork`:

1. `_delegation_skill_scope(context, child_id, arguments)` builds the child's scope from the parent's effective scope + `skill_allow`/`skill_deny`.
2. `_record_skill_scope(child, scope)` stores the payload in `child.descriptor["skillScope"]`:
   ```json
   { "granted": ["office", "office-docx"], "revoked": [] }
   ```
3. On cold resume (`subagent_cold_resume.py`), `scope_from_payload()` rebuilds the exact scope from the descriptor payload.

### Subagent tool arguments (`tools/delegation.py`)

```python
class SubagentArguments(ToolArguments):
    skill_allow: list[str] | None = Field(default=None, max_length=64)
    skill_deny: list[str] | None = Field(default=None, max_length=64)
```

- `skill_allow`: the only skills the child may see. Omit to inherit parent's skills.
- `skill_deny`: skills the child may not see (revoked even if ancestor granted).
- External backends (ACP, Claude Code, Codex, DSH SDK) reject `skill_allow`/`skill_deny` — only the in-process backend enforces per-child skill filters.

### Registry integration

`SkillRegistry` methods now accept `scope`:

```python
registry.get(name, for_admin=..., scope=...)      # Returns None if scope denies
registry.list_for_scope(scope)                    # Returns filtered list
registry.snapshot()                               # Returns all; caller filters
```

The `ToolContext` carries `skill_scope: Any = None`.  The main agent has `None` (unrestricted).  Child agents receive their computed scope.

### Per-turn catalog for children

`_child_system_messages(context, child)` in `subagent_runner.py` calls
`prepare_skill_catalog_message(scope=context.skill_scope)`, so every child turn
gets a catalog scoped to its grants.

## Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_skill_enabled_names` | `str` | `""` | Optional comma/semicolon separated skill allowlist. |
| `deepspace_skill_disabled_names` | `str` | `""` | Optional comma/semicolon separated skill denylist. |

## What to watch for

- The skill scope is computed from the parent's effective scope at delegation time.  If the parent's scope changes later, the child does not automatically see the change (the child's scope is fixed at creation and stored in its descriptor).
- A child's tool context is built with its scope.  Every tool call the child makes (including nested `skill_list`, `skill_load`, `skill_resource`) is filtered by that scope.
- External subagent backends cannot enforce per-child skill filters.  Use the in-process backend (`backend: "in-process"`) for scoped children.
- The scope payload in the descriptor is the effective visibility (granted/revoked sets), not the full chain.  This makes cold resume independent of the parent process.

## Related chapters

- [Bundled Skills](33-bundled-skills.md) — skill discovery, providers, and Office workflows.
- [Delegation and Subagents](04-delegation-and-subagents.md) — child agent creation, tool filters, and cold resume.
- [Per-turn Skill Catalog Injection](54-skill-catalog-and-injection.md) — how the model learns available skills.
- [The Tool Catalogue](09-the-tool-catalogue.md) — `skill_list`, `skill_load`, `skill_resource`, `skill_validate`.
