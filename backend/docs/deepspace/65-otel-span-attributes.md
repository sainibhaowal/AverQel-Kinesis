# 65. Session Telemetry — OTel Span Attributes and Custom Exporters

The `session_telemetry.py` module emits OpenTelemetry spans for every turn,
tool call, provider request, and skill operation.  This chapter documents the
exact span attributes, event names, and how to configure custom exporters.

## Use case

- An operator wants to trace a slow turn across provider, tool, and skill boundaries.
- A deployment integrates with an existing OTel collector (Jaeger, Zipkin, Tempo).
- Custom dashboards correlate skill load latency with turn outcome.

## Why it matters

- Structured spans replace ad-hoc logging for distributed tracing.
- Span attributes are stable identifiers for querying and alerting.
- Custom exporters allow integration without code changes.

## How it works

### Span hierarchy

```
Turn (deepspace.turn)
├── Provider Request (deepspace.provider.request)
│   ├── Tool Call (deepspace.tool.call)  [per tool]
│   │   ├── Tool Result (deepspace.tool.result)
│   │   └── Skill Load (deepspace.skill.load)  [if skill tool]
│   └── Provider Response (deepspace.provider.response)
└── Turn Settled (deepspace.turn.settled)
```

### Turn span (`deepspace.turn`)

| Attribute | Type | Description |
|---|---|---|
| `deepspace.turn.id` | string | Turn UUID |
| `deepspace.conversation.id` | string | Conversation UUID |
| `deepspace.tenant.id` | string | Tenant UUID |
| `deepspace.user.id` | string | User UUID |
| `deepspace.agent.depth` | int | Delegation depth (0=root) |
| `deepspace.agent.preset` | string | Active preset ID |
| `deepspace.agent.model` | string | Provider/model in use |
| `deepspace.turn.prompt_chars` | int | User prompt character count |
| `deepspace.turn.tools_planned` | int | Tools the model requested |
| `deepspace.turn.duration_ms` | int | Wall-clock turn duration |

### Provider request span (`deepspace.provider.request`)

| Attribute | Type | Description |
|---|---|---|
| `deepspace.provider.name` | string | Provider identifier |
| `deepspace.provider.model` | string | Model identifier |
| `deepspace.provider.request_chars` | int | Request payload size |
| `deepspace.provider.tools_available` | int | Tools offered to model |

### Tool call span (`deepspace.tool.call`)

| Attribute | Type | Description |
|---|---|---|
| `deepspace.tool.name` | string | Tool identifier |
| `deepspace.tool.call_id` | string | Unique call UUID |
| `deepspace.tool.mode` | string | `read` \| `write` \| `interactive` |
| `deepspace.tool.risk_level` | string | `read` \| `write` \| `broad` \| `destructive` |
| `deepspace.tool.args_chars` | int | Arguments JSON size |
| `deepspace.tool.depth` | int | Delegation depth of caller |

### Tool result span (`deepspace.tool.result`)

| Attribute | Type | Description |
|---|---|---|
| `deepspace.tool.name` | string | Tool identifier |
| `deepspace.tool.call_id` | string | Matching call ID |
| `deepspace.tool.status` | string | `ok` \| `error` \| `denied` \| `capacity` \| `spilled` |
| `deepspace.tool.duration_ms` | int | Tool execution time |
| `deepspace.tool.result_chars` | int | Result payload size |
| `deepspace.tool.error_code` | string | `TOOL_TIMEOUT`, `FS_STALE_VERSION`, etc. |

### Skill operation span (`deepspace.skill.load` / `list` / `resource` / `validate`)

| Attribute | Type | Description |
|---|---|---|
| `deepspace.skill.name` | string | Skill name |
| `deepspace.skill.action` | string | `load` \| `list` \| `resource` \| `validate` |
| `deepspace.skill.provider` | string | `filesystem` \| `bundled` \| `remote` \| `runtime` |
| `deepspace.skill.source` | string | `project` \| `user` \| `custom` \| `bundled` \| `remote` |
| `deepspace.skill.outcome` | string | `ok` \| `not_found` \| `error` \| `invalid` |
| `deepspace.skill.duration_ms` | int | Operation latency |

### Events (within spans)

| Event name | Span | Fields |
|---|---|---|
| `deepspace.turn.start` | turn | `{ prompt_preview: string }` |
| `deepspace.provider.stream_chunk` | provider.request | `{ chars: int }` |
| `deepspace.tool.approval_requested` | tool.call | `{ tool: string, reason: string }` |
| `deepspace.skill.catalog_injected` | turn | `{ entries: int, truncated: bool, incomplete: bool }` |
| `deepspace.turn.settled` | turn | `{ stop_reason: string, tools_executed: int }` |

### Custom exporter configuration

The telemetry service is initialized in `session_telemetry.py:init_telemetry()`:

```python
def init_telemetry(settings: Settings) -> SessionTelemetry:
    # 1. Create TracerProvider
    provider = TracerProvider(resource=Resource.create({
        "service.name": "deepspace",
        "deployment.environment": settings.deployment_environment,
    }))

    # 2. Add exporters based on settings
    if settings.otel_exporter_otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
        ))
    if settings.otel_exporter_console:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    # 3. Set global provider
    trace.set_tracer_provider(provider)
    return SessionTelemetry(tracer=provider.get_tracer("deepspace"))
```

### Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `otel_exporter_otlp_endpoint` | `str` | `""` | OTLP/gRPC endpoint (e.g., `http://localhost:4317`). |
| `otel_exporter_otlp_headers` | `str` | `""` | Headers as `k1=v1,k2=v2`. |
| `otel_exporter_console` | `bool` | `false` | Also export to stdout. |
| `otel_sampler` | `str` | `"parentbased_traceidratio"` | Sampler: `always_on`, `always_off`, `traceidratio`, `parentbased_traceidratio`. |
| `otel_sampler_ratio` | `float` | `1.0` | Ratio for traceidratio samplers. |

### Adding custom attributes

Instrumentation code can add attributes to the current span:

```python
from opentelemetry import trace

span = trace.get_current_span()
if span.is_recording():
    span.set_attribute("deepspace.custom.my_key", "value")
```

## What to watch for

- **Span sampling**: With `parentbased_traceidratio` at ratio 1.0, all spans are exported.  Reduce ratio in high-volume deployments.
- **Attribute cardinality**: Avoid high-cardinality values (UUIDs, raw text) as attributes; use events for variable data.
- **Export batching**: `BatchSpanProcessor` buffers spans; configure `schedule_delay_millis` for latency vs. throughput tradeoff.
- **Skill content never in spans**: Skill instructions and resource content are excluded from telemetry (privacy boundary).

## Related chapters

- [Session Telemetry + OTel](28-session-telemetry.md) — overview and setup.
- [Skill Load Telemetry](59-skill-telemetry.md) — skill-specific events.
- [Operations and Troubleshooting](24-operations-and-troubleshooting.md) — debugging with traces.
