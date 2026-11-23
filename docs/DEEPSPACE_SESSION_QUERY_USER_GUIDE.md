# DeepSpace Session Query — User Guide

**Date:** 2026-11-19

---

## What It Is

**Session Query** lets you search, read, and trace your conversation history. Every turn, tool call, and delegation is recorded as a typed event — this is the read side of that log.

**Use it when:**
- You need to find a conversation from last week ("Find conversations about docker")
- You want to replay what happened in a conversation ("Show me the timeline")
- You need to debug a tool call ("Trace event #42")

---

## What You See in the UI

| Location | What It Does |
|---|---|
| **Chat** | Ask naturally: *"Find conversations about docker"* |
| **Side Panel → Trajectory** | Visual timeline of the conversation |

---

## How to Use It (Natural Language)

| You Say | Agent Does |
|---|---|
| *"Find conversations about docker"* | `session_search(query="docker")` — searches all your conversations |
| *"Search this conversation for 'timeout'"* | `session_event_search(conversation_id="...", query="timeout")` |
| *"Show me the timeline of this conversation"* | `session_trace(conversation_id="...")` |
| *"Read event #42 with context"* | `session_event_read(conversation_id="...", seq=42)` |
| *"Trace event #42"* | `session_event_trace(conversation_id="...", seq=42)` |

---

## Tools

### `session_search` — Cross-Conversation Search

Searches **all** your conversations for a text.

**Arguments:** `query` (1–200 chars), optional `limit` (1–50, default 10)

**Returns:** Conversations ranked by recency + relevance, with excerpt from strongest matching event.

### `session_event_search` — Within-Conversation Search

Searches events in one conversation.

**Arguments:** `conversation_id`, `query`, optional `limit` (1–100, default 20)

**Returns:** Matching events in sequence order with excerpt around each match.

### `session_event_read` — Read One Event

Reads one event with its neighbors.

**Arguments:** `conversation_id`, `seq` (event sequence number)

**Returns:** The event plus context (before/after events).

### `session_trace` — Conversation Timeline

Turn-by-turn view of a conversation.

**Arguments:** `conversation_id`

**Returns:** Turn count, message count, and turn-by-turn view with user/assistant messages and tool calls merged into each turn.

### `session_event_trace` — Event Lineage

Shows the turn/step hierarchy for one event.

**Arguments:** `conversation_id`, `seq`

**Returns:** Target event's turn, step, ancestors, and all sibling events in the same step.

---

## Tool Observations (Automatic)

When tools run, the system automatically extracts **structured observations**:

| Field | What It Shows |
|---|---|
| `tool_name` | Which tool ran |
| `status` | called / completed / failed |
| `input_summary` | One-line summary of arguments |
| `output_summary` | One-line summary of result |
| `duration_ms` | How long it took |
| `error` | Error message if failed |

**Example:**
```
bash: cmd: npm test → completed in 3,241ms
web_search: search: redis config → 12 results in 842ms
subagent: delegated: research competitors → completed in 12,300ms
```

These are **automatically available** to the agent — no action needed.

---

## Export (API Only)

Export is not a tool; use the API directly:

| Format | Endpoint | What You Get |
|---|---|---|
| **HTML** | `GET /api/v1/deepspace/chats/{id}/export?format=html` | Styled with CSS — turns, tool calls, observations, event timeline |
| **Markdown** | `GET /api/v1/deepspace/chats/{id}/export?format=markdown` | Plain text for notes/docs |
| **ZIP Archive** | `GET /api/v1/deepspace/chats/{id}/export?format=zip` | HTML + JSON + README in one file |

---

## Cold-Read API (Debug/Advanced)

Direct, low-level access to raw event store:

| Capability | What It Does |
|---|---|
| Filter by event type | `event_types=["tool_call", "tool_result"]` |
| Filter by sequence range | `seq_range=(100, 200)` |
| Filter by time range | `time_range=(start, end)` |
| Pagination | `limit=100, offset=50` |
| Count only | `count_events(...)` |
| Stats | `get_event_stats(...)` — totals, by type, top tools |

---

## Configuration (Admin)

| Setting | Default | Description |
|---|---|---|
| `deepspace_session_log_encryption` | `false` | Encrypt payloads (disables SQL search) |
| `MAX_SESSION_RESULTS` | 50 | Max conversations per search |
| `MAX_EVENT_RESULTS` | 100 | Max events per search |
| `MAX_TRACE_EVENTS` | 2000 | Max events in trace |
| `EXCERPT_CHARS` | 240 | Excerpt length |

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| No search results | Query too specific / no matches | Try broader terms |
| Search slow | FTS5 not populated | Run `populate_fts5(db)` |
| "Conversation not found" | Wrong ID or tenant | Check conversation ID |
| Export fails | Missing permissions | Need `queries:run` |
| No observations | No tool calls in conversation | Normal for chat-only |

---

## Technical Details (If You're Curious)

- **FTS5 Virtual Table** — `deepspace_session_event_fts5` with triggers for auto-indexing
- **Event Model** — `DeepSpaceSessionEvent` (SQLAlchemy + Pydantic)
- **Search** — Case-insensitive `LIKE` + FTS5 `MATCH` (when enabled)
- **Lineage** — Turn/step boundaries detected via `TURN_START/END`, `STEP_START/END`
- **Observations** — Matched TOOL_CALL ↔ TOOL_RESULT by `call_seq`
- **Export** — HTML with embedded CSS, Markdown fallback, ZIP with JSON

---

## Related Documentation

| Topic | Document |
|---|---|
| Tool catalogue | `backend/docs/deepspace/11-deepspace-native-tool-surface.md` |
| Session Events Model | `backend/app/deepspace/models/session_event.py` |
| Trajectory Panel | `frontend/app/dashboard/deepspace/_components/DeepSpaceSidePanel.tsx` |
| In-app help | `frontend/app/documentation/deepspace/page.tsx` |

---

## TL;DR

Say "find X", "search Y", "trace Z". The agent uses `session_search`, `session_event_search`, `session_event_read`, `session_trace`, `session_event_trace`. Check **Trajectory** panel for visual timeline. Use **Export API** for records.