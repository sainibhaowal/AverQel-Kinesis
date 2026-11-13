# 63. Workflow PTC Runtime

The `workflow-ptc` package provides a sandboxed JavaScript execution environment
for running workflow scripts.  It uses a guest/host architecture where the
workflow script runs in an isolated realm with controlled access to host
capabilities via the `agent`, `pipeline`, `parallel`, and `phase`/`log` hooks.

## Use case

- A workflow script needs to fan out hundreds of subagent calls with controlled
  concurrency and structured progress reporting.
- The execution must be sandboxed: no filesystem, network, timer, or Node.js APIs
  directly accessible to the script.
- The host controls resource limits (agent count, round budget, output size).

## Why it matters

- Workflow scripts are user-supplied code; the guest/host boundary prevents
  privilege escalation.
- The PTC runtime enforces caps (max agents, max rounds, max result chars) at
  the engine level, not just by convention.
- Structured result rendering ensures the model sees a consistent output format.

## How it works

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        HOST (DeepSpace)                         │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  WorkflowEngine (workflow/workflow/src/index.ts)         │   │
│  │  - start(script, meta, args, parent) → WorkflowRun      │   │
│  │  - Manages agent fan-out, pipeline, parallel, phase/log  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                              │                                  │
│                    guest/host boundary                         │
│                              ▼                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Guest Realm (workflow-ptc/src/guest.ts)                │   │
│  │  - Isolated JavaScript context (no Node.js globals)      │   │
│  │  - Host-provided hooks: agent, pipeline, parallel, phase│   │
│  │  - Script runs here, returns JSON-serializable value     │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Guest realm (`workflow-ptc/src/guest.ts`)

The guest is a minimal JavaScript environment created via `vm` (Node.js) or
equivalent sandbox.  It provides:

- **Global hooks** (injected by host):
  - `agent(prompt, opts?)` → `Promise<any>` — runs subagent
  - `pipeline(items, ...stages)` → `Promise<any[]>` — multi-stage fan-out
  - `parallel(thunks)` → `Promise<any[]>` — concurrent execution
  - `phase(title)` — start progress phase
  - `log(message)` — narrate progress
  - `args` — the tool call's `args` input (verbatim)

- **Schema restriction**: `opts.schema` for `agent` only supports a subset of
  JSON Schema: `type`, `properties`, `required`, `additionalProperties`,
  `items`, `enum`, `const`, `oneOf`.  Object root only.

- **No access to**: `require`, `import`, `fs`, `net`, `http`, `timer`, `process`,
  `console`, or any Node.js built-ins.

### Host engine (`workflow/workflow/src/index.ts`)

The `WorkflowEngine` manages the workflow lifecycle:

```typescript
interface WorkflowEngine {
    start(script: string, meta: WorkflowMeta, args?: Record<string, unknown>, parent: Agent): WorkflowRun;
}

interface WorkflowRun {
    id: WorkflowRunId;
    meta: WorkflowMeta;
    result: Promise<WorkflowResult>;
    cancel(reason?: string): void;
    dispose(): Promise<void>;
}
```

**Caps enforced by engine:**

| Cap | Default | Config |
|---|---|---|
| Max agents started | 100 | `meta.maxAgents` |
| Max rounds per agent | 12 | `meta.maxRounds` |
| Max result chars | 50,000 | `maxResultChars` (tool config) |
| Agent timeout | 1800s | `deepspace_agent_max_runtime_seconds` |

### Background execution (`run_in_background: true`)

When `run_in_background` is set:

1. Engine starts the run inside a `ctx.jobs` job starter.
2. Returns `{ kind: "background", jobId, runId }` immediately.
3. Job's `done` promise resolves to `JobOutcome` with rendered result.
4. Job cancellation → `run.cancel()`.
5. Run disposal and recorder finalization happen in job settlement.

### Progress recording (`tool-workflow/src/record.ts`)

Four session events capture the workflow trajectory:

| Event | Data |
|---|---|
| `tool-workflow/run-start` | `{ runId, name }` |
| `tool-workflow/agent-start` | `{ runId, seq, label, phase?, childId }` |
| `tool-workflow/agent-end` | `{ runId, seq, outcome }` |
| `tool-workflow/run-end` | `{ runId, stopReason }` |

Failures in recording are logged but don't fail the workflow.

### Workflow schema (tool parameters)

```json
{
  "script": "string (required) - plain JavaScript body, top-level await allowed",
  "meta": {
    "name": "string (required) - kebab-case workflow name",
    "description": "string (required) - one-line description",
    "whenToUse": "string (optional) - guidance",
    "phases": [
      { "title": "string", "detail": "string", "provider": "string", "model": "string" }
    ]
  },
  "args": "object (optional) - exposed as global `args` in script",
  "run_in_background": "boolean (optional, default true)"
}
```

### Script authoring contract

The tool description embeds the exact hooks and semantics:

```
Script-body hooks:
- agent(prompt, opts?): Promise<any>
  - Without opts.schema: resolves to child's final text
  - With opts.schema: resolves to validated object (subset of JSON Schema)
  - Resolves null when child fails (filter with .filter(Boolean))
  - Other opts: label (display), phase (progress group), provider/model overrides
- pipeline(items, ...stages): Promise<any[]>
  - Runs each item through stages independently, NO barrier between stages
  - Each stage receives (prev, item, index)
  - Stage throw drops that ITEM to null and skips remaining stages
- parallel(thunks): Promise<any[]>
  - Runs zero-argument functions concurrently, awaits ALL (barrier)
  - Throwing thunk resolves to null
- phase(title) — start progress phase
- log(message) — narrate progress
- args — the tool call's args input, verbatim

Misused hooks (bad arguments, unknown options, unsupported schemas, tripped caps)
end the whole script instead of producing null.
```

## Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_workflow_tool_name` | `str` | `"workflow"` | Model-facing tool name. |
| `deepspace_workflow_max_result_chars` | `int` | `50000` | Rendered result ceiling. |
| `deepspace_workflow_enable_run_in_background` | `bool` | `true` | Enable background job mode. |

## What to watch for

- The guest realm has **no timers** — `setTimeout`, `setInterval`, `Date.now()` are not available.  Scripts must be synchronous or async via host hooks.
- `pipeline` has **no barrier** between stages; use `parallel` when a stage needs all prior results.
- A throwing `agent` call resolves to `null`; the script must handle `null` results.
- Schema validation for `agent` is strict: unsupported keywords cause script termination.
- Background runs require `ctx.jobs` (load `dsh-jobs` and `dsh-tool-jobs`).
- The workflow tool registers a system prompt section with usage guidance.

## Related chapters

- [Workflow Orchestration](38-workflow-orchestration.md) — user-facing workflow guide.
- [The Tool Catalogue](09-the-tool-catalogue.md) — `workflow_run` tool reference.
- [Delegation and Subagents](04-delegation-and-subagents.md) — `agent` hook uses subagent infrastructure.
