# 58. Runtime skill provider

A running process can publish ephemeral skills at rank 250 via
`SkillRegistry.register_runtime_skill(definition)`.  This is intended for editor
extensions, ephemeral tool integrations, and dynamic workflows that want to
expose a procedure without writing files.

## Use case

- An editor extension registers a "format-on-save" skill for the current project.
- A workflow dynamically creates a skill for a one-time procedure.
- A plugin publishes a skill that exists only for the lifetime of the conversation.

## Why it matters

- Skills are not persisted to disk — they exist only in the running process.
- Rank 250 places them between project roots (200) and custom roots (300).
- The returned callback removes the skill again; no cleanup needed.
- Multiple runtime skills can coexist; a project skill with the same name outranks a runtime skill.

## How it works

### Registration

```python
from app.deepspace.services.skill_registry import skill_registry, SkillDefinition, SkillInvocationPolicy

registry = skill_registry(settings, workspace_root)

remove = registry.register_runtime_skill(SkillDefinition(
    name="dynamic-skill",
    description="Created at runtime",
    when_to_use="One-time procedure",
    invocation=SkillInvocationPolicy(model_invocable=True, user_invocable=True),
    source="runtime",
    provider="runtime",
    rank=250,
    content="Dynamic instructions...",
))

# Later, when the skill is no longer needed:
remove()  # Idempotent; safe to call twice
```

### RuntimeSkillProvider

- Created lazily on first `register_runtime_skill()` call.
- Single instance per registry (reused for all runtime skills).
- `discover()` returns all registered runtime skills.
- Skills have `provider="runtime"`, `source="runtime"`, `rank=250`.

### Precedence

| Rank | Source | Wins against |
|---|---|---|
| 100 | Project `.kinesis/skills` | — |
| 200 | Project `.agents/skills` | Project `.kinesis/skills` |
| 250 | **Runtime skills** | Project roots |
| 300 | Custom roots | Runtime skills |
| 350 | Remote catalog | Custom roots |
| 400 | Shared KineSis | Remote catalog |
| 500 | Shared agents | Shared KineSis |
| 600 | Bundled | Shared agents |

A project skill with the same name as a runtime skill wins (lower rank = higher priority).

### Auto content hash

`SkillDefinition.__post_init__` fills `content_sha256` when a provider does not supply it, so every runtime skill carries provenance.

### Scope filtering

Runtime skills are subject to the same scope filtering as other skills:
- `registry.get(name, scope=child_scope)` respects child agent grants.
- `registry.list_for_scope(scope)` filters runtime skills.
- Per-turn catalog injection includes runtime skills visible to the agent.

## What to watch for

- Runtime skills are process-local.  They do not survive a restart and are not shared across workers.
- The registry is per-workspace (tenant/user/workspace).  A runtime skill registered in one workspace is not visible in another.
- Multiple callers registering the same runtime skill name: first-wins within the runtime provider; later registrations are ignored (warning logged).
- The `remove()` callback is idempotent — safe to call multiple times.

## Related chapters

- [Bundled Skills](33-bundled-skills.md) — skill discovery, providers, and Office workflows.
- [Skill Scope and Delegation](55-skill-scope-and-delegation.md) — child agent skill visibility.
- [Per-turn Skill Catalog Injection](54-skill-catalog-and-injection.md) — how the model learns available skills.
