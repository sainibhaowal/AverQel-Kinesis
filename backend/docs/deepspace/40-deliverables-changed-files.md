# 40. Deliverables & Changed Files

Every DeepSpace conversation tracks the files it changed and the artifacts it declared as deliverables. The deliverables projection reads the typed session log and the in-process store to build the available picture. Durable `present` tool events provide a fallback when the API and worker are separate processes.

## Use case

- The user wants to see "what files did this conversation actually touch?"
- A reviewer needs the list of declared outputs (reports, diagrams, generated code) for a session
- The deployment wants an audit trail of filesystem mutations per conversation

## Why it matters

- Tool calls like `fs_write`, `fs_edit`, and `fs_delete` mutate files; the session log records the call, not the bytes
- The deliverables projection reconstructs changed paths from `tool/call` events and counts write/delete calls
- Declared deliverables (from the native `present` tool) are tracked separately in the deliverable store

## How it works

### Changed files projection

The `changed_file_projection` scans the session log for these write tool calls:

1. Finds `tool/call` events for `fs_write`, `fs_edit`, `fs_str_replace`, `fs_delete`, `ssh_fs_write`, and `ssh_fs_delete`
2. Extracts the target path from arguments
3. Counts write and delete calls per path
4. Returns first-seen paths with write/delete counts and the last turn/time

### Declared deliverables

The `declared_deliverable_projection` reads the in-process `DeepSpaceDeliverableStore`:

1. Turns record declared deliverables via the native `present` tool
2. The store holds the workspace-relative path, conversation, turn, and declaration time per deliverable
3. The projection returns all declared items for the conversation

### API

```
GET /api/v1/deepspace/chats/{conversation_id}/deliverables?limit=200
```

Response:

```json
{
  "changed_files": [
    {
      "path": "src/auth/jwt.ts",
      "write_count": 3,
      "delete_count": 0,
      "last_turn": 4,
      "last_changed_at": "2026-10-26T10:29:00+00:00"
    }
  ],
  "declared_deliverables": [
    {
      "path": "report.pdf",
      "conversation_id": "conversation-uuid",
      "turn": 4,
      "declared_at": 1793000940.0
    }
  ],
  "turn_changes": [],
  "store_available": true
}
```

### Frontend

The **Deliverables** tab in the side panel shows two sections:

- **Changed Files**: Files with write/delete counts and last-change metadata; the current panel lists them but does not make them clickable or open an editor
- **Declared Deliverables**: Explicitly declared workspace-relative paths with conversation, turn, and declaration-time metadata

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_session_log_encryption` | bool | `true` | Encrypts payloads; if `true`, changed file paths are not visible in SQL |

## What to watch for

- **Log dependency**: Changed files come from the typed log; if logging is disabled or events are pruned, the projection is incomplete
- **In-process store**: Size and turn-change metadata live in memory; durable `present` tool events allow declared paths to be reconstructed after a process boundary
- **Path resolution**: Paths are relative to the conversation's workspace root; absolute paths are not tracked
- **Encryption**: When `deepspace_session_log_encryption=true`, the projection cannot read tool arguments from SQL; it falls back to event metadata
- **Large outputs**: The projection reports paths and counts, not file bytes or MIME metadata. It does not inline tool-result contents.
- **Terminal writes**: `terminal` tool calls that write files via shell redirection are not captured by this projection; use `present` after the file exists when it should appear as a deliverable
