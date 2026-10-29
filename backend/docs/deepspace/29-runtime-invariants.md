# 29. Runtime Invariant Registry

DeepSpace provides a declarative runtime-invariant registry. The chat service
evaluates its enabled built-ins after a turn has measured its tool and provider
work, and emits an `invariant` stream frame for each failed check. The registry
also exposes programmatic `check_all()` and `check_one()` methods; it is not a
standalone scheduler or alerting service.

## Use case

- Operators need guardrails that report selected misconfigurations or resource exhaustion after a turn
- Developers need a framework to express and enforce system invariants (budgets, limits, integrity)
- Callers can run invariants on demand; the chat integration runs them once at the end of a turn

## Why it matters

- Previously, limits were enforced ad-hoc in code with no central registry
- Invariants are first-class: registered, enabled/disabled, tagged, and executable
- `check_all()` supports optional tag filtering, and `check_one()` runs one enabled invariant
- The registry returns structured results with severity, message, and details; the chat stream reports failures only

## How it works

### Architecture

1. **Configuration**: `deepspace_invariant_enabled=true` plus per-invariant limits
2. **Registry**: `InvariantRegistry` holds all invariants, tracks enabled/disabled state
3. **Invariants**: Each implements `Invariant.check(context) -> InvariantResult`
4. **Execution**: the chat service calls `check_all()` after the turn; other callers may invoke it directly
5. **Results**: `InvariantResult` contains pass/fail, severity, message, details, duration, and timestamp

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_invariant_enabled` | bool | `true` | Chat-service switch for reporting invariant failures after a turn |
| `deepspace_invariant_tool_budget` | int | `50` | Max tool calls per turn |
| `deepspace_invariant_provider_latency_ms` | int | `120000` | Max provider latency |
| `deepspace_invariant_max_turns` | int | `1000` | Max turns per conversation |
| `deepspace_invariant_subagent_depth` | int | `16` | Max delegation depth |
| `deepspace_invariant_max_memory_mb` | int | `2048` | Process memory limit |
| `deepspace_invariant_min_disk_gb` | float | `1.0` | Minimum free disk space |

### Built-in invariants

| Invariant | Tags | Default severity | Current chat context |
|-----------|------|-----------------|---------------------|
| `tool_call_budget` | budget, tools | error | Measured from the turn's tool-call count |
| `provider_latency` | latency, provider | warning | Measured from the turn's provider latency |
| `conversation_turn_limit` | turns, limits | error | Measured from the turn's round index |
| `session_event_sequence` | events, integrity | error | Registry-only unless a caller supplies event sequences |
| `subagent_depth` | subagents, delegation | error | Registry-only unless a caller supplies depth |
| `workspace_path` | workspace, security | error | Registry-only unless a caller supplies violations |
| `memory_usage` | memory, resources | warning | Measures the current process RSS |
| `disk_space` | disk, resources | error | Measures free space on `/` |

### Custom invariants

```python
class CustomInvariant(Invariant):
    name = "custom_check"
    severity = "warning"
    description = "My custom check"
    tags = ("custom",)

    async def check(self, context: InvariantContext) -> InvariantResult:
        # Your logic here
        return InvariantResult(
            name=self.name,
            passed=True,
            message="All good",
        )

registry = get_invariant_registry(settings)
registry.register(CustomInvariant())
```

### Execution

```python
registry = get_invariant_registry(settings)

# Check all enabled invariants when a caller has assembled a context
results = await registry.check_all(context)

# Check invariants with specific tags
results = await registry.check_all(context, tags=("budget", "tools"))

# Check single invariant
result = await registry.check_one("tool_call_budget", context)
```

### Result structure

```python
@dataclass
class InvariantResult:
    name: str
    passed: bool
    message: str
    details: dict
    duration_ms: float
    severity: Literal["error", "warning", "info"]
    timestamp: datetime
```

## What to watch for

- **Execution point**: The chat service runs the checks after the measured turn, and a diagnostic failure does not fail an otherwise completed turn.
- **Side effects**: Invariants must be pure reads; never mutate state
- **Context**: Provide minimal context; invariants should not need heavy DB queries
- **Alerting**: No alerting integration is provided by this registry; deployments must consume the results or stream frames themselves.
- **Tags**: Use tags to group invariants for selective execution. The chat integration currently runs all enabled invariants.
- **Custom invariants**: Register them programmatically; they are not loaded from the web settings page or a deployment manifest.
- **Rollback**: Invariants never rollback; they only report. The turn loop decides action.
