# 56. Skill authoring and validation

Every skill is a Markdown file with YAML frontmatter.  The `skill_validate` tool
checks a draft skill before it is installed, preventing malformed skills from
silently disappearing at discovery time.

## Use case

- A contributor writes a new skill; CI runs `skill_validate` before merging.
- A user edits an existing skill; the editor validates on save.
- A deployment accepts skills from external sources; validation rejects bad ones.

## Why it matters

- A skill without frontmatter, with an invalid name, or with missing description is caught before it reaches the registry.
- Duplicate names are warned against the current workspace catalog.
- Resource reachability is checked (no path traversal).
- Size limits are enforced (40K chars body, 160K bytes file).

## How it works

### Frontmatter schema

```yaml
---
name: kebab-case-skill-name
description: One-line routing description shown in catalogs.
when-to-use: Optional extra routing guidance.
model-invocable: true      # default true
user-invocable: true       # default true
---
# Markdown instructions follow
```

- `name`: kebab-case by default (`^[a-z0-9]+(?:-[a-z0-9]+)*$`).  Underscores allowed unless `deepspace_skill_strict_names=true`.
- `description`: required, non-empty.
- `when-to-use`: optional.
- `model-invocable`: if `false`, skill excluded from model catalog and `skill_load` for model.
- `user-invocable`: if `false`, skill excluded from user slash/at-skill invocation (HTTP 403).

### Validation tool (`skill_validate`)

```json
{
  "name": "skill_validate",
  "arguments": {
    "content": "---\nname: my-skill\ndescription: A skill\n---\nInstructions...",
    "name": "my-skill"  // optional, for duplicate check
  }
}
```

Returns:
```json
{
  "status": "ok",
  "valid": true,
  "issues": []
}
```

Or with issues:
```json
{
  "status": "invalid",
  "valid": false,
  "issues": [
    { "code": "skill_invalid", "message": "invalid skill name", "severity": "error" },
    { "code": "instructions_short", "message": "instructions under 50 chars", "severity": "warning" },
    { "code": "name_duplicate", "message": "skill 'my-skill' already exists in workspace", "severity": "warning" }
  ]
}
```

### Issue codes

| Code | Severity | Meaning |
|---|---|---|
| `skill_empty` | error | Document is empty or whitespace only. |
| `frontmatter_unclosed` | error | Frontmatter delimiter `---` not closed. |
| `skill_invalid` | error | Name invalid (not kebab-case, or underscores with strict mode). |
| `instructions_short` | warning | Instruction body under 50 characters. |
| `name_duplicate` | warning | Skill name already exists in workspace. |
| `skill_too_large` | error | Source file exceeds 160,000 bytes. |
| `instructions_truncated` | warning | Instruction body exceeds 40,000 characters. |
| `resource_missing` | error | Referenced resource file not found. |
| `resource_escape` | error | Resource path traverses outside skill directory. |
| `resource_unchecked` | warning | Resource path supplied but no `resource_base` to validate against. |

### Resource reachability

When `resource_base` is supplied (e.g., the skill's asset directory), validation
checks that every `resource_paths` entry:

1. Exists.
2. Is a file (not a directory).
3. Does not escape the `resource_base` (no `..`, no symlinks out).

Without `resource_base`, a `resource_unchecked` warning is emitted.

### Strict names

`deepspace_skill_strict_names: true` rejects underscores in skill names
(kebab-case only).  Default `false` allows underscores for backward
compatibility with existing skills.

### Integration

- `skill_validate` tool is registered in `NATIVE_TOOL_SPECS` (read mode).
- `validate_skill_document()` function in `skill_validation.py` is the core logic.
- Registry discovery (`FilesystemSkillProvider`) runs validation implicitly; invalid skills are logged and skipped.

## Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_skill_strict_names` | `bool` | `false` | Require portable kebab-case filesystem skill names; rejects underscores. |

## What to watch for

- Validation is a pre-install check; it does not prevent a skill from being written to disk.  The registry simply won't discover invalid skills.
- The `skill_validate` tool accepts a `name` parameter for duplicate checking against the current workspace.  Without it, duplicate warnings are not emitted.
- Resource reachability requires a `resource_base` (the skill's asset directory).  For bundled skills, this is the packaged asset root.

## Related chapters

- [Bundled Skills](33-bundled-skills.md) — skill discovery, providers, and Office workflows.
- [The Tool Catalogue](09-the-tool-catalogue.md) — `skill_list`, `skill_load`, `skill_resource`, `skill_validate`.
- [Skill Scope and Delegation](55-skill-scope-and-delegation.md) — child agent skill visibility.
