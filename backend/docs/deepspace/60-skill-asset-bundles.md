# 60. Skill asset bundles

Office skills ship as self-contained asset bundles: each skill has its
`SKILL.md` instruction file plus a `references/` directory with supporting
documents.  The `skill_resource` tool serves these files, enabling skills to
include templates, schemas, and sidecar documentation.

## Use case

- The `office` skill includes a `references/sidecar.md` with Word template guidance.
- The `office-xlsx` skill includes `references/formulas-and-recalculation.md`.
- A custom skill bundles a JSON schema in `references/schema.json` and reads it via `skill_resource`.

## Why it matters

- Skills are self-contained: instructions + resources in one directory.
- `skill_resource` enforces path containment (no traversal, no symlinks out).
- Binary resources (images, PDFs) are returned as base64.
- Bundled skills work identically to filesystem skills — same `resource_base` protocol.

## How it works

### Asset directory structure

```
backend/app/deepspace/skill_assets/office/
├── office/
│   ├── SKILL.md
│   └── references/
│       └── sidecar.md
├── office-docx/
│   ├── SKILL.md
│   └── references/
│       └── word-processing.md
├── office-xlsx/
│   ├── SKILL.md
│   └── references/
│       └── formulas-and-recalculation.md
├── office-pptx/
│   ├── SKILL.md
│   └── references/
│       └── presentation-design.md
├── office-render/
│   ├── SKILL.md
│   └── references/
│       └── render-options.md
├── office-recalculate/
│   ├── SKILL.md
│   └── references/
│       └── recalculation-guide.md
└── office-export-pdf/
    ├── SKILL.md
    └── references/
        └── pdf-export-options.md
```

### BundledSkillsManager

`BundledSkillsManager.get_bundled_skill_entries()` returns:
```python
{
    "office": (document_text, Path(".../skill_assets/office/office")),
    "office-docx": (document_text, Path(".../skill_assets/office/office-docx")),
    ...
}
```

- Office skills: `resource_base` = asset directory (serves `references/`).
- Badge skills (`badge-python`, etc.): `resource_base` = `None` (inline only).

### Registry integration

`BundledSkillProvider.discover()` creates `SkillDefinition` with:
- `resource_base = asset_directory` for office skills.
- `resource_base = None` for badge skills.

### Reading resources

```python
# Model calls skill_resource
{
    "name": "office",
    "path": "references/sidecar.md"
}
```

Returns:
```json
{
  "status": "ok",
  "name": "office",
  "path": "references/sidecar.md",
  "size_bytes": 1234,
  "encoding": "utf-8",
  "content": "# Office sidecar\n\nTemplate guidance..."
}
```

Binary files (e.g., `references/template.png`):
```json
{
  "status": "ok",
  "encoding": "base64",
  "content": "iVBORw0KGgoAAAANSUhEUgAA..."
}
```

### Path containment

`SkillRegistry.read_resource(skill, relative_path)`:
1. Resolves `skill.resource_base` (must exist).
2. Resolves `base / relative_path`.
3. Rejects if:
   - Target not under base (`is_relative_to`).
   - Target is a symlink.
   - Target is not a file.
   - Target exceeds 160 KB.

### Installing to workspace

`BundledSkillsManager.install_skill("office", workspace_root)`:
- Copies `SKILL.md` to `.agents/skills/office/SKILL.md`.
- Copies entire `references/` directory.
- Returns `True` on success.

## Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_bundled_office_enabled` | `bool` | `true` | Includes bundled Office skills and enables `office_process` tool. |
| `deepspace_bundled_badge_enabled` | `bool` | `true` | Includes bundled quality-badge skills. |

## What to watch for

- Asset bundles are packaged via `collect_data_files("app")` in `averqel-backend.spec`; verify inclusion in desktop builds.
- `skill_resource` reads through the registry, so scope filtering applies — a child agent without the skill grant cannot read its resources.
- Binary resources are base64-encoded; large files should be avoided (160 KB limit).
- The `references/` directory is not scanned for nested skills — only the `SKILL.md` at the bundle root is discovered.

## Related chapters

- [Bundled Skills](33-bundled-skills.md) — skill discovery, providers, and Office workflows.
- [The Tool Catalogue](09-the-tool-catalogue.md) — `skill_resource` tool.
- [Office Processing](33-bundled-skills.md#office-processing) — `office_process` tool.
