# 62. Workspace Registry Internals

The `WorkspaceRegistry` is the durable authority for workspace records, their
display order, and session membership.  It integrates with the storage domain
for persistence and the session persistence layer for history bootstrap.

## Use case

- A deployment needs to manage multiple workspaces per user.
- Sessions must be reliably associated with their workspace even after restarts.
- Archiving a session requires checking for active work across all providers.

## Why it matters

- Workspace order is stable and survives restarts (not derived from filesystem).
- Session membership is validated against canonical cwd paths, not stored paths.
- The bootstrap process reconstructs workspace order from session history when
  the registry is first initialized.

## How it works

### Storage schema (`storage_domain.py`)

The registry uses a KV table (`workspaces`) and a global record (`WorkspaceDomainState`):

```python
# WorkspaceRecord (stored per workspace)
{
    "path": "/absolute/canonical/path",
    "title": "Display title",
    "sessionIds": ["session-uuid-1", "session-uuid-2"],
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-01-01T00:00:00Z",
}

# WorkspaceDomainState (global)
{
    "initialized": true,
    "defaultWorkspaceId": "uuid-or-null",
    "workspaceIds": ["ws-uuid-1", "ws-uuid-2", ...],  # display order
    "archivedSessionIds": ["session-uuid-3", ...],
    "pinnedSessionIds": ["session-uuid-4", ...],
    "pendingMutation": None | {"operation": "create|delete", "workspaceId": "uuid"},
}
```

### Startup sequence (`workspace_registry.py:Service.init`)

1. Open storage domain (`workspaceDomainSpec`).
2. Recover pending mutation (create/delete that crashed mid-write).
3. Validate stored state (order matches table, no duplicate paths/sessions).
4. If not initialized:
   a. List all session headers from `sessionPersistence`.
   b. Build header index (canonical cwd → session).
   c. Bootstrap: group sessions by path, create workspace records, order by newest session.
   d. Mark initialized, persist order.
5. Else if table has entries: rebuild header index from stored headers.
6. Index live sessions from `sessions` service.
7. Rebuild `WorkspaceEntity` cache from table.
8. Report filtered candidates (sessions whose cwd no longer matches workspace path).

### Bootstrap detail

The bootstrap groups stored session headers by their canonical workspace path.
For each path:
- Takes the newest session timestamp as the workspace's "rank".
- Creates a workspace record with all sessions for that path not already accounted.
- Orders workspaces by newest-session timestamp (descending), then path.

This means a workspace's position in the list reflects its most recent activity.

### Session activity events

Two waterfall/parallel events coordinate archival:

| Event | Mode | Purpose |
|---|---|---|
| `workspace/session-activity` | waterfall | Ask all providers what runs for a session. Any activity refuses archive. |
| `workspace/session-stop` | parallel | Request providers stop the session's work (cancel turn, subagents, jobs, schedules). |

Listeners prepend their activity entries. The registry's innermost callback returns
an empty list, so a composition without providers archives freely.

### Archive / Pin / Unpin

- **Archive**: Moves session to `archivedSessionIds` (hidden from groupings). Drops pin. Optional `stopActivity` skips activity check and requests stops.
- **Pin**: Prepends to `pinnedSessionIds` (surfaced ahead of unpinned). Mutually exclusive with archive.
- **Unpin/Unarchive**: Removes from respective list. No existence check (removing unknown is idempotent).

### WorkspaceEntity

```python
class WorkspaceEntity:
    path: str                    # canonical absolute path
    title: str                   # display title
    sessionIds: list[str]        # filtered to valid sessions
    createdAt: str
    updatedAt: str

    # sessionIds getter filters:
    # - session must exist in live sessions OR session persistence
    # - session's canonical cwd must equal this workspace's path
```

### Error types

| Error | When raised |
|---|---|
| `WorkspaceUnknownSessionError` | Archive/pin names unknown session |
| `WorkspaceActiveSessionError` | Archive without `stopActivity` but activity reported |
| `WorkspaceArchivedSessionPinError` | Pin an archived session |
| `WorkspaceOrderInvalidError` | Reorder names unknown workspace |

## Settings

No dedicated settings; uses `storageDomain` and `sessionPersistence` services.

## What to watch for

- The bootstrap only runs once (when `initialized: false`).  Changing session
  history later does not reorder workspaces; use `insertBefore` for manual reorder.
- `pendingMutation` ensures a crashed create/delete can be recovered on next startup.
- Session header validation uses `realpath`; symlinks in cwd are resolved.
- Filtered candidates are logged at WARNING level; investigate if sessions
  unexpectedly disappear from workspace membership.

## Related chapters

- [Workspaces and Files](15-workspaces-and-files.md) — user-facing workspace operations.
- [Application and Shared Agent Paths](47-home-and-agent-paths.md) — path resolution.
- [Storage Domains](52-storage-domains.md) — underlying storage facility.
