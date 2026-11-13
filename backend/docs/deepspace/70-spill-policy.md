# 70. Spill Policy — Retention, Thresholds, and Object Storage

The spill system (`spill.py`, `spill_policy.py`, `spill_store.py`) offloads
large tool results to durable storage, returning a `spill_ref` that the model
can read back with `spill_read`.

## Use case

- A tool returns 5 MB of log output; the context window cannot hold it.
- A workflow produces a large JSON artifact; it must persist across turns.
- A deployment uses tenant object storage (S3/GCS) for spill durability.

## Why it matters

- Prevents context window exhaustion from large tool results.
- Durable storage survives process restarts.
- Object storage backend enables multi-instance deployments.

## How it works

### Spill trigger (`spill_policy.py`)

```python
class SpillPolicy:
    threshold_chars: int = 24000      # deepspace_spill_threshold_chars
    max_chars: int = 2_000_000        # deepspace_spill_max_chars
    backend: "storage" | "local"      # deepspace_spill_backend

    def should_spill(self, result: ToolResult) -> bool:
        return len(result.text) > self.threshold_chars

    def create_spill(self, result: ToolResult) -> SpillRef:
        # 1. Truncate to max_chars
        content = result.text[:self.max_chars]
        # 2. Compute content hash
        digest = sha256(content.encode()).hexdigest()
        # 3. Store
        spill_id = store.put(digest, content)
        # 4. Return reference
        return SpillRef(spill_id=spill_id, digest=digest, size=len(content))
```

### Spill reference (`spill.py`)

```python
@dataclass
class SpillRef:
    spill_id: str        # Content-addressed (sha256) or UUID
    digest: str          # sha256 of content
    size: int            # bytes
    preview: str         # First 500 chars for model context
```

**Tool result with spill:**
```json
{
  "status": "ok",
  "spill_ref": {
    "spill_id": "sha256:abc123...",
    "digest": "abc123...",
    "size": 5242880,
    "preview": "First 500 chars of output..."
  },
  "spilled": true
}
```

### Spill store (`spill_store.py`)

Two backends:

| Backend | Implementation | Durability | Multi-instance |
|---|---|---|---|
| `local` | Filesystem (`<root>/spills/<spill_id>`) | Single machine | ❌ |
| `storage` | Tenant object storage (S3/GCS/Azure) | Distributed | ✅ |

**Local store (`LocalSpillStore`):**
```
<spill_root>/
├── <spill_id>          # Content file
├── <spill_id>.meta     # JSON: { digest, size, created_at, tenant_id }
└── index.sqlite        # Optional: spill_id -> metadata
```

**Storage store (`StorageSpillStore`):**
- Uses `StorageDomain` with `backend="storage"` (S3/GCS).
- Object key: `spills/<tenant_id>/<spill_id>`
- Metadata in object metadata or companion `.meta` object.

### Retention policy (`spill_policy.py:RetentionPolicy`)

```python
class RetentionPolicy:
    retention_seconds: int = 7_776_000  # 90 days (deepspace_spill_retention_seconds)
    cleanup_interval_seconds: int = 3600

    async def cleanup(self, store: SpillStore) -> int:
        cutoff = now() - self.retention_seconds
        deleted = 0
        for spill in store.list_older_than(cutoff):
            if not spill.is_referenced():
                store.delete(spill.spill_id)
                deleted += 1
        return deleted
```

**Reference tracking:**
- `spill_read` increments `access_count` and updates `last_accessed`.
- Active conversation turns hold implicit references (not cleaned until turn settles).
- Background jobs hold references until completion.

### Spill read tool (`spill_read`)

```json
{
  "name": "spill_read",
  "arguments": {
    "spill_ref": "sha256:abc123...",
    "max_characters": 10000,
    "offset": 0
  }
}
```

Returns:
```json
{
  "content": "...",
  "truncated": false,
  "offset": 0,
  "remaining": 0
}
```

### Spill session tool (`spill_session`)

Lists spills for the current conversation:
```json
{
  "name": "spill_session",
  "arguments": {}
}
```

Returns:
```json
{
  "spills": [
    { "spill_id": "...", "size": 5242880, "created_at": "...", "referenced": true }
  ]
}
```

### Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_spill_threshold_chars` | `int` (2000-500000) | `24000` | Result size above which spill triggers. |
| `deepspace_spill_max_chars` | `int` (10000-20000000) | `2000000` | Max spill size; larger results truncated. |
| `deepspace_spill_backend` | `str` | `"storage"` | `"storage"` (object storage) or `"local"` (filesystem). |
| `deepspace_spill_local_root` | `str` | `""` | Local spill root (empty = app private storage). |
| `deepspace_spill_retention_seconds` | `int` (60-315360000) | `7776000` | Retention period (90 days). |

### Object storage configuration (`storage` backend)

The `storage` backend uses the tenant's configured object storage:

```python
# Configured via storage domain
domain = await storage_domain.open(DomainSpec(
    root=Path("/storage"),
    backend="s3",  # or "gcs", "azure"
    name="spill",
))
```

**S3 example:**
```yaml
# Storage domain config
type: s3
bucket: deepspace-spills
region: us-east-1
prefix: spills/
credentials:
  access_key_id: ${AWS_ACCESS_KEY_ID}
  secret_access_key: ${AWS_SECRET_ACCESS_KEY}
```

**GCS example:**
```yaml
type: gcs
bucket: deepspace-spills
prefix: spills/
credentials: ${GOOGLE_APPLICATION_CREDENTIALS}
```

### What to watch for

- **Threshold tuning**: Too low → excessive spills (latency). Too high → context overflow.
- **Retention vs. references**: A spill referenced by an active turn is never deleted.
- **Local backend**: Not suitable for multi-instance deployments (spills on one node invisible to others).
- **Storage backend**: Requires configured tenant object storage; falls back to local if unavailable.
- **Max chars**: Results exceeding `max_chars` are silently truncated; model sees `truncated: true`.
- **Cleanup job**: Runs every `cleanup_interval_seconds`; ensure it doesn't conflict with backup windows.

## Related chapters

- [Operations and Troubleshooting](24-operations-and-troubleshooting.md) — spill debugging.
- [Storage Domains](52-storage-domains.md) — underlying storage facility.
- [The Tool Catalogue](09-the-tool-catalogue.md) — `spill_read`, `spill_session` tools.
