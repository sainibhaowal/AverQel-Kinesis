# 28. Session Telemetry + OpenTelemetry

When enabled, DeepSpace emits structured OpenTelemetry spans for a session,
turns, tools, and provider calls. One sampling decision is made at the
OpenTelemetry provider, so child spans remain consistent with their session.

## Use case

- Operators need to monitor agent performance, latency, and error rates in production
- Developers need distributed traces to debug slow turns or failed tool calls
- SREs need metrics for alerting on degraded agent performance

## Why it matters

- Previously, telemetry existed only at the API level (request latency, error rates)
- Now every instrumented turn, tool call, and provider call emits a span with rich attributes
- OTLP export allows integration with any OTel-compatible backend (Jaeger, Zipkin, Grafana Tempo, Datadog, Honeycomb)
- Console export for development debugging

## How it works

### Architecture

1. **Configuration**: `deepspace_telemetry_enabled=true` plus an OTLP endpoint
2. **Provider**: `SessionTelemetry` initializes OTel SDK with resource attributes
3. **Spans**: The chat loop opens a session-root span and a nested turn span, then records provider and tool spans beneath the turn
4. **Attributes**: Rich semantic attributes on every span (conversation_id, turn, step, tool_name, provider, model, tokens, latency, success/error)
5. **Sampling and export**: `deepspace_telemetry_sample_rate` uses the OTel trace-id ratio sampler; spans export through OTLP gRPC or the console exporter

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_telemetry_enabled` | bool | `false` | Master switch |
| `deepspace_telemetry_otel_endpoint` | str | `null` | OTLP gRPC endpoint (e.g., `http://jaeger:4317`) |
| `deepspace_telemetry_otel_headers` | dict | `{}` | OTLP headers (auth, etc.) |
| `deepspace_telemetry_service_name` | str | `"deepspace"` | OTel service name |
| `deepspace_telemetry_environment` | str | `"production"` | Deployment environment |
| `deepspace_telemetry_sample_rate` | float | `1.0` | Trace sampling rate (0.0-1.0) |
| `deepspace_telemetry_export_console` | bool | `false` | Also export to stdout |
| `deepspace_telemetry_attributes` | dict | `{}` | Static resource attributes |

### Span hierarchy

```
deepspace.session (conversation_id)
  └─ deepspace.turn (turn=N)
     ├─ deepspace.tool.browser_navigate (tool_name, step, success, duration_ms)
     ├─ deepspace.tool.fs_write (tool_name, step, success, duration_ms)
     └─ deepspace.provider.openai (provider, model, input_tokens, output_tokens, latency_ms)
```

### Span attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `deepspace.conversation_id` | string | Conversation UUID |
| `deepspace.turn` | int | Turn number |
| `deepspace.step` | int | Step number |
| `deepspace.tool.name` | string | Tool name |
| `deepspace.tool.success` | bool | Tool success/failure |
| `deepspace.tool.duration_ms` | int | Tool execution time (ms) |
| `deepspace.tool.wall_ms` | int | Wall time including queue wait |
| `deepspace.tool.error` | string | Error message if failed |
| `deepspace.provider` | string | Provider name |
| `deepspace.model` | string | Model name |
| `deepspace.provider.input_tokens` | int | Prompt tokens |
| `deepspace.provider.output_tokens` | int | Completion tokens |
| `deepspace.provider.latency_ms` | int | Provider latency |
| `deepspace.provider.success` | bool | Provider success |

### Usage in code

```python
telemetry = get_session_telemetry(settings)

with telemetry.turn_span(conversation_id, turn=5) as span:
    with telemetry.tool_span(conversation_id, 5, 2, "browser_navigate") as tool_span:
        result = await browser_navigate(...)
        telemetry.record_tool_result(tool_span, success=True, duration_ms=150)
```

## What to watch for

- **Sampling**: The configured rate is validated between `0.0` and `1.0` and applied by the OTel provider. A rate of `1.0` keeps every trace; a rate of `0.0` keeps none.
- **PII**: Never put user prompts or sensitive data in span attributes; use events for structured logs
- **Cardinality**: Avoid high-cardinality attributes (e.g., full prompts) as label values
- **Export latency**: OTLP batch processor buffers spans; configure batch timeout for real-time needs
- **Resource attributes**: Include deployment metadata (region, version, cluster) in `telemetry_attributes`
- **Costs**: OTel backends charge by span volume; sample aggressively in high-volume deployments
- **Development**: Use `export_console=true` for local debugging without a backend
