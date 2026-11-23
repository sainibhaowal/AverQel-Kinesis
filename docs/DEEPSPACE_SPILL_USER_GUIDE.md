# DeepSpace Spill (Large Output Handling) — User Guide

**Date:** 2026-11-19

---

## What It Is

**Spill** is what happens when a tool result is too large to carry in the model's context. Instead of silently truncating, DeepSpace writes the complete result to durable, content-addressed storage and gives the agent a **spill reference** plus a bounded preview.

**You do not trigger it manually.** The runtime decides from the result size, and the agent can read the full result back with `spill_read`.

---

## What You See

| Situation | What Happens |
|---|---|
| A command produces a result above the threshold | The model receives `status: "spilled"`, a preview, `spill_ref`, and `size_bytes` |
| The agent needs more | It calls `spill_read` with the `spill_ref` |
| You look at the chat | You see the preview and the spill notice |

A spilled result looks like this:

```json
{
  "status": "spilled",
  "tool_name": "bash",
  "spill_ref": "9f2c...64 hex characters...",
  "size_bytes": 1234567,
  "preview": "first lines ... last lines",
  "preview_truncated": true,
  "omitted_bytes": 1200000,
  "message": "The complete result is stored. Read it with spill_read using the spill_ref, or use the preview above when the excerpt is enough."
}
```

---

## How It Works (Automatic)

1. A tool returns a result.
2. DeepSpace serializes it and compares the size with `deepspace_spill_threshold_chars` (default **24,000 characters**).
3. At or below the threshold, the result is returned unchanged.
4. Above it, the serialized result is written to the spill store and the model receives the preview and reference.
5. A result above `deepspace_spill_max_chars` (default **2,000,000**) is refused rather than truncated.

The preview is an ordered head-and-tail excerpt fitted to a 1,024-token budget. Image blocks inside a structured result stay whole; the runtime records `omitted_bytes` and `omitted_images`.

---

## Retrieving Spilled Output

```
User: "Show me the full build log"
Agent: spill_read(spill_ref="9f2c...")            → first 200,000 characters
Agent: spill_read(spill_ref="9f2c...", offset=200000) → the next page
```

`spill_read` options:

| Option | Default | Meaning |
|---|---|---|
| `spill_ref` | required | The 64-character SHA-256 reference |
| `offset` | 0 | Character offset to start at (max 5,000,000) |
| `max_characters` | 200,000 | Page size (100–200,000) |
| `include_images` | `true` | Recover supported images embedded in the result |

`spill_session` is a related tool: it saves a readable reference to the current conversation history, which is then read with `spill_read`.

---

## Where Spilled Data Lives

- **Tenant-scoped, content-addressed storage**: key `{tenant_id}/spill/{sha256}`
- Two backends selected by `deepspace_spill_backend`:
  - `storage` (default) — the deployment's object storage / local MinIO root
  - `local` — a private local directory under `deepspace_spill_local_root`
- Identical payloads deduplicate to the same key
- Local writes are atomic (temp file then rename)
- Expired spills are swept periodically on a best-effort basis; a cleanup failure never fails a tool result

---

## What Triggers Spill

Spill is decided **per tool result**, not per output stream. Any native tool whose serialized result crosses the threshold is spilled, including shell output, large file reads, search results, SSH output, and subagent output.

Terminal reading is separate: `terminal_read` returns at most `deepspace_terminal_max_output_bytes` (256 KiB by default) from the session buffer, and `job_output` caps its own text. Those bounds apply before the result reaches the spill policy.

---

## Configuration (Admin)

| Setting | Default | Description |
|---|---|---|
| `deepspace_spill_threshold_chars` | `24000` | Character count above which a result spills |
| `deepspace_spill_max_chars` | `2000000` | Ceiling above which a result is refused |
| `deepspace_spill_backend` | `storage` | `storage` or `local` |
| `deepspace_spill_local_root` | empty | Required when the backend is `local` |
| `deepspace_spill_retention_seconds` | `7776000` (90 days) | Age after which the sweep may delete a spill |

There is no `spill_enabled` switch and no byte-based buffer setting; the threshold is in characters.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| *"The spilled result exceeds the deployment's spill ceiling"* | Result is larger than `deepspace_spill_max_chars` | Raise the ceiling deliberately, or narrow the command |
| *"Spill reference not found"* | Wrong reference, expired retention, or another tenant | Use the exact 64-character `spill_ref` from the spilled result |
| *Agent says "output truncated"* | The preview is bounded; the full result is still stored | Ask the agent to page through `spill_read` |
| *Spill read fails* | Storage backend or local root is unavailable | Check `deepspace_spill_backend`, the object store, and `deepspace_spill_local_root` |
| *Nothing spills* | Result is under the threshold | Raise the command's output or lower the threshold |

---

## Technical Details (If You're Curious)

- **`spill_result()`** — threshold check, size ceiling, store write, preview build
- **`result_preview()`** — head/tail token budget, image-block retention
- **`SpillStore`** — chooses `ObjectStorageSpillBackend` or `LocalFileSpillBackend`
- **`SpillRef`** — SHA-256, byte size, tenant
- **Cleanup** — periodic `cleanup_expired(older_than=now - retention)`, best effort
- **Tenant isolation** — every key starts with the tenant id, and reads are scoped to the caller's tenant

---

## Related Documentation

| Topic | Document |
|---|---|
| Spill policy internals | `backend/app/deepspace/services/spill_policy.py` |
| Spill store | `backend/app/deepspace/services/spill_store.py` |
| Spill tools | `backend/app/deepspace/services/tools/spill.py` |
| Remaining chapters | `backend/docs/deepspace/` |

---

## TL;DR

- A result above **24,000 characters** spills automatically to tenant storage.
- The agent gets a head/tail preview and a 64-character `spill_ref`.
- `spill_read` returns the full result in pages of up to 200,000 characters.
- Spills expire after 90 days by default and are swept periodically.
