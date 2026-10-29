# 34. Session Format Migration Chain

DeepSpace contains a versioned export-payload migration chain (v1 → v2 → v3
→ v4). The current chain is used by the session-log export path; it is not a
general migration hook on every session-event read and it is not the database
schema migration system.

## Use case

- An exported event payload may need to be upgraded to the current envelope
- Export consumers need a stable way to request JSONL or JSON payloads
- Developers need explicit, testable transformation steps

## Why it matters

- Previously, `SESSION_FORMAT_VERSION = 1` with no migration logic
- Now a chain of explicit migrations handles schema evolution
- The export service applies the chain to the serialized event payload
- The original database row is not rewritten

## How it works

### Architecture

1. **Configuration**: `deepspace_format_migration_enabled=true` plus current export version
2. **Migrations**: Each step is a `SessionFormatMigration` subclass with `migrate()` and `rollback()`
3. **Chain**: `SessionFormatMigrationChain` applies migrations sequentially
4. **Export integration**: the session-export route applies the chain before writing each output event

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_format_migration_enabled` | bool | `true` | Master switch |
| `deepspace_format_current_version` | int | `4` | Current format version |
| `deepspace_format_auto_migrate` | bool | `true` | Allow the export path to apply migrations |
| `deepspace_format_backup` | bool | `true` | Declared backup preference; no backup operation is currently implemented |

### Migration chain

| Version | Migration | Description |
|---------|-----------|-------------|
| 1 → 2 | `V1ToV2Migration` | Add `subagent_id` field for delegation tracking |
| 2 → 3 | `V2ToV3Migration` | Add `run_id` for run tracking, normalize payload |
| 3 → 4 | `V3ToV4Migration` | Add `step` field, separate tool call/result payloads |

### Migration implementation

```python
class V1ToV2Migration(SessionFormatMigration):
    from_version = 1
    to_version = 2
    description = "Add subagent_id field"

    def migrate(self, event: dict) -> dict:
        event = dict(event)
        event.setdefault("subagent_id", None)
        event["format_version"] = self.to_version
        return event

    def rollback(self, event: dict) -> dict:
        event = dict(event)
        event.pop("subagent_id", None)
        event["format_version"] = self.from_version
        return event
```

### Chain usage

```python
chain = get_format_migration_chain(settings)

# Migrate one serialized event to the configured version
migrated = chain.migrate_event(event)

# Migrate list of events
migrated = chain.migrate_events(events)

# Roll back one serialized event in memory
rolled_back = chain.rollback_event(event, target_version=2)

# Get chain info
print(chain.get_current_version())  # 4
print(chain.get_migration(2))  # V2ToV3Migration
```

### Export-time migration

When `deepspace_format_auto_migrate=true` (default) and session export is
enabled:

1. The export route fetches events from the database
2. Each serialized event is checked for `format_version` (missing means v1)
3. If < current, `chain.migrate_event()` applied
4. The migrated event is streamed to the caller
5. The original event in the database is unchanged

### Backwards compatibility

- Serialized events without `format_version` are treated as v1 by the chain
- The checked-in migrations are intended to be safe to apply more than once; verify that property when adding a new step
- Rollback is available as an in-memory debugging helper

## What to watch for

- **Data integrity**: Migrations are read-time only; DB never rewritten automatically
- **Performance**: Migration runs for every exported event; keep migrations fast
- **Idempotence**: Migrations must be idempotent (re-running produces same result)
- **Rollback testing**: Rollback is an in-memory programmatic helper; it is not an automatic database operation.
- **New migrations**: Add to `_migrations` list in order; bump `current_version`
- **Schema validation**: Consider adding JSON Schema validation per version
- **Large event sets**: Batch migration for bulk operations; avoid N+1 queries
