# DeepSpace Permissions and Sandbox Modes User Guide

**Date:** 2026-11-19

**Audience:** End users controlling what the agent may do in DeepSpace

---

## Overview

DeepSpace controls agent actions with three separate layers:

| Layer | Controls | Where It Lives |
|---|---|---|
| **Agent preset** | Which tools the agent may use, delegation depth, plan default, spill threshold | Per conversation |
| **Sandbox mode** | Whether writes and execution are allowed, and where they may land | Per conversation |
| **Approval mode** | Whether a restricted write pauses for approval | Deployment setting |

They are independent. A permissive sandbox does not add tools a preset withholds, and a preset with shell tools still cannot write in `read-only` mode.

---

## Agent Presets

The built-in presets are:

| Preset ID | Name | Sandbox | Delegation | Best For |
|---|---|---|---|---|
| `standard` | Standard | deployment default (`workspace-write`) | depth 3 | General development |
| `ptc` | Programmatic tool calling | deployment default | depth 1 | One program that calls native tools |
| `minimal` | Minimal | `read-only` | none | Quick questions, code reading |
| `creator` | Creator | deployment default | none | Notes, Library files, artifacts, memory |
| `custom` | Custom | configurable | configurable | Your own saved tool list and settings |

A preset can only narrow what the deployment allows. It cannot grant a tool the deployment disabled.

### API

```bash
# Catalogue of presets available to the user
GET /api/v1/deepspace/chats/presets

# Preset recorded for one conversation
GET /api/v1/deepspace/chats/{conversation_id}/preset

# Choose a preset for the conversation
PUT /api/v1/deepspace/chats/{conversation_id}/preset
{"preset": "standard"}

# Choose a saved custom preset
PUT /api/v1/deepspace/chats/{conversation_id}/preset
{"preset": "custom_01a1f...", "settings": {"tools": ["fs_read", "fs_write"]}}
```

Related endpoints: `GET/PUT /api/v1/deepspace/chats/presets/default` for the user's default, and `GET/PUT /api/v1/deepspace/chats/presets/enablement` for hiding presets the user does not want.

---

## Sandbox Modes

| Mode | Filesystem Access |
|---|---|
| `read-only` | Read and inspect only; every write tool is refused |
| `workspace-write` | Write inside the conversation workspace and temp dirs; system paths stay out of reach |
| `danger-full-access` | Full filesystem access; use only for trusted work |

### API

```bash
GET /api/v1/deepspace/chats/{conversation_id}/sandbox-mode
PUT /api/v1/deepspace/chats/{conversation_id}/sandbox-mode
{"mode": "workspace-write"}
```

Changing the mode closes live terminals for that conversation, because a terminal opened under one fence must not keep running under another.

---

## Approval Mode

Approval is a deployment setting (`deepspace_approval_mode`):

| Value | Behavior |
|---|---|
| `never` | No approval prompts; the sandbox mode alone bounds the agent. This is the local default. |
| `ask` | A write or execution in a restricted mode pauses and raises a permission card |

When a turn is awaiting approval, the UI shows an approval card. Approving or rejecting it resumes the paused turn through `POST /api/v1/deepspace/chats/{conversation_id}/approvals/{approval_id}`.

---

## The `/permission` Command

The chat slash command has a small, fixed vocabulary:

```
/permission auto
/permission ask
/permission deny
```

An unknown value is refused with `Invalid mode. Use: auto, ask, or deny`. The command replies with the selected mode and records it in the command result.

The durable controls are the preset, sandbox-mode, and deployment approval settings above; changing them through the API or settings is what the next turn enforces.

---

## Choosing a Setting

| Situation | Recommended |
|---|---|
| Reading code, answering questions | `minimal` preset, `read-only` |
| Writing code, running tests | `standard` preset, `workspace-write` |
| Automating a trusted local workflow | `standard` or `custom`, `danger-full-access` only if genuinely needed |
| Note and document work | `creator` preset |

---

## Troubleshooting

| Issue | Solution |
|---|---|
| A tool is refused | Check the conversation preset; the tool may not be in its allowlist |
| A write is denied | The sandbox is `read-only`; switch to `workspace-write` in Settings or the API |
| A write paused | Approval mode is `ask`; approve the card to resume the turn |
| `/permission` accepted a mode but behavior did not change | The slash command is informational; set the preset and sandbox mode explicitly |
| Custom preset missing | It belongs to the user who saved it; check `GET /presets` |

---

## Related

- [DeepSpace Jobs Guide](DEEPSPACE_JOBS_USER_GUIDE.md) — Background work
- [DeepSpace Questions Guide](DEEPSPACE_QUESTIONS_USER_GUIDE.md) — Answering structured questions
- [DeepSpace Settings Forms Guide](DEEPSPACE_SETTINGS_FORMS_USER_GUIDE.md) — Deployment settings
