# DeepSpace Filesystem Service — User Guide

**Date:** 2026-11-19

---

## Overview

DeepSpace's **unified filesystem service** (`DeepSpaceFileSystemService`) is the single service layer that powers all file operations in your workspace. Every file tool (`fs_read`, `fs_write`, `fs_edit`, `fs_str_replace`, `fs_delete`) and the unified `read`/`edit`/`delete` tools (with `target="workspace"`) delegate to this service.

The service provides **atomic writes**, **cross-process locking**, **file watching**, **patch application**, **str-replace**, and **version guards** — all behind a single consistent interface.

---

## What You Get

| Feature | What It Means for You |
|---------|----------------------|
| **Atomic writes** | No partial files. Every write is complete or doesn't happen. |
| **File locking** | Coordinate with other processes. `lock()`/`unlock()` with stale-PID detection. |
| **File watching** | Get notified when files change. `watch(path, callback)` fires on any mutation. |
| **Patch apply** | Apply unified diffs safely. `apply_patch(path, diff)` applies atomically. |
| **Str-replace** | Replace exact strings with version guards. Refuses if file changed. |
| **Version tracking** | Every file has a SHA-256 version. Edits can require the version you read. |

---

## How It Works

All file tools (`fs_read`, `fs_write`, `fs_edit`, `fs_str_replace`, `fs_delete`) and the unified tools (`read`, `edit`, `delete` with `target="workspace"`) go through the **same service**.

```
Your request → Tool handler → DeepSpaceFileSystemService → Workspace on disk
```

The service handles:
- Path resolution against your workspace root
- Sandbox enforcement (read-only, workspace-write, danger-full-access)
- Atomic writes with fsync + rename
- Cross-process locking via `.lock` files
- Version checking (SHA-256 of file content)
- File watcher notifications
- Observation policy: a file must be read before it can be modified (strict mode)

---

## Sandbox Modes

The service enforces the conversation's sandbox mode:

| Mode | What's Allowed |
|------|----------------|
| `read-only` | Read only. All writes refused. |
| `workspace-write` (default) | Read/write inside workspace. Host paths refused. |
| `danger-full-access` | Full host filesystem access (absolute paths, `..`, `~`). Requires explicit approval. |

The mode comes from your conversation settings (`deepspace_permission_mode`) or can be overridden per-request.

---

## File Versions & Concurrency

Every file has a **version** — the SHA-256 hash of its content (64 hex chars).

- `fs_read` returns the version
- `fs_edit` and `fs_str_replace` accept `expected_version`
- If the file changed since you read it, the edit is **refused** with `FS_STALE_VERSION`
- You get the current version in the refusal — read again, then retry

This prevents lost updates when multiple agents or processes edit the same file.

---

## Observation Policy (Strict Mode)

In addition to version guards, the service enforces an **observation policy**: a file must be read by the current conversation before it can be written. If you attempt to write a file you haven't read, the edit is refused with `FS_NOT_OBSERVED`. This ensures the agent has current context before making changes.

---

## Using the Tools

### Read a File

```json
{
  "name": "fs_read",
  "arguments": {
    "path": "src/main.py",
    "offset": 1,
    "limit": 100
  }
}
```

Returns: content with numbered lines, line count, version, size, and `truncated` flag.

### Write a File

```json
{
  "name": "fs_write",
  "arguments": {
    "path": "src/new_file.py",
    "content": "print('Hello')",
    "mode": "create"  // or "overwrite", "append"
  }
}
```

Modes: `create` (fails if exists), `overwrite` (replaces), `append` (adds to end).

### Edit a File

```json
{
  "name": "fs_edit",
  "arguments": {
    "path": "src/main.py",
    "operation": "replace",  // or "append", "rename", "move"
    "content": "new content",
    "expected_version": "abc123..."  // optional, from fs_read
  }
}
```

Operations: `replace` (full content), `append` (add to end), `rename`/`move` (needs `new_path`).

### Str-Replace (Exact String Replacement)

```json
{
  "name": "fs_str_replace",
  "arguments": {
    "path": "src/main.py",
    "old_string": "print('old')",
    "new_string": "print('new')",
    "replace_all": false,  // true = replace all occurrences
    "expected_version": "abc123..."
  }
}
```

- Refuses if `old_string` not found
- Refuses if multiple matches and `replace_all=false`
- Use `replace_all=true` to replace all occurrences

### Delete a File

```json
{
  "name": "fs_delete",
  "arguments": {
    "path": "src/old_file.py",
    "recursive": false  // true for directories
  }
}
```

---

## Advanced Features (via SDK / Programmatic Access)

If you're building integrations or using the SDK directly, the `DeepSpaceFileSystemService` exposes additional capabilities:

### File Locking

```python
await fs.lock("data.json", wait_ms=5000)
try:
    data = json.loads((await fs.read("data.json")).content)
    data["counter"] += 1
    await fs.write("data.json", json.dumps(data), mode="overwrite")
finally:
    fs.unlock("data.json")
```

### File Watching

```python
events = []
unwatch = fs.watch("src/main.py", lambda path: events.append(path))

# ... later, when file is modified via any tool ...
await fs.write("src/main.py", "new content", mode="overwrite")
# events == ["src/main.py"]

unwatch()  # Stop watching
```

### Patch Application

```python
patch = """@@ -1,3 +1,4 @@
 def hello():
+    print("starting")
     print("Hello, World!")
"""

await fs.apply_patch("src/main.py", patch)
```

### Directory Operations

```python
# List files
files = fs.list(".", recursive=True)

# Glob pattern
results = fs.glob("src/**/*.py")

# Grep search
results = fs.grep("TODO", path="src", case_sensitive=False)

# Stat
stat = fs.stat("src/main.py")
# stat.path, stat.size_bytes, stat.version, stat.is_file, etc.
```

---

## Error Codes

| Code | Meaning | What to Do |
|------|---------|------------|
| `FS_STALE_VERSION` | File changed since you read it | Read again, get new version, retry |
| `FS_AMBIGUOUS_EDIT` | File gone, can't check version | Read workspace again, locate file, retry |
| `FS_NOT_OBSERVED` | File not read by this conversation before write | Read the file first, then retry |
| `SANDBOX_MODE_REQUIRED` | Path outside workspace in restricted mode | Use workspace-relative path or enable Full access |
| `WORKSPACE_PATH_ERROR` | Path outside workspace in Full access | Use a path the backend user can access |
| `NOT_FOUND` | File doesn't exist | Check path, use `fs_glob` to locate |
| `IS_DIRECTORY` | Path is a directory | Use `fs_list` to list, or `recursive=true` for delete |
| `EXISTS` | File exists (create mode) | Use `overwrite` or different path |
| `NOT_EMPTY` | Directory not empty (delete) | Use `recursive=true` |
| `BINARY_FILE` | Binary file; use `read_binary` | Use the binary read method |
| `INVALID_ARGS` | Invalid arguments for operation | Check required fields for the operation |
| `LOCK_TIMEOUT` | Couldn't acquire lock | Retry, or check for stuck process |
| `UNAVAILABLE` | Workspace unavailable | Check workspace root exists and is writable |

---

## Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_permission_mode` | `read-only`, `workspace-write`, `danger-full-access` | `workspace-write` | Default sandbox mode |
| `deepspace_workspace_root` | path | `~/Kinesis` (desktop) or configured base | Workspace root directory |

---

## What to Watch For

- **Atomic writes are per-file** — concurrent writes to different files proceed in parallel
- **Locks are per-file** — locking `a.json` doesn't block `b.json`
- **Watchers are in-process** — they only fire for mutations through this service in the same process
- **Version guards are optional** — omitting `expected_version` behaves as before (last write wins)
- **Observation policy is strict** — a file must be read before write in the same conversation
- **Binary files** — `fs_read` uses the shared document extraction pipeline; unsupported formats return `capability_failure=true`
- **Large files** — writes capped at 400K chars; reads capped at 200K chars returned (with `truncated` flag)

---

## Related Documentation

| Topic | Document |
|-------|----------|
| Subagent guide | `DEEPSPACE_SUBAGENT_USER_GUIDE.md` |
| Permission guide | `DEEPSPACE_PERMISSION_USER_GUIDE.md` |

---

## TL;DR

- Single service powers all workspace file tools
- Atomic writes, cross-process locking, file watching, patch apply
- Version guards prevent lost updates (`FS_STALE_VERSION`)
- Observation policy requires read-before-write (`FS_NOT_OBSERVED`)
- Sandbox mode bounds where you can read/write
- SDK exposes locking, watching, patches, glob, grep, stat