# 54. Per-turn skill catalog injection

The model learns what skills exist from a bounded, rebuilt-every-turn system
instruction.  A skill created, edited, or removed since the last turn appears
on the next turn without any stored state to invalidate.

## Use case

- A project adds a new skill; the model sees it immediately.
- A skill is edited; the model sees the new description and instructions.
- A skill is removed; the model stops seeing it on the next turn.
- A remote skill catalog goes offline; the model is told the catalog is incomplete.

## Why it matters

- No stored catalog to invalidate; the live registry is the source of truth.
- The model never guesses `skill_list`; it knows every visible skill before its first tool call.
- A failed provider does not silently look like "no skills exist" — the catalog carries `incomplete: true`.

## How it works

### Turn assembly

`chat_service.py` calls `prepare_skill_catalog_message()` during turn setup.  The
function:

1. Resolves the workspace root for the authenticated tenant/user.
2. Builds the ranked `SkillRegistry` from all seven providers.
3. Calls `build_skill_catalog()` with the registry, the admin flag, and the character budget.
4. If the snapshot has entries or is incomplete, returns a dynamic system message with:
   - `role: "system"`
   - `content`: the framed catalog text
   - `source`: metadata object with `kind: "skill-catalog"`, `digest`, `truncated`, `incomplete`, and per-entry provenance

### Catalog rendering (`skill_catalog.py`)

`build_skill_catalog(registry, for_model, for_admin, max_chars, description_chars, scope)`:

1. Calls `registry.snapshot()` → `(skills, observations)`.
2. Filters by `scope` (for child agents) if provided.
3. Filters by `for_model` (model-invocable only) and `for_admin` (include admin-only).
4. Creates `SkillCatalogEntry` for each eligible skill with escaped description.
5. Sorts by name (case-insensitive).
6. Computes a stable digest of all eligible entries (before budget truncation).
7. Adds entries in sorted order until `max_chars` budget is exhausted.
8. Returns `SkillCatalogSnapshot` with `entries`, `incomplete`, `truncated`, `digest`, `char_count`.

### Budget and truncation

- `deepspace_skill_catalog_max_chars` (default 12,000) bounds the rendered catalog.
- Entries cost `len(name) + len(description) + len(provider) + 8` characters.
- When truncated, `truncated: true` and the model sees a notice: "This catalog was shortened by the character budget. Call `skill_list` for the complete list."
- The `digest` always covers all eligible entries, so the model knows the list is partial.

### Incomplete catalogs

If any provider fails its two discovery attempts, `incomplete: true` is set.  The
rendered catalog includes: "Some skills could not be checked. Only confirmed skills are listed; a lower-priority definition of the same name may be selected when a preferred source is unavailable. Discovery will be retried on the next request."

An empty complete catalog produces no message.  An empty incomplete catalog still produces one, so a failed provider never looks like "no skills exist".

### Source metadata

The returned system message carries a `source` field:

```json
{
  "kind": "skill-catalog",
  "digest": "sha256...",
  "truncated": false,
  "incomplete": false,
  "entries": [
    { "name": "office", "provider": "bundled", "source": "bundled", "content_sha256": "..." },
    { "name": "review", "provider": "filesystem", "source": "project", "content_sha256": "..." }
  ]
}
```

This allows the frontend to render a skill picker without parsing prose.

## Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_skill_catalog_enabled` | `bool` | `true` | Enables per-turn catalog injection. |
| `deepspace_skill_catalog_max_chars` | `int` (0-200000) | `12000` | Character budget for the rendered catalog. |
| `deepspace_skill_cache_ttl_seconds` | `float` (0-60) | `1.0` | Registry cache TTL; `0.0` disables caching (tests default to `0.0`). |

## What to watch for

- The catalog is rebuilt every turn from the live registry.  A large catalog or many providers increases per-turn latency; tune the budget or disable catalog injection if necessary.
- The catalog injection is a dynamic system instruction (not persisted to the conversation history).  The `source` metadata on the system message carries the catalog digest and truncation state but is not surfaced to the UI.
- A child agent's skill scope is computed from the parent's effective scope at delegation time and stored in the child's descriptor.  A cold-resumed child restores the same scope from that payload.

## Related chapters

- [Bundled Skills](33-bundled-skills.md) — skill discovery, providers, and Office workflows.
- [Delegation and Subagents](04-delegation-and-subagents.md) — child agent skill scope inheritance.
- [The Tool Catalogue](09-the-tool-catalogue.md) — `skill_list`, `skill_load`, `skill_resource`, `skill_validate`.
