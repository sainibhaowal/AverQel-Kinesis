# 47. Application and shared agent paths

## Use case

A desktop user wants application databases and caches kept separate from
shared agent configuration, while projects may live at an explicitly chosen
path.

## Why it matters

One ambiguous home directory scatters data or makes global skills disappear
when a conversation switches workspaces. Path labels should be readable, but
they must not weaken workspace confinement.

## How it works

DeepSpace has two distinct per-user roots:

| Root | Override | Default | Purpose |
|---|---|---|---|
| Application data | `AVERQEL_HOME` | `~/.averqel-harness` | Databases, Library storage, caches, secrets, logs, and desktop profile. |
| Shared agent configuration | `AVERQEL_AGENTS_HOME` | `~/.agents` | User-level agent skills and related configuration. |

Both roots accept `~` and `~/...` paths. The desktop launcher passes the
resolved values to its local backend. The shared `.agents` root is not created
or migrated automatically. On the standalone desktop only, skill discovery
combines workspace `.agents/skills` with shared `~/.agents/skills`; a
workspace skill with the same name takes precedence. Shared skills are not
read in a shared server deployment.

`app/core/home_paths.py` provides safe relative joins, home-friendly display
labels, and canonical paths for filesystem watchers. Workspace-root resolution
uses the same tilde expansion; plugin HMR canonicalizes watched source paths
so symlink aliases do not create duplicate watcher entries. Existing workspace
paths, tenant boundaries, and the non-destructive workspace migration remain
unchanged.

## What to watch for

- `AVERQEL_HOME` and `AVERQEL_AGENTS_HOME` are local process configuration;
  they are not model-controlled path inputs.
- Shared skills are an intentional local-desktop feature. A server must not
  point all tenants at one user's host `.agents` directory.
- Canonicalization is for path identity and watcher deduplication. It does not
  replace `resolve_within_root` or its symlink-confinement checks.
- Focused tests are in `backend/tests/unit/test_native_home_paths.py`,
  `backend/tests/unit/test_deepspace_shared_skills.py`, and
  `applications/desktop/tests/home-paths.test.cjs`.
