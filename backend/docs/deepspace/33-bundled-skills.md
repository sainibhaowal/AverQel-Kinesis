# 33. Bundled Skills

DeepSpace contains a backend `BundledSkillsManager` with built-in skill
definitions for Office workflows (Word, Excel, PowerPoint) and quality badges
(Python, Security, Documentation). The manager is a service-level API in the
current release; `skill_list` and `skill_load` read installed workspace files
and do not automatically expose or install these definitions.

## Use case

- Embedding code wants ready-to-use skill text without writing it from scratch
- Office skills describe document/spreadsheet/presentation procedures
- Badge skills describe code quality, security, and documentation checks

## Why it matters

- Previously, `skill_load` only read workspace `.agents/skills/` directories
- The manager can list, read, and explicitly install the built-in definitions
- Installed files follow the same Markdown format as workspace skills

## How it works

### Architecture

1. **Configuration**: the manager reads the office and badge toggles; the global enabled, auto-install, and custom directory fields are declared but are not currently used to gate or route the manager
2. **Manager**: `BundledSkillsManager` holds built-in skill definitions
3. **Installer**: `install_skill()` writes skill Markdown to workspace `.agents/skills/`
4. **Integration**: `workspace_skills()` returns installed workspace skills only; it does not merge bundled definitions

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_bundled_skills_enabled` | bool | `true` | Declared master switch; the current manager does not use it to disable listing or installation |
| `deepspace_bundled_office_enabled` | bool | `true` | Enable Office skills |
| `deepspace_bundled_badge_enabled` | bool | `true` | Enable badge skills |
| `deepspace_bundled_auto_install` | bool | `true` | Declared auto-install setting; no automatic installation hook is currently wired |
| `deepspace_bundled_skill_dir` | str | `".agents/skills"` | Declared directory setting; the current manager uses the fixed `.agents/skills` path |

### Office skills

| Skill | Description | Tools used |
|-------|-------------|------------|
| `office_word` | Create/edit/convert Word docs | `fs_write`, `fs_read`, python-docx |
| `office_excel` | Create/analyze spreadsheets | `fs_write`, `fs_read`, openpyxl |
| `office_powerpoint` | Create/edit presentations | `fs_write`, `fs_read`, python-pptx |

### Badge skills

| Skill | Description | Checks |
|-------|-------------|--------|
| `badge_python` | Code quality | ruff, mypy, pytest, coverage |
| `badge_security` | Security scan | bandit, pip-audit, truffleHog |
| `badge_documentation` | Doc completeness | docstrings, README, API docs |

### Installation

```python
manager = get_bundled_skills_manager(settings)

# Install one skill to workspace
manager.install_skill("office_word", workspace_root)

# Install all bundled skills
results = manager.install_all(workspace_root)
# {"office_word": True, "office_excel": True, ...}

# Check if installed
manager.is_installed("office_word", workspace_root)
```

### Auto-install

There is no automatic installation on conversation creation or on `skill_list`
and `skill_load`. Call `install_skill()` or `install_all()` explicitly.

### Skill format

Each skill is a Markdown file with:

```markdown
# Skill Name

## Purpose
What this skill does.

## Procedures
### Procedure Name
1. Step 1
2. Step 2

## Tools Available
- tool_name for purpose
```

### Listing available skills

```python
manager = get_bundled_skills_manager(settings)
skills = manager.list_available()
# [{"id": "office_word", "name": "Office Word", "description": "Create..."}, ...]
```

## What to watch for

- **Dependencies**: Office skills need `python-docx`, `openpyxl`, `python-pptx`; badge skills need `ruff`, `mypy`, `pytest`, `bandit`, `pip-audit`, `trufflehog`
- **Installation**: `install_skill()` writes `SKILL.md` and can overwrite an existing file with the same skill id; it is not an idempotent-preserving installer.
- **Workspace isolation**: Skills installed per workspace; no cross-contamination
- **Versioning**: Bundled skills versioned with DeepSpace; updates on upgrade
- **Custom skills**: Users can still add `.agents/skills/custom_skill/SKILL.md` alongside bundled
- **Tool availability**: A skill is instructional Markdown, not an executable workflow. Its referenced libraries and tools must be available separately.
