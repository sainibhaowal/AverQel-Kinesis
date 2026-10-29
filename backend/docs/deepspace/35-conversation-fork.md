# 35. Conversation Fork

DeepSpace supports forking conversations at any persisted message or session-log turn, creating a divergent copy with its own event sequence and storage objects.

## Use case

- User wants to explore an alternative path from a previous turn
- Team wants to branch a conversation for parallel experimentation
- User wants to preserve a conversation state before a risky operation

## Why it matters

- Previously, conversations were linear; no way to branch
- Now `fork_conversation()` creates a divergent copy from any turn/message
- Forked conversation inherits history up to fork point, then diverges

## How it works

### Architecture

1. **Configuration**: `deepspace_fork_enabled=true` plus copy options
2. **Service**: `ConversationForkService` handles fork logic
3. **API**: `POST /api/v1/deepspace/chats/{id}/fork` creates fork
4. **Copy**: Messages, events, workspace files, file versions, and media artifacts are copied up to the fork point when file copying is enabled

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_fork_enabled` | bool | `true` | Master switch |
| `deepspace_fork_max_depth` | int | `10` | Max fork nesting |
| `deepspace_fork_copy_messages` | bool | `true` | Copy messages up to fork point |
| `deepspace_fork_copy_events` | bool | `true` | Copy session events |
| `deepspace_fork_copy_files` | bool | `true` | Copy workspace files |
| `deepspace_fork_suffix` | str | `" (fork)"` | Title suffix for forks |

### API

```
POST /api/v1/deepspace/chats/{conversation_id}/fork
{
  "turn": 5,
  "message_id": "optional-message-uuid",
  "title": "Optional custom title",
  "copy_files": true
}
```

Response:
```json
{
  "id": "new-uuid",
  "title": "Original title (fork)",
  "kind": "deepspace",
  "created_at": "2026-10-26T10:30:00Z",
  "updated_at": "2026-10-26T10:30:00Z"
}
```

### Fork logic

1. **Source**: Load the source conversation within the authenticated tenant and user.
2. **Fork point**: `message_id` copies through that message; `turn` copies events through that turn; omitting both copies the latest state.
3. **Create**: Create a new conversation and enforce the configured maximum lineage depth.
4. **Copy messages**: Copy message versions and preserve their timestamps while assigning new message and version IDs.
5. **Copy events**: Copy selected events and renumber them from sequence 1; append a durable fork-lineage event.
6. **Copy files**: When `copy_files` is true, copy workspace folders, files, versions, media artifacts, and private storage blobs with new destination keys.
7. **Return**: Return the normal conversation resource for the new conversation.

### Lineage

The fork service stores lineage in the new conversation's typed session log as a `conversation/forked` event. The event carries the source conversation ID, fork depth, and requested message or turn boundary without adding model-visible history.

### Client integration

The DeepSpace web client adds a **Fork here** action to completed assistant
responses. It sends the response message ID to the same `POST` route, requests
the configured file-copy behavior, adds the returned conversation to history,
and activates it. The API remains available for a precise `turn` or
`message_id` request. A deployment-level `deepspace_fork_copy_files=false`
setting cannot be overridden by a client request.

## What to watch for

- **Fork depth**: `max_depth` prevents infinite nesting; default 10
- **Resource usage**: Each fork copies messages/events; large conversations = more storage
- **File copying**: Workspace files copied on fork; can be large; optional
- **Ownership**: Forked conversation owned by same user/tenant
- **Events**: Session events copied up to fork point; new events have new sequence numbers
- **Subagents**: Child agents not copied; fork starts fresh
- **Metadata**: Fork depth is tracked in the typed log; no current UI projects
  fork lineage.
- **Permissions**: Only conversation owner can fork
- **Cleanup**: No automatic orphan-fork cleanup is implemented by this service.
