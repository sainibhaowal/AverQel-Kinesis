# 68. Storage Domain API — JSON vs SQLite, Migration, and Component Ownership

The storage domain (`storage_domain.py`) provides a unified facility for
mounting isolated key-value and global stores per component.  It supports
both JSON (file-per-record) and SQLite backends with atomic writes and
event notifications.

## Use case

- A component needs durable key-value storage with atomic updates.
- Multiple components share a storage root but must not see each other's data.
- A deployment migrates from JSON to SQLite for performance.

## Why it matters

- Component isolation prevents accidental data corruption.
- Atomic writes (JSON) and transactions (SQLite) guarantee consistency.
- Event notifications enable reactive invalidation (e.g., workspace registry).

## How it works

### Domain facility (`storage_domain.py`)

```python
class StorageDomain:
    def open(spec: DomainSpec) -> Domain:
        # Returns a Domain with:
        # - table(name) -> KvTable[K, V]
        # - global -> DomainGlobal[State]
        # - close() -> cleanup

class DomainSpec:
    root: Path                    # Storage root directory
    backend: "json" | "sqlite"    # Backend selection
    name: str                     # Domain name (component ownership)
```

### JSON backend (`storage_json.py`)

**File layout:**
```
<root>/
├── <domain_name>/
│   ├── tables/
│   │   ├── <table_name>/
│   │   │   ├── <key>.json        # One file per record
│   │   │   └── ...
│   │   └── ...
│   ├── globals/
│   │   ├── <global_name>.json    # One file per global
│   │   └── ...
│   └── events/
│       └── <table_name>.log      # Append-only event log
```

**Atomic write protocol:**
```python
def put(key: K, value: V) -> None:
    # 1. Write to temp file
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(orjson.dumps(value))
    # 2. fsync
    tmp.flush(); os.fsync(tmp.fileno())
    # 3. Atomic rename
    tmp.replace(path)
    # 4. Append to event log
    log.append({"op": "put", "key": key, "ts": now()})
```

**Per-record units:** Each record is a standalone JSON file.  Supports
`get`, `put`, `delete`, `scan(prefix)`, `count()`.

**Event log:** Append-only JSONL for invalidation listeners.  Each entry:
```json
{ "op": "put|delete", "key": "...", "ts": "2026-01-01T00:00:00Z" }
```

### SQLite backend (`storage_sqlite.py`)

**Schema:**
```sql
CREATE TABLE kv_<table_name> (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL,
    updated_at INTEGER NOT NULL  -- unix ms
);

CREATE TABLE globals (
    name TEXT PRIMARY KEY,
    value BLOB NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    op TEXT NOT NULL,        -- put, delete
    key TEXT NOT NULL,
    ts INTEGER NOT NULL
);
```

**WAL mode:** Enabled by default for concurrent readers.

**Transactions:** `table.transaction(fn)` provides ACID for multi-key ops.

### Global records (`DomainGlobal`)

```python
class DomainGlobal:
    def get() -> State
    def set(state: State) -> None
    def update(fn: Callable[[State], State]) -> None  # atomic read-modify-write
```

Used for singleton state (e.g., `WorkspaceDomainState`).

### Component ownership

Each domain is named after its owning component:

| Domain name | Owner | Tables | Globals |
|---|---|---|---|
| `workspace` | WorkspaceRegistry | `workspaces` | `WorkspaceDomainState` |
| `session` | SessionStore | `sessions`, `events` | `SessionMeta` |
| `spill` | SpillStore | `spills` | `SpillIndex` |
| `subagent` | SubagentManager | `subagents` | — |

### Migration: JSON → SQLite

```python
async def migrate_json_to_sqlite(json_root: Path, sqlite_root: Path, domain_name: str):
    json_domain = StorageDomain.open(DomainSpec(root=json_root, backend="json", name=domain_name))
    sqlite_domain = StorageDomain.open(DomainSpec(root=sqlite_root, backend="sqlite", name=domain_name))

    for table_name in json_domain.list_tables():
        json_table = json_domain.table(table_name)
        sqlite_table = sqlite_domain.table(table_name)

        async with sqlite_table.transaction() as tx:
            for key in json_table.scan(""):
                value = json_table.get(key)
                if value is not None:
                    tx.put(key, value)

        # Replay events for consistency
        for event in json_domain.read_events(table_name):
            sqlite_domain.append_event(event)
```

### Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_storage_backend` | `str` | `"json"` | `"json"` or `"sqlite"` per domain. |
| `deepspace_storage_root` | `str` | `"<app_home>/storage"` | Root directory for all domains. |
| `deepspace_storage_sqlite_wal` | `bool` | `true` | Enable WAL mode for SQLite. |

## What to watch for

- **JSON backend scales poorly** beyond ~10,000 records per table (filesystem inode limits, scan latency).  Migrate to SQLite for large tables.
- **SQLite WAL mode** allows concurrent readers but only one writer.  High-write components should batch updates.
- **Event log replay** on startup ensures listeners don't miss updates during restart.
- **Cross-domain references** are not enforced; use UUIDs as foreign keys.
- **Backup**: JSON backend = copy directory tree.  SQLite = `.backup()` API or VACUUM INTO.

## Related chapters

- [Storage Domains](52-storage-domains.md) — overview and mounting.
- [Workspace Registry Internals](62-workspace-registry-internals.md) — workspace domain usage.
- [Operations and Troubleshooting](24-operations-and-troubleshooting.md) — backup/recovery.
