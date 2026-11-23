# DeepSpace Skills — User Guide

**Date:** 2026-11-19

---

## What It Is

A **skill** is a reusable Markdown playbook that the agent can load and follow. Skills live in your workspace (or ship bundled with DeepSpace), and the agent discovers them on every turn.

**Use it when:**
- You have a repeatable process (code review, spreadsheet analysis, document formatting)
- You want the agent to follow a specific methodology
- You want to share a process inside a project

**Skip it when:**
- The task is one-off or exploratory
- The agent's default behavior already works well

---

## What You See in the UI

| Location | What It Shows |
|---|---|
| **Chat** | The agent lists skills with `skill_list`, loads one with `skill_load`, and reads linked files with `skill_resource` |
| **Workspace `.agents/skills/`** | Your project-scoped skills, discovered automatically |
| **In-app help** | `Documentation → DeepSpace` describes the skill workflow |

There is no separate Skills settings page in the Kinesis desktop build; skills are files in the workspace plus the bundled set.

---

## How to Use It (Natural Language)

| You Say | Agent Does |
|---|---|
| *"What skills are available?"* | `skill_list()` → returns the catalog with name, description, provider, and source |
| *"Load the office-xlsx skill"* | `skill_load(name="office-xlsx")` → the skill body enters the context |
| *"Read the checklist from that skill"* | `skill_resource(name="my-skill", path="checklist.md")` |
| *"Validate this skill draft before I install it"* | `skill_validate(content="...", name="my-skill")` |

---

## The Skill Catalog

`skill_list` returns the discovered skills, and the model also sees a framed catalog:

```
<available_skills>
- office-xlsx: Create, inspect, and edit Excel workbooks with Python. (provider: bundled)
- my-code-review: Security-focused code review checklist (provider: filesystem)
</available_skills>
```

Each entry carries:
- **Name** — the exact string for `skill_load`
- **Description** — one-line summary
- **Provider** — `bundled` for built-ins, `filesystem` for workspace files
- **Source** — which root discovered it (`bundled`, `project-agents`, and so on)

Invocation flags are parsed from frontmatter (`model-invocable`, `user-invocable`, `admin-only`), but DeepSpace does not currently expose a `/skill-name` slash command; ask the agent to load the skill.

---

## Built-in (Bundled) Skills

Kinesis ships these 13 skills:

| Skill | Purpose |
|---|---|
| `badge-documentation` | Verify documentation completeness for a changed area |
| `badge-python` | Verify Python code quality and best practices |
| `badge-security` | Scan code for security vulnerabilities before delivery |
| `office` | Choose and safely complete a Word, spreadsheet, or presentation task |
| `office-docx` | Create, inspect, and edit DOCX files with Python |
| `office-export-pdf` | Export a DOCX, XLSX, or PPTX file to PDF with LibreOffice |
| `office-pptx` | Create, inspect, and edit PowerPoint presentations with Python |
| `office-recalculate` | Recalculate workbook formulas with LibreOffice when available |
| `office-render` | Render Office files to inspect their visual layout |
| `office-xlsx` | Create, inspect, and edit Excel workbooks with Python |
| `office_excel` | Office Excel skill (legacy compatibility alias) |
| `office_powerpoint` | Office PowerPoint skill (legacy compatibility alias) |
| `office_word` | Office Word skill (legacy compatibility alias) |

These are always available — no install needed.

---

## Workspace Skills (Your Own)

Put skills under your workspace at:

```
<workspace>/.agents/skills/
├── my-code-review/
│   ├── SKILL.md          # instructions plus optional YAML frontmatter
│   └── checklist.md      # optional resource, read via skill_resource
└── quick-note.md         # or a flat Markdown skill
```

Discovery rules:
- A **directory** skill needs `SKILL.md` (or `skill.md` / `README.md`); its folder name is the fallback skill name.
- A **flat `.md` file** is a skill whose name is the file stem.
- The document body is the instruction; it is capped at 40,000 characters.
- Resources are any other files beside the skill document; read them with `skill_resource`, and paths cannot escape the skill directory.
- Nothing is cached across edits: the registry rescans on the next turn.

**Example `SKILL.md`:**

```markdown
---
name: my-code-review
description: Security-focused code review checklist
when-to-use: Reviewing a pull request before merge
model-invocable: true
user-invocable: true
admin-only: false
---

# Code review

1. Read the diff and list every changed entry point.
2. Check authentication, authorization, and input validation.
3. Run the project's tests and report the exact command and result.
```

Frontmatter is optional scalar YAML; the recognised keys are `name`, `description`, `when-to-use`, `model-invocable`, `user-invocable`, and `admin-only`.

---

## Validation Before Install

Before adding a skill to your workspace, validate it:

```
User: "Validate this skill draft"
Agent: skill_validate(content="...", name="my-skill")
```

Checks:
- Frontmatter is closed and its flags are `true` / `false`
- The name is lowercase words separated by hyphens or underscores (1–120 characters)
- The description is present (missing descriptions are a warning) and at most 500 characters
- The instruction body is at least 20 characters and at most 40,000 characters before truncation
- Referenced resources resolve under the skill directory
- A duplicate workspace name is reported as a warning

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| Skill not in catalog | No `SKILL.md` / `skill.md` / `README.md`, or a flat file that is not `.md` | Add the document at the expected path |
| *"Skill not found"* | Name mismatch or the skill is not model-invocable | Use the exact name from `skill_list` |
| Resource not found | Path wrong or the file is outside the skill directory | Keep resources beside the skill document and use a relative path |
| Validation fails | Bad frontmatter, invalid name, or oversized body | Fix the finding reported by `skill_validate` |
| Skill changes not visible | The name collides with a higher-ranked source | Rename the workspace skill or remove the duplicate |

---

## Technical Details (If You're Curious)

- **Discovery** — ordered roots, rescanned on demand; workspace skills live under `.agents/skills`
- **Catalog framing** — `<available_skills>` envelope for the model
- **Scope filtering** — workspace skills are visible only in their workspace
- **Content hashing** — SHA-256 of the truncated instruction body for change detection
- **Event log** — tool calls are recorded in the session event log
- **Telemetry** — content-free counters when enabled

---

## Related Documentation

| Topic | Document |
|---|---|
| Tool catalogue | `backend/docs/deepspace/09-the-tool-catalogue.md` |
| Skill registry internals | `backend/app/deepspace/services/skill_registry.py` |
| Skill validation | `backend/app/deepspace/services/skill_validation.py` |
| In-app help | `frontend/app/documentation/deepspace/page.tsx` (Skills section) |

---

**TL;DR:** Ask "what skills?", then "load X". Put your own under `.agents/skills/` as `SKILL.md` or a flat `.md` file. Bundled skills work out of the box.
