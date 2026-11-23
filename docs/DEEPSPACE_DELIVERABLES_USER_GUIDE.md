# DeepSpace Deliverables User Guide

**Date:** 2026-11-19

**Audience:** End users working with file deliveries and workspace change tracking in DeepSpace

---

## Overview

DeepSpace tracks two kinds of file activity in every conversation:

1. **Changed Files** — Files modified by tool calls (`fs_write`, `fs_edit`, `fs_str_replace`, `fs_delete`, etc.)
2. **Declared Deliverables** — Files explicitly published via the `present` tool for download/review

Both appear in the **Deliverables** side panel and via API.

---

## The `present` Tool — Publishing Deliverables

When the agent needs to share a file for download, review, or separate access, it uses the **`present`** tool:

```
Agent calls: present(paths=["report.pdf", "data.csv", "slides.pptx"])
```

**What happens:**
- The files are recorded as **declared deliverables** with conversation, turn, and timestamp
- They appear in the Deliverables panel under **Declared Deliverables**
- The user can click to open/download the files
- A `deliverables/presented` event is logged in the session timeline

**Rules:**
- Accepts a `paths` array (1–4 workspace-relative paths)
- Every path must name an existing file inside the project workspace
- A path outside the workspace, or a file that does not exist, refuses the **whole call**
- The tool does not copy files; they stay in the workspace

**When to expect it:**
- Agent finishes generating a report, spreadsheet, or presentation
- Agent creates an artifact the user needs to review separately (not inline in chat)
- Agent explicitly says "I've published the report as a deliverable"

**What you see in chat:**
- The agent's response mentions the files (inline paths are clickable)
- The Deliverables panel updates with the new entries

---

## Changed Files — Automatic Tracking

Every file the agent modifies via tools is automatically tracked:

| Tool | Tracked? |
|------|----------|
| `fs_write` | ✅ |
| `fs_edit` / `fs_str_replace` | ✅ |
| `fs_delete` | ✅ |
| `ssh_fs_write` / `ssh_fs_delete` | ✅ |
| Shell redirections (`> file.txt`) | ❌ (use `present` after) |
| Terminal writes | ❌ (use `present` after) |

**What you see:**
- In the Deliverables panel → **Changed Files** section
- Each entry shows: path, write count, delete count, last turn, last change time
- Click a path to open the file in the workspace viewer

---

## Workspace Changes — Per-Turn Diffs

At the **end of each turn**, DeepSpace captures a git-based snapshot of workspace changes **if the workspace is a git repository**.

**What's recorded per turn:**
- Files added, modified, deleted, or renamed
- Lines added / removed per file
- Binary vs text detection
- Oversized file flag (>2 MB)

**Where to find it:**
- Deliverables panel → **Workspace Changes** (per turn)
- API: `GET /api/v1/host/chats/{id}/workspace-changes?turn=3`
- Diff viewer: `GET /api/v1/host/chats/{id}/workspace-changes/3/diff/0`

**What the diff shows:**
- Unified diff format (like `git diff`)
- Line-level changes for text files
- Binary/oversized files marked accordingly

**Requirements:**
- Workspace must be a git repository (`git init` if needed)
- Only turns with actual changes produce a `workspace/changes` event

---

## File Reference Guidance (System Prompt)

The agent receives guidance on how to reference files:

> **Inline reference:** Mention paths naturally — `src/main.py` (clickable)
>
> **Deliverable declaration:** Use `present(paths=["report.pdf"])` for separate access
>
> **@file references:** Use `@path/to/file` syntax to explicitly reference a file. This creates a clickable link and tells the system to include the file in context. Example: `"See \`@src/main.py\` for the entry point."`

This ensures consistent, clickable file references throughout the conversation.

---

## Slash Commands

### `/compact` — Trigger Compaction

Manually trigger a compaction of the conversation history:

```
/compact              # Basic compaction (elides old messages, prunes tool results, offloads images)
/compact --llm        # Enable LLM-based summarization
/compact --llm=true   # Same as --llm
/compact -l           # Short flag for LLM summarization
```

**What happens:**
- Emits a `compaction/requested` event
- Runs at the start of the next turn
- With `--llm`: uses LLM to summarize history, replaces old messages with a summary

**Status:** Implemented in the slash command registry. The `--llm` summarization path requires a configured LLM.

---

## Cross-Session References

When the agent references a previous conversation, you'll see a **Cross-Session References** section in the context with a summary of the linked conversation, key decisions, and open questions.

---

## How to Verify Features Are Working

### 1. Deliverables Panel

Open the side panel (📦 icon) → **Deliverables** tab:
- **Changed Files** — lists modified files with write counts
- **Declared Deliverables** — lists `present` tool outputs
- **Workspace Changes** — per-turn git diffs (click a turn to view line-level diffs)

### 2. Workspace Changes Diff Viewer

Click any turn in the **Workspace Changes** section to open the diff viewer:
- Shows every file changed in that turn with `+` (added) / `-` (removed) line counts
- Click a file to see the full line-level diff with old/new line numbers
- Added lines are green, removed lines are red, unchanged lines are gray
- Binary or oversized files show metadata only (no diff available)
- Copy the diff to clipboard with the copy button

### 3. In-Chat Indicators

- Agent says "Published as deliverable" or "Available in Deliverables panel"
- File paths in agent messages are clickable (open in viewer)
- Turn completion shows workspace changes summary if changes occurred

### 4. Session Timeline (Advanced)

The session log records these events:
- `deliverables/presented` — when `present` tool runs
- `workspace/changes` — at end of each turn (git-based diff)
- `session/reference` — cross-session reference created
- `compaction/start` — compaction pipeline started
- `compaction/summary` — LLM summary generated
- `compaction/end` — compaction completed

View via API:
```bash
GET /api/v1/deepspace/chats/{conversation_id}/deliverables
GET /api/v1/host/chats/{conversation_id}/workspace-changes
GET /api/v1/host/chats/{conversation_id}/workspace-changes/{turn}/diff/{file_index}
```

### 5. API Verification

```bash
# List deliverables
curl /api/v1/deepspace/chats/{id}/deliverables

# List all workspace changes (grouped by turn)
curl /api/v1/host/chats/{id}/workspace-changes

# List workspace changes for turn 3
curl /api/v1/host/chats/{id}/workspace-changes?turn=3

# Get diff for file 0 in turn 3
curl /api/v1/host/chats/{id}/workspace-changes/3/diff/0

# Present files via API (stub - tool is the main path)
curl -X POST /api/v1/host/chats/{id}/present \
  -H "Content-Type: application/json" \
  -d '{"files": [{"path": "report.pdf", "description": "Q4 Report"}]}'
```

---

## Troubleshooting

| Issue | Cause | Resolution |
|-------|-------|------------|
| Deliverable not appearing | File doesn't exist at `present` time | Ensure file exists before agent calls `present` |
| No changed files tracked | Only shell/terminal writes used | Use `fs_write`/`fs_edit` or call `present` after |
| Empty workspace changes | Not a git repo, or no changes in turn | Initialize git (`git init`) or make file edits |
| Path not clickable | Absolute path used | Use workspace-relative paths (`src/file.py`) |
| Diff missing | File is binary or >2 MB | Binary/oversized files show metadata only |
| `@file` not resolved | Path doesn't exist or typo | Use workspace-relative paths; check spelling |
| `/compact` not working | Not in active conversation | Run in active chat; check `--llm` flag |
| Cross-session ref missing | No prior conversation linked | Agent must create reference in prior chat |

---

## Quick Reference

| Feature | Trigger | UI Location | API |
|---------|---------|-------------|-----|
| **Present** | Agent calls `present(paths=[])` | Deliverables → Declared | `POST /api/v1/host/chats/{id}/present` |
| **Changed Files** | `fs_write`/`edit`/`delete` | Deliverables → Changed | `GET /api/v1/deepspace/chats/{id}/deliverables` |
| **Workspace Changes** | End of each turn (git repo) | Deliverables → Workspace Changes | `GET /api/v1/host/chats/{id}/workspace-changes` |
| **Per-File Diff** | Click in Workspace Changes | Diff modal | `GET /api/v1/host/chats/{id}/workspace-changes/{turn}/diff/{idx}` |
| **File References** | Agent mentions path | Inline clickable link | N/A |
| **@file References** | User types `@path/file` | Inline clickable link | N/A |
| **Cross-Session Refs** | Agent links prior chat | Context panel | N/A |
| **/compact** | User types `/compact` | N/A (slash command) | N/A |

---

## Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_file_references_enabled` | bool | `true` | Enable file reference system prompt |
| `deepspace_session_log_encryption` | bool | `true` | Encrypts payloads; affects path visibility in SQL |
| `deepspace_workspace_changes_enabled` | bool | `true` | Enable git-based workspace changes recorder |
| `deepspace_compaction_llm_enabled` | bool | `false` | Enable LLM-based summarization during compaction |
| `deepspace_compaction_llm_max_tokens` | int | `4000` | Max tokens for LLM summary |

---

## Summary for Users

**You don't need to do anything special.** The agent handles deliverables automatically:
1. When it creates a file you should review separately → it calls `present(paths=[])`
2. When it edits files → changes are tracked automatically
3. At each turn end → workspace diff is captured (if git repo)
4. All visible in the **Deliverables** side panel (📦)

**To check:** Open the Deliverables panel after any agent turn that creates or modifies files.