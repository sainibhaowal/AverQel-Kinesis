# 52. Storage domains

DeepSpace starts a persistent storage hub for components that need a small,
versioned key-value domain. It is a component-storage facility, not a second
authoritative store for users, conversations, permissions, or other records
already owned by the application database.

## Use case

A feature module needs durable key-value records with a declared version and
named tables, without inventing a private file format or opening its own
SQLite connection.

## Why it matters

In-memory backends lose state at restart, and temporary directories make a
feature appear to work while silently discarding its data. A shared runtime
also needs one clear owner for opening and closing its storage resources.

## How it works

At API startup, `DeepSpaceStorageRuntime` registers two persistent backends:

- `sqlite`, stored at `AVERQEL_HOME/db/deepspace-kv.sqlite3`, is the default
  route for domains.
- `json`, stored under `AVERQEL_HOME/storage/deepspace-kv`, is available when
  a component explicitly routes a domain to it.

The runtime mounts `DomainFacility` as `app.state.deepspace_storage.domain`.
A component declares its own `DomainSpec` and opens it through that facility;
the resulting domain exposes named tables with asynchronous writes and emits
`domain/changed` events through its context when one is provided. Writes are
queued per domain, and shutdown waits for those writes before closing domains
and their backends. A recoverable write error does not block later writes; an
incompatible/unknown storage state marks that domain unreadable until it is
reopened. Storage files are created with owner-only permissions by the SQLite
and atomic-JSON backends.

The live `deepspace_terminal_preferences` domain stores only a user's default
shell and terminal dimensions. `TerminalPreferencesStore` derives each record
key from the authenticated tenant and user; API callers cannot choose a key or
read another identity's record. The API rejects unavailable shells and invalid
dimensions. Terminal session ownership, workspace paths, commands, and output
remain outside this domain. If the storage hub is unavailable, the preference
read/write endpoints return an unavailable response, while terminal creation
uses built-in defaults.

Example:

```python
spec = define_domain(
    DomainSpec(name="plugin_cache", version=1, tables={"entries": domain_table("json")})
)
domain = await request.app.state.deepspace_storage.domain.open(spec)
await domain.table("entries").put("latest", {"etag": "abc"})
```

## What to watch for

- Use stable domain and table names that match the lowercase identifier
  contract. Increment the domain version when its stored shape changes, and
  define compatible versions only when the code can actually read them.
- Do not copy application database records or secrets into this KV store. The
  application's SQL database remains authoritative for tenant-scoped and
  relational data.
- The hub is infrastructure exposed for feature domains; it does not migrate
  existing feature data into KV storage or replace the existing SQL models.
- The isolated storage tests verify persistence, tenant/user key isolation,
  input validation, and application of saved terminal defaults. The measured
  storage scope passed **61 tests with 100% statement and branch coverage**
  (363 statements and 80 branches). These tests do not cover every disk-failure
  mode or host filesystem.
