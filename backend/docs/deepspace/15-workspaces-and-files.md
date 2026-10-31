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
  - [The path rules](#the-path-rules)
  - [The search tools](#the-search-tools)
  - [The version guards](#the-version-guards)
  - [The diff tool](#the-diff-tool)
  - [The file tree the panel shows](#the-file-tree-the-panel-shows)
- [What to watch for](#what-to-watch-for)

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

Every tool that touches a file runs in the conversation's own workspace. That
list is the file tools, file search, the shell, the terminals, the diff, the
skills, the workflows, the type graph, the extensions, and the plugin code.

### The default workspace

Every account has one default workspace. Its identifier is `default` and its
name is `Default`. Its path is `<base>/<tenant id>/<user id>`.

The base is `deepspace_workspace_root` when the deployment sets it. When the
setting is empty, the local desktop uses `<AVERQEL_HOME>/workspaces`, which is
`~/.averqel-harness/workspaces` by default. The account directory is created
when first needed. A user may also attach any existing project folder from the
workspace picker; registration metadata stays under the app root, while the
project stays in the folder the user selected.

### Named workspaces

An account may keep any number of named workspaces beside the default. Each
one lives in its own directory under
`<base>/<tenant id>/<user id>.workspaces/<slug>`. That directory is a
sibling of the account's default workspace rather than a folder inside it, so
a named workspace never shows up in the default workspace's file tree and a
conversation working in one cannot reach the others through the file tools.

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
| `GET /api/v1/deepspace/chats/{id}/workspace` | Reads the workspace the conversation works in and the recorded choice. |
| `PUT /api/v1/deepspace/chats/{id}/workspace` | Records the workspace the conversation works in. |
| `GET /api/v1/deepspace/chats/{id}/workspace/tree?path=` | Lists one directory of the conversation's workspace. |
| `GET /api/v1/deepspace/chats/{id}/workspace/file?path=` | Reads one text file of the conversation's workspace. |

### The file tools

| Tool | Mode | What it does |
|---|---|---|
| `fs_read` | read | Reads a workspace text file with numbered lines. |
| `fs_write` | write | Creates, overwrites, or appends to a workspace text file. |
| `fs_edit` | write | Replaces, appends, renames, or moves a workspace file. |
| `fs_str_replace` | write | Replaces one exact string inside a workspace file. |
| `fs_delete` | write | Deletes a workspace file or directory. |

The unified `read`, `edit`, and `delete` tools also reach the workspace. They
take `target="workspace"` and a `path`. The `read` tool names the workspace
line budget `line_limit`, because its `limit` already means something else
for a Library query.

`fs_read` returns the file's content with numbered lines, the total line
count, whether more lines follow, and the file's version. The `limit`
argument caps the lines one read returns at 5 000, and 2 000 by default. A
file larger than 200 000 characters is reported as truncated. A file that
holds binary content is reported as such rather than read as text.

`fs_write` takes a `mode` of `create`, `overwrite`, or `append`. `create`
refuses a file that already exists. `append` refuses a file that does not
exist. A write may hold 400 000 characters.

`fs_edit` takes an `operation` of `replace`, `append`, `rename`, or `move`.
`replace` and `append` need `content`. `rename` and `move` need `new_path`.

### The path rules

Every path a tool resolves is checked in several steps. The path must be
relative, so an absolute path is refused. The path may not contain a `..`
segment. The path may not exceed 1024 characters or 64 segments.

The path is then joined to the workspace root and checked again after
symlink resolution, so a link inside the workspace that points outside it is
refused too.

A path that breaks these rules is a refusal the model can act on, not a
crash. The result carries the message and the path.

### The search tools

| Tool | Mode | What it does |
|---|---|---|
| `fs_grep` | read | Searches workspace file contents with a regular expression. |
| `fs_glob` | read | Lists workspace paths by name pattern. |

`fs_grep` takes a `pattern`, an optional `path`, an optional `glob`
filename filter, an `output_mode`, a `case_sensitive` flag, and a
`max_results`.

| `output_mode` | What the result carries |
|---|---|
| `content` | The matching lines, with their path and line number. |
| `files_with_matches` | The names of the files that matched. |
| `count` | The number of matches in each file. |

A pattern that is not a valid regular expression is refused before the
search runs.

`fs_glob` takes a `pattern`, an optional `path`, and a `max_results`. The
pattern matches against the full relative path and against the file name. A
`**` in the pattern is a path-aware wildcard, so `src/**/*.ts` matches the
path and not only the file name.

Both tools stay inside the workspace and both cap what they return. A search
reads at most 5 000 files, reports at most 200 matches, and shows at most
500 characters of a single matching line. A glob returns at most 2 000 paths.

Neither tool descends into `.git`, `node_modules`, `__pycache__`, `.venv`,
`venv`, `.mypy_cache`, or `.pytest_cache`.

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
- The path rules refuse an absolute path. A tool that needs a path outside
  the workspace cannot be given one.
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
