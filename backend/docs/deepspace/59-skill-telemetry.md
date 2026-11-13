# 59. Skill load telemetry

Every skill operation emits a structured telemetry event wired into the existing
session telemetry pipeline.  This enables observability: which skills are used,
how long they take to load, and which fail.

## Use case

- Identify dead skills (never loaded) for cleanup.
- Measure catalog size impact on turn latency.
- Detect skill load failures (missing files, permission errors).
- Correlate skill usage with task outcomes.

## Why it matters

- You cannot tune a catalog you never measure.
- Detects when a skill silently disappears (file deleted, provider failed).
- Feeds into session telemetry for end-to-end turn analysis.

## How it works

### Events emitted (`skill_telemetry.py`)

`record_skill_event(event, conversation_id, settings)` emits:

```python
@dataclass(frozen=True, slots=True)
class SkillTelemetryEvent:
    action: Literal["load", "list", "resource", "validate", "register", "remove"]
    name: str
    provider: str = ""
    source: str = ""
    outcome: Literal["ok", "not_found", "error", "invalid"] = "ok"
    duration_ms: float = 0.0
```

### Session telemetry integration

When `deepspace_skill_telemetry_enabled=True` (default):

1. `record_skill_event` increments in-process counters.
2. Calls `session_telemetry.get_session_telemetry(settings).record_session_event()` with:
   - `conversation_id`
   - Event dict: `{ "type": "skill_load", "tool": "skill-name", ... }`
   - **No skill instructions/content** in the event (privacy boundary).

### Counters (in-process)

```python
skill_counters()  # -> {
    "load:ok": 42,
    "load:not_found": 3,
    "load:error": 1,
    "list:ok": 15,
    "resource:ok": 8,
    "resource:error": 2,
    "validate:ok": 5,
    "validate:invalid": 1,
    "register:ok": 2,
    "remove:ok": 1,
    "provider:filesystem": 30,
    "provider:bundled": 12,
    "provider:remote": 0,
}
```

### Tools that emit events

| Tool | Actions |
|---|---|
| `skill_load` | `load:ok` / `load:not_found` |
| `skill_list` | `list:ok` |
| `skill_resource` | `resource:ok` / `resource:error` |
| `skill_validate` | `validate:ok` / `validate:invalid` |
| `RuntimeSkillProvider.register_runtime_skill()` | `register:ok` |
| `RuntimeSkillProvider.remove()` | `remove:ok` |

### Disabling

`deepspace_skill_telemetry_enabled: false` disables event emission entirely (counters still increment for in-process visibility).

## What to watch for

- Events carry **no skill content** — only name, provider, source, outcome, duration.
- Export failures (session telemetry unavailable) never break the caller; counters still increment.
- Disabled telemetry (`deepspace_skill_telemetry_enabled=false`) never reaches the session telemetry service.
- Counters are in-process only; they reset on process restart.

## Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_skill_telemetry_enabled` | `bool` | `true` | Emits structured skill events to session telemetry. |

## Related chapters

- [Session Telemetry + OTel](28-session-telemetry.md) — OpenTelemetry spans for turns, tools, providers.
- [Bundled Skills](33-bundled-skills.md) — skill discovery, providers, and Office workflows.
- [The Tool Catalogue](09-the-tool-catalogue.md) — `skill_list`, `skill_load`, `skill_resource`, `skill_validate`.
