# 15. Workspaces and files

A workspace is the directory tree a conversation works in. DeepSpace gives
every account a default workspace and lets it keep named workspaces beside
it, and this chapter describes how a conversation chooses one, which tools
work inside it, and how the file tools keep a path inside it.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [The default workspace](#the-default-workspace)
  - [Named workspaces](#named-workspaces)
  - [The per-conversation choice](#the-per-conversation-choice)
  - [The routes](#the-routes)
  - [The file tools](#the-file-tools)
  - [The document readers](#the-document-readers)
  - [The path rules](#the-path-rules)
  - [The search tools](#the-search-tools)
  - [The version guards](#the-version-guards)
  - [The diff tool](#the-diff-tool)
  - [The file tree the panel shows](#the-file-tree-the-panel-shows)
- [What to watch for](#what-to-watch-for)

## New in this release: Durable project registry

The workspace catalogue is now backed by a durable per-account registry that
stores the user's project order, stable identities, session groupings, and
session order inside each project. The registry lives in
`.workspaces/.registry.json` beside the registration directories and is written
atomically with `os.replace` and `fsync`, serialized across processes with an
advisory lock, so a crash leaves either the old or the new manifest, never a
half-written file.

Key capabilities the registry adds:

- **Stable identity** — Every registration carries a UUID identifier that
  survives rename and path changes. The identifier is the registration's
  directory name; renaming a workspace changes only its display name, never
  the identifier or the folder.
- **User ordering** — Workspaces are listed in the order the user arranges
  them (`moveWorkspace(workspaceId, beforeId?)`). New workspaces are appended.
- **Rename** — `PATCH /workspaces/{id}` changes the display name without
  moving the directory or changing the identifier.
- **Reorder** — `PUT /workspaces/{id}/order?before=` moves a workspace before
  another or to the end.
- **Project → session grouping** — `GET /workspaces/projects` returns every
  project with its member conversations in the user's order. A conversation's
  workspace selection (the `workspace/selected` session event) remains the
  source of truth; the registry caches the grouping for the sidebar.
- **Ordered sessions inside a project** — `PUT /workspaces/{id}/sessions/order`
  and `PUT /workspaces/{id}/sessions/{conversationId}/order` let the user
  drag-and-drop conversations within a project.
- **Non-destructive delete** — `DELETE /workspaces/{id}` removes the
  registration and moves its internal directory into `.deleted/`; a project
  directory the workspace pointed at is never touched. A crashed delete is
  finished on the next load by a tombstone in the manifest.
- **Missing directory** — If a custom or internal workspace directory
  disappears, the registry preserves the record and reports
  `status: "missing-dir"`. The workspace is not recreated; the UI can show
  "Folder missing".
- **Active-work guard** — Deleting a workspace checks for live agent runs,
  queued turns, subagents, and terminal sessions. If work is active the
  delete returns `WORKSPACE_IN_USE` with the kinds of work blocking it. The
  caller may retry with `?stop_sessions=true` to close terminal sessions
  only; agent work must finish or be cancelled first.
- **Crash-safe writes** — The manifest is written atomically with a temporary
  file and `os.replace` + `fsync`. A pending-mutation marker and the
  registration markers together guarantee recovery without data loss.

## Use case

- A user works on two projects and wants each conversation to keep its own
  files apart.
- The agent needs to read a file, change it, and then show what changed.
- A user opens the file panel to look at the tree the agent is working in
  while the agent works.
- A test runs in the terminal while the agent edits the source it tests.

## Why it matters

- One tree for everything means two conversations overwrite each other's
  files.
- A path that is not checked means a tool call can read or write a file the
  account does not own.
- An edit that does not check the file's current content loses a change
  another process made in the meantime.
- A tree that is not shown leaves the user guessing what the agent is
  working on.

## How it works

The Library and the workspace are different storages. The Library holds one
document per conversation as a database row. The workspace is a directory
tree on disk that outlives one conversation, and it is what a coding agent
edits.

In Read-only and Workspace modes, local file tools, shell commands,
terminals, search, and diffs stay inside the conversation's selected
workspace. Full access can use other paths the backend process's OS user can
reach. Routine host file edits do not require a separate approval; deletes
outside the workspace, recursive deletes, `.git` changes, and protected
operating-system or user-configuration paths do. Changing the workspace closes
its persistent terminal sessions.

### The default workspace

Every account has one default workspace. Its identifier is `default` and its
name is `Default`. In the standalone desktop app, its path is `~/Kinesis`.
Shared deployments and other local runtimes keep the account-scoped path
`<base>/<tenant id>/<user id>`.

An explicit `deepspace_workspace_root` keeps the account-scoped layout. With
no explicit root, standalone desktop uses `~/Kinesis` itself for its default
project workspace. Other local runtimes use `<AVERQEL_HOME>/workspaces`, which
is `~/.averqel-harness/workspaces` by default, with tenant and user folders
below it. Shared deployments also keep tenant and user folders below their
configured base. Desktop named-workspace registrations live under
`<AVERQEL_HOME>/workspaces`; the project directories the user selects remain
at their chosen paths.

On first use after this change, desktop copies files from the previous
`<AVERQEL_HOME>/workspaces/<tenant>/<user>` default into `~/Kinesis` without
deleting that source. It also migrates files from the old
`~/Kinesis/<tenant>/<user>` default directly into `~/Kinesis`. Existing files
are never overwritten. When there are no symlinks, files move if their names
are free; a conflict stays in the old folder. If symlinks exist, ordinary files
are copied and the old folder is retained so relative links keep their meaning.
The migration is recorded after its first attempt, so old files are not copied
back after a user edits or deletes the new copy.
Named-workspace registrations from the old Kinesis ID folders are copied into
app data and the old catalogue is removed only after every item is safely
present there. Conflicting or symlink catalogue items remain in the old
location, which remains the active catalogue when migration cannot finish. A
custom folder such as `MY DATA` remains at its selected path.

The desktop's application-data root and shared `.agents` root are separate;
`AVERQEL_AGENTS_HOME` defaults to `~/.agents` and is not created just by
resolving it. See [Application and shared agent paths](46-home-and-agent-paths.md)
for path expansion, safe joining, display labels, and watcher canonicalization.

### Named workspaces

An account may keep any number of named workspaces beside the default. On
desktop, each registration lives under
`<AVERQEL_HOME>/workspaces/<tenant id>/<user id>.workspaces/<slug>`. In other
runtimes it lives under
`<base>/<tenant id>/<user id>.workspaces/<slug>`. The registration directory
is outside the default workspace, so a named workspace never appears inside
the default file tree and a conversation working in one cannot reach the
others through the file tools.

A workspace has two names. The identifier is the slug of the name, which is
the name in lower case with its spaces replaced by dashes, and it is the
directory the workspace lives in. The display name is the name the user
gave, and the workspace directory records it in a
`.deepspace-workspace.json` file, so the catalogue can publish the name the
user chose rather than the slug derived from it.

A workspace name may hold letters, digits, spaces, dashes, underscores, and
dots. It may not exceed 64 characters, may not contain `..`, and must hold
at least one letter or digit. A name the account already uses is refused
with the status code 409.

### Stable identity and user ordering

Every workspace registration carries a stable UUID identifier (the `id` field)
that is the registration's directory name. This identifier never changes —
renaming a workspace changes only the display name, and moving a workspace to
a different project directory changes only the `path` field. The user's chosen
order is stored in the registry's `order` array and persists across restarts.
New workspaces are appended to the end of the order.

### Rename a workspace

`PATCH /api/v1/deepspace/chats/workspaces/{id}` changes the display name
without moving the directory or changing the identifier. The name must pass
the same validation as a new workspace and must not conflict with another
workspace's name (case-insensitive). The response returns the updated
workspace record with its stable `id`, new `name`, unchanged `path`, and
`status`.

### Reorder workspaces

`PUT /api/v1/deepspace/chats/workspaces/{id}/order` moves one workspace
before another. The request body is `{ "before": "<workspace-id>" }`; omitting
`before` or setting it to `null` moves the workspace to the end. The default
workspace always stays first and cannot be moved. The response returns the
full workspace list in the new order.

### Project sidebar: grouped conversations

`GET /api/v1/deepspace/chats/workspaces/projects` returns every project in the
user's order, each with its member conversations in the user's session order.
A conversation's membership is still determined by its latest
`workspace/selected` session event; the registry caches the grouping for fast
sidebar rendering. Conversations with no recorded selection appear under the
default workspace. A conversation whose recorded workspace was removed also
falls back to the default workspace.

The response shape is:

```json
{
  "workspaces": [
    {
      "id": "default",
      "name": "Default",
      "path": "/home/user/Kinesis",
      "is_default": true,
      "status": "ok",
      "sessions": [
        { "id": "<conversation-uuid>", "title": "Design notes", "updated_at": "2026-11-10T10:00:00Z" }
      ]
    },
    {
      "id": "reports",
      "name": "Reports",
      "path": "/home/user/projects/reports",
      "is_default": false,
      "status": "missing-dir",
      "sessions": [
        { "id": "<conversation-uuid>", "title": "Release plan", "updated_at": "2026-11-10T09:00:00Z" }
      ]
    }
  ],
  "default": "default"
}
```

The `status` field is `"ok"` when the registered directory exists, or
`"missing-dir"` when it does not. The UI displays a warning badge for
`missing-dir` projects.

### Ordered sessions inside a project

`PUT /api/v1/deepspace/chats/workspaces/{id}/sessions/order` replaces the
entire session order for one project. The request body is
`{ "conversation_ids": ["<uuid>", ...] }`. The order is deduplicated and only
conversations that currently work in the project are accepted.

`PUT /api/v1/deepspace/chats/workspaces/{id}/sessions/{conversationId}/order`
moves one conversation before another within the same project. The request
body is `{ "before": "<conversation-uuid>" }`; omitting `before` appends it
to the end.

Both endpoints return the stored order. The UI implements drag-and-drop
reordering with these endpoints.

### Non-destructive delete

`DELETE /api/v1/deepspace/chats/workspaces/{id}` removes a named workspace
from the catalogue without destroying user data. The registration directory
is moved into `.deleted/` under the account's named workspace root, preserving
any files the user created inside it. A custom project directory that the
workspace pointed at is never touched. The default workspace cannot be deleted.

If the manifest write succeeds but the directory move is interrupted (for
example by a crash), the next load reads the `tombstones` array and finishes
the move, so no registration is left in a half-deleted state.

### Missing directory is reported, not recreated

If a workspace's custom project directory or internal registration directory
disappears (deleted externally, unmounted, or moved), the registry preserves
the record and reports `status: "missing-dir"`. The workspace is **not**
recreated, so the user sees "Folder missing" in the sidebar and can decide
whether to point the workspace at a new directory or remove the registration.
`workspace_root_for` raises `WorkspaceUnavailableError` instead of silently
creating an empty directory.

### Active-work guard on delete

Before removing a workspace, the catalogue checks every conversation that
works in it for live work:

- Agent runs with status `running`, `awaiting_user`, `awaiting_approval`,
  `awaiting_plan_review`, or `cancelling`.
- Queued turns with status `queued` or `running`.
- Subagent runs with status `running` or `busy`.
- Live terminal sessions.

If any are found, the delete returns `409` with the error code
`WORKSPACE_IN_USE`, a summary message, and a `details.activities` array
listing each kind of work and the conversation it belongs to. The caller may
retry with `?stop_sessions=true` to close terminal sessions and then delete;
agent work and queued turns must finish or be cancelled through their normal
paths first.

### Crash-safe registry

The manifest at `.workspaces/.registry.json` is written atomically:

1. A process-wide lock and an advisory file lock (`flock` on POSIX,
   `msvcrt.locking` on Windows) serialize writers.
2. The new JSON is written to a temporary file in the same directory.
3. The temporary file is `fsync`ed, then `os.replace`d over the manifest.
4. The parent directory is `fsync`ed where the platform supports it.

A crash at any point leaves either the old manifest or the new one, never a
partial write. On load, the registry:

- Quarantines a corrupt manifest (moves it to `.registry.json.corrupt-<ts>`)
  and rebuilds from the registration markers.
- Finishes any tombstones left by interrupted deletes.
- Reconciles registration directories that have no manifest entry (legacy
  migrations or external creations) by reading their markers.

The per-account lock and the atomic replace together guarantee that concurrent
writes and crashes cannot corrupt the catalogue.

### The per-conversation choice

A conversation names the workspace it works in. The choice is recorded in the
typed session log as a `workspace/selected` event, so it survives a restart
and cannot be changed out of band.

The effective workspace is the conversation's last recorded selection, and
the account's default workspace when there is none. A workspace that belongs
to another account is refused exactly as one that does not exist, so a
caller cannot learn that it exists.

The composer carries a **workspace** menu that lists the account's
workspaces and offers **Add workspace…** to create another one.

### The routes

| Route | What it does |
|---|---|
| `GET /api/v1/deepspace/chats/workspaces` | Lists the caller's workspaces and names the default. |
| `POST /api/v1/deepspace/chats/workspaces` | Creates one named workspace. |
| `GET /api/v1/deepspace/chats/workspaces/projects` | Lists the projects and the conversations grouped under each one. |
| `PUT /api/v1/deepspace/chats/workspaces/{id}/order` | Moves one workspace before another in the user's order. |
| `PUT /api/v1/deepspace/chats/workspaces/{id}/sessions/order` | Replaces the session order of one workspace. |
| `PUT /api/v1/deepspace/chats/workspaces/{id}/sessions/{conversationId}/order` | Moves one conversation before another inside its workspace. |
| `PATCH /api/v1/deepspace/chats/workspaces/{id}` | Renames one workspace without changing its identity. |
| `DELETE /api/v1/deepspace/chats/workspaces/{id}` | Removes a named workspace (guarded by active-work check). |
| `GET /api/v1/deepspace/chats/{id}/workspace` | Reads the workspace the conversation works in and the recorded choice. |
| `PUT /api/v1/deepspace/chats/{id}/workspace` | Records the workspace the conversation works in. |
| `GET /api/v1/deepspace/chats/{id}/workspace/tree?path=` | Lists one directory of the conversation's workspace. |
| `GET /api/v1/deepspace/chats/{id}/workspace/file?path=` | Reads one text file of the conversation's workspace. |

### The file tools

| Tool | Mode | What it does |
|---|---|---|
| `fs_read` | read | Reads a workspace text file or an authorized supported document with numbered extracted lines. |
| `fs_write` | write | Creates, overwrites, or appends to a workspace text file. |
| `fs_edit` | write | Replaces, appends, renames, or moves a workspace file. |
| `fs_str_replace` | write | Replaces one exact string inside a workspace file. |
| `fs_delete` | write | Deletes a workspace file or directory. |

The unified `read`, `edit`, and `delete` tools also reach the workspace. They
take `target="workspace"` and a `path`. Workspace `read` rejects a missing or
blank path before dispatch and asks the model to locate the file with `fs_glob`
or another authorized listing; it never attempts a read with an absent path.
The `read` tool names the workspace line budget `line_limit`, because its
`limit` already means something else for a Library query.

`fs_read` returns the file's content with numbered lines, the total line
count, whether more lines follow, and the file's version. The `limit`
argument caps the lines one read returns at 5 000, and 2 000 by default. A
file larger than 200 000 characters is reported as truncated. Supported
binary documents are extracted through the same bounded ingestion readers as
Library uploads, and the result includes `content_type`, `extraction_method`,
page/OCR metadata, warnings, and the reader capability that was used.

`fs_write` takes a `mode` of `create`, `overwrite`, or `append`. `create`
refuses a file that already exists. `append` refuses a file that does not
exist. A write may hold 400 000 characters.

`fs_edit` takes an `operation` of `replace`, `append`, `rename`, or `move`.
`replace` and `append` need `content`. `rename` and `move` need `new_path`.

### The document readers

The workspace reader is deliberately shared with Library ingestion so a file
does not need to be uploaded merely to be parsed. The active reader inventory
is available through `averqel_inspect_query` with `capability="files"`; it
reports supported extensions, extraction methods, conversion requirements,
OCR/vision settings, byte/page/text limits, and whether LibreOffice is
available for conversion-backed formats.

The built-in readers cover PDF text and OCR, plain text and Markdown, common
source/config/data formats, DOCX, PPTX, XLSX, common raster images, and
bounded LibreOffice conversion for supported legacy Office and OpenDocument
formats. A small download-only fallback handles the configured iWork/WPS
extensions when it can recover text. This is a capability inventory, not a
claim that every proprietary binary format has a lossless parser.

Uploads check the filename extension and declared MIME type, then use file
signatures as a fallback when the declaration is not compatible. Windows uses
portable signatures for PDF, common raster images, ZIP-based Office files,
legacy Office files, and UTF-8 text, so MIME validation does not depend on a
native libmagic DLL. Archive uploads still pass through the separate archive
security checks.

When a format is unsupported, a reader dependency is unavailable, a file is
too large, or extraction fails safely, `fs_read` returns
`capability_failure=true`, `stop_turn=true`, `no_retry=true`, and a stable
error code plus the supported extensions. DeepSpace reports that exact
limitation and does not switch to `bash`, `find`, `sandbox_execute`, browser
navigation, or guessed alternate readers. The user can then import/upload the
file, enable the required deployment capability, or provide a supported
representation.

### The path rules

Read-only and Workspace paths must resolve inside the selected workspace.
Absolute paths, `..` escapes, and symlinks that leave the workspace are
refused. Each path may be at most 1024 characters and 64 segments. Shell
sessions in these modes require an operating-system cage that enforces the
same boundary; without one, the shell refuses to start.

Full access also accepts absolute paths, `~`, and paths containing `..`,
after normal path and symlink resolution. The backend still runs as the local
OS user, so files that user cannot access remain unavailable. Search tools
with a wildcard and no `path` search only the selected workspace. For one
exact filename in Full access, `fs_glob` with no `path` makes a single bounded
lookup across the selected workspace, the current user's home, `/mnt`, and
`/media` (stopping at the first match). For a different host folder, the model
may provide its absolute path. The model must not start an ordinary search at
`/`; it may use `path="/"` only with `entire_system=true` when the user
explicitly asks for the entire system.
`fs_glob` and `fs_grep` return `search_root` so the result records exactly
where it looked. File writes
outside the selected workspace and every delete require a direct approval.
Common operating-system and disk-damaging commands are blocked.

A path that breaks these rules is a refusal the model can act on, not a
crash. The result carries the message and the path, and marks the refusal as
terminal for the current turn so the model cannot loop through alternate
tools. In Workspace mode, a host path such as `~/Downloads/report.pdf` is not
authorized; Full access must be effective before the backend can attempt it.

### The search tools

| Tool | Mode | What it does |
|---|---|---|
| `fs_grep` | read | Searches workspace file contents with a regular expression. |
| `fs_glob` | read | Lists workspace paths by name pattern. |

`fs_grep` takes a `pattern`, an optional `path`, an `entire_system` flag, an
optional `glob` filename filter, an `output_mode`, a `case_sensitive` flag,
and a `max_results`.

| `output_mode` | What the result carries |
|---|---|
| `content` | The matching lines, with their path and line number. |
| `files_with_matches` | The names of the files that matched. |
| `count` | The number of matches in each file. |

A pattern that is not a valid regular expression is refused before the
search runs.

`fs_glob` takes a `pattern`, an optional `path`, an `entire_system` flag, and a
`max_results`. The
pattern matches against the full relative path and against the file name. A
`**` in the pattern is a path-aware wildcard, so `src/**/*.ts` matches the
path and not only the file name.

For an exact filename in Full access, omit `path` and call `fs_glob` once.
The result lists `search_roots`, absolute matching paths, and `scanned_entries`.
The walk checks nearby folders before deep folders, runs outside the API event
loop, and stops after 10,000 entries. `search_truncated` distinguishes a
bounded partial search from a complete no-match result; `skipped_paths` records
permission-denied or protected paths. Do not repeat the same lookup through
shell commands or alternate spellings. DeepSpace permits at most four
`fs_glob`/`fs_grep` calls per turn, including calls before and after an
approval pause, so the model must report the evidence it has rather than spin.

`fs_grep` and `fs_glob` reject a Full-access search whose resolved path is `/`
unless `entire_system=true` is present. This makes a whole-host scan an
intentional tool call rather than an accidental interpretation of a broad
filename request.

In Read-only and Workspace modes, both tools stay inside the workspace. Full
access may search another host path when the path is explicitly supplied.
Both tools cap traversal at 10,000 filesystem entries. `fs_grep` reads at most
5,000 files, reports at most 200 matches, and shows at most 500 characters of
a single matching line. A glob returns at most 2,000 paths. The filesystem
walk runs on a worker thread so a slow host directory does not block the API's
event loop.

Neither tool descends into `.git`, `node_modules`, `__pycache__`, `.venv`,
`venv`, `.mypy_cache`, or `.pytest_cache`.

Host searches use a streaming directory walk rather than eagerly materializing
the entire tree. They never descend into `/proc`, `/sys`, `/dev`, `/run`,
`/boot`, or `/root`, even in Full access, because these are virtual,
process-backed, kernel, or administrator-owned trees rather than ordinary user
document storage. An unreadable directory does not fail the whole search. The
result includes `skipped_paths`, with each skipped path and a reason such as
`permission denied while reading directory` or `protected system tree`. This
keeps an explicit Full-access path useful without requiring `sudo` or exposing
the search to a single protected Linux directory failure.

### The version guards

A file's version is the lowercase hexadecimal SHA-256 digest of its bytes,
64 characters long. `fs_read` returns it, and `fs_edit` and `fs_str_replace`
accept it as `expected_version`.

When the file on disk no longer holds the expected version, the edit is
refused with the code `FS_STALE_VERSION` and nothing is written, so a
concurrent change is never lost. The refusal carries the version the file
holds now, so the model can read the file again and retry.

When the file has gone entirely, the refusal carries the code
`FS_AMBIGUOUS_EDIT` and asks the model to read the workspace again.

The guard is optional. An edit without an `expected_version` behaves as it
always did.

### The diff tool

`workspace_diff` answers one question: what changed in the workspace. It
takes an optional `path` to limit the diff to one file or directory, and an
optional `max_bytes`.

A workspace that is a git repository is answered with git's own diff, which
covers both the unstaged and the staged changes. The result carries
`source: git` and the list of changed paths.

A workspace that is not a repository has no history, so the tool falls back
to the newest snapshot the workspace recorded and diffs the two file sets.
The result carries `source: snapshot`. When no baseline exists yet, the
result says so and lists the files the workspace holds.

The baseline is recorded before each turn's work begins, so the diff shows
what that turn changed.

The default size of the diff is 200 000 bytes and the largest is 1 000 000.
A diff that does not fit carries the note that it was truncated and that
some changes are not shown. A git command gets 15 seconds.

### The file tree the panel shows

The Files tab of the side panel lists the conversation's workspace. A
directory expands inline and fetches its entries the first time it opens,
and clicking a file loads its text into the preview below the tree.

The listing shows the name, the path, the kind, and the size in bytes of
each entry. It never walks into `.git`, `node_modules`, `__pycache__`,
`.venv`, `venv`, `dist`, `build`, or `.next`, and it hides any entry whose
name begins with a dot. One listing returns at most 500 entries.

The preview reads at most 512 000 bytes and says so when the file is
longer. It also names the language of the file from its extension, so the
preview can highlight it.

## What to watch for

- The workspace is a directory on the server's disk, not a row in the
  database. A server restart does not lose it, and a server with no
  persistent volume does.
- A conversation that never records a selection works in the account's
  default workspace. Moving a conversation to a named workspace means
  recording the selection again.
- A named workspace holds one display name file, `.deepspace-workspace.json`.
  Deleting it leaves the directory in place with a slug as its name.
- **The durable registry at `.workspaces/.registry.json` is the source of
  truth for project order, session grouping, and session order. The
  registration markers remain the portable record; a missing or corrupt
  manifest is rebuilt from them.**
- **Deleting a workspace never destroys user data. The registration moves to
  `.deleted/`; a custom project directory is untouched. A crashed delete is
  finished on the next load.**
- **A missing workspace directory is reported as `missing-dir` and is never
  silently recreated. The UI should surface this state so the user can
  re-point or remove the registration.**
- **Deleting a workspace with live agent runs, queued turns, subagents, or
  terminals returns `WORKSPACE_IN_USE`. The caller must stop that work first;
  only terminal sessions can be force-closed with `?stop_sessions=true`.**
- **The manifest is written atomically with `os.replace` + `fsync` under a
  process lock and advisory file lock. Concurrent writes and crashes cannot
  corrupt it.**
- Read-only and Workspace refuse paths outside the selected workspace. Full
  access allows host paths available to the local OS user and asks for
  approval before external writes or deletes.
- The version guard is optional, and an edit that names no version can
  overwrite a change another process made.
- `FS_STALE_VERSION` and `FS_AMBIGUOUS_EDIT` are refusals the model can
  recover from by reading the file again.
- A search that hits its cap reports `truncated`, so a broad pattern over a
  large tree may not show every match.
- The search tools skip the dependency and cache directories, so a match
  inside them is never found.
- The diff of a workspace that is not a git repository depends on a baseline
  snapshot. The baseline is in memory, so it does not survive a restart.
- The diff of a non-repository workspace lists the files whose hash changed
  and then shows their text differences.
- The file tree in the panel hides dotfiles, so a file the agent created
  under a dotted name is not visible there.
- The panel's preview stops at 512 000 bytes, and it is a plain text
  preview, not an editor.
