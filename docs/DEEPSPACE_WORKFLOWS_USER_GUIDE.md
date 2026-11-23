# DeepSpace Workflows — User Guide

**Date:** 2026-11-19

---

## What a Workflow Actually Is

Normal chat = **one agent, one question, one answer.** Coding chat = the same, but the agent can edit files and run commands in your workspace.

A **workflow** is when the agent breaks one piece of work into **several workers**, each with a clear job, and then combines their answers. Think "a team of specialists you brief once" rather than "one person doing everything."

**Use it when:** the work splits cleanly into many similar pieces — 50 files to audit, 3 competitors to compare, one migration across many files, or one set of findings that should be checked from several angles.

**Skip it when:** you only need one or two extra opinions. The agent delegates those directly with `subagent`; a workflow is the heavier tool and burns more agents.

---

## Two Ways to Run a Workflow

| Mode | What It Is | When to Use |
|---|---|---|
| **Named workflow** | A JSON document under `.averqel/workflows/<name>.json` listing steps and dependencies | Repeatable recipes you save once, run many times |
| **Inline script** | A JavaScript orchestration script with `workflow.agent`, `workflow.parallel`, `workflow.pipeline`, etc. | One-off complex orchestration, or when you need dynamic logic |

Both modes run steps as child agents with the same tools, model, and sandbox as the parent.

---

## Named Workflows (JSON Documents)

A named workflow is a JSON document in the project workspace at:
```
.averqel/workflows/<name>.json
```

### Document Structure

```json
{
  "description": "Audit every file in src/",
  "max_concurrency": 2,
  "steps": [
    { "id": "step-1", "task": "Audit src/auth.py", "depends_on": [] },
    { "id": "step-2", "task": "Audit src/db.py", "depends_on": [] },
    { "id": "step-3", "task": "Summarize findings", "depends_on": ["step-1", "step-2"] }
  ]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `description` | No | Human-readable description |
| `max_concurrency` | No (default: step count) | Max parallel child agents (1–20) |
| `steps` | Yes | Array of step objects |
| `steps[].id` | Yes | Unique step identifier |
| `steps[].task` | Yes | The task text for the child agent |
| `steps[].depends_on` | No | Array of step IDs this step waits for |

**Limits:**
- Max 20 steps per workflow
- Max 20,000 chars per task
- Max 200,000 chars per document

### Dependency Execution

Steps with `depends_on` wait for those steps to complete. Independent steps run in parallel up to `max_concurrency`. Waves are executed sequentially: wave 1 completes before wave 2 starts.

---

## Inline Scripts (JavaScript Orchestration)

Pass a `script` string to `workflow_run` instead of `name`. The script runs in a sandboxed engine with these hooks:

| Hook | Purpose |
|---|---|
| `workflow.agent(task, options?)` | Run one child agent to completion |
| `workflow.parallel(items, mapper, options?)` | Run mapper over every item concurrently |
| `workflow.pipeline(items, ...stages)` | Run items through sequential stages |
| `workflow.phase(title)` | Start a progress phase |
| `workflow.log(message)` | Narrate progress |
| `workflow.args` | The call's input text |

**Example:**
```javascript
const results = await workflow.parallel(files, async (file) => {
  return workflow.agent(`Audit ${file}`, { label: file });
});
workflow.phase("summarize");
return await workflow.agent(`Summarize: ${JSON.stringify(results)}`);
```

**Requirements:** `deepspace_ptc_node_enabled: true` (off by default), Node.js ≥ 20, bubblewrap.

---

## Running a Workflow

### From Chat (Natural Language)

| You Say | Agent Does |
|---|---|
| *"Audit every file in src/"* | Creates/runs a workflow with one step per file |
| *"Compare Acme, Bolt, and Cirrus"* | Runs three parallel research workers |
| *"Run the release checklist"* | Runs the saved workflow named "release-checklist" |

The agent decides whether to use a named workflow or inline script.

### From API

```bash
# Named workflow
POST /api/v1/deepspace/chats/{id}/workflow_run
{"name": "release-checklist", "input": "v1.2.3"}

# Inline script
POST /api/v1/deepspace/chats/{id}/workflow_run
{"script": "return workflow.agent('Hello');"}

# Background (returns job_id immediately)
POST /api/v1/deepspace/chats/{id}/workflow_run
{"name": "release-checklist", "run_in_background": true}
```

### Tool Arguments

| Argument | Type | Description |
|---|---|---|
| `name` | string (optional) | Workflow document name (without `.json`) |
| `script` | string (optional) | JavaScript orchestration script |
| `input` | string (optional) | Input passed to all steps |
| `meta` | object (optional) | Metadata for script mode |
| `run_in_background` | bool | Queue and return job handle immediately |

---

## Background Runs

When `run_in_background: true`:
- Returns `{"status": "background", "job_id": "..."}` immediately
- The run executes in the DeepSpace worker
- Progress events are emitted to the session log
- Use `GET /api/v1/deepspace/chats/{id}/workflow_jobs/{job_id}` to poll

---

## Workflow Editor (UI)

The **Workflow Editor** in the side panel lets you manage workflow documents:

- **List** — shows all workflows in `.averqel/workflows/`
- **New** — starts a blank document with `max_concurrency: 2` and one empty step
- **Edit** — JSON editor with syntax highlighting and validation
- **Save** — writes the document back to the workspace
- **Delete** — removes the document

**Access:** The editor is available in the side panel. Read-only conversations can run workflows but cannot save or delete them.

---

## Real Example Scenarios

### Scenario 1 — "Audit every file in `src/`"

The agent splits it into one worker per file. Each worker checks its file against the rules you gave. If you asked for *"a score and a one-line verdict per file"*, each worker returns a JSON object — so the agent gets clean numbers it can total and rank.

### Scenario 2 — "Compare Acme, Bolt, and Cirrus"

The agent runs three workers at once (independent). Each researches one competitor and returns the same fields: pricing, key features, and limits. Because every worker returns the same shape, the comparison table is complete.

### Scenario 3 — "Check these 12 findings from three angles"

The agent fans 12 findings across three reviewer angles (correctness, security, performance). Every finding gets every angle. A finding that only one angle flagged is clearly the interesting one.

### Scenario 4 — "Run our weekly release checklist"

You build it once in the Workflow Editor: steps "lint", "unit tests", "dependency audit", "changelog check", where the last depends on the first three passing. Save it. Next week, *"run the release checklist"* and it's the same every time.

---

## What to Expect

- **The agent writes the recipe itself.** You never write JavaScript for named workflows. The tool's description tells the agent exactly which hooks exist.
- **More agents = more time and more tokens.** That's the trade for parallelism and coverage. Workflows have caps (max 20 steps, max 20 concurrent).
- **A failed worker doesn't sink the whole run** — that worker yields `null` and the others continue.
- **Named workflows run exactly once.** The infinite-recursion bug is fixed and covered by a regression test.
- **Background runs keep going.** The run gets a durable handle, a worker owns it, and it survives the turn that started it.

---

## Configuration (Admin)

| Setting | Default | Description |
|---|---|---|
| `deepspace_ptc_enabled` | `true` | Master toggle for programmatic tool calling |
| `deepspace_ptc_node_enabled` | `false` | Enable inline JavaScript scripts (needs Node.js + bubblewrap) |
| `deepspace_ptc_node_executable` | `node` | Node.js executable |
| `deepspace_ptc_node_timeout_ms` | `120000` | Script execution timeout |
| `deepspace_workflow_runs_enabled` | `true` | Enable workflow execution |

If you get *"JavaScript PTC runtime is disabled"*, enable `deepspace_ptc_node_enabled`.

---

## Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| "No workflow named X" | Document not in `.averqel/workflows/` | Create it in the editor or via API |
| "Workflow step ids must be unique" | Duplicate `id` in steps | Use unique IDs |
| "Workflow's step dependencies contain a cycle" | Circular `depends_on` | Fix the dependency graph |
| "JavaScript PTC runtime is disabled" | `deepspace_ptc_node_enabled=false` | Enable in settings |
| Background run never completes | No DeepSpace worker consuming queue | Check worker is running |
| "This conversation is in read-only mode" | Sandbox is `read-only` | Switch to `workspace-write` or `danger-full-access` |

---

## Related Documentation

| Topic | Document |
|---|---|
| Workflow contract, hooks, schema, API | `backend/docs/deepspace/38-workflow-orchestration.md` |
| Tool catalogue entry | `backend/docs/deepspace/11-deepspace-native-tool-surface.md` |
| Workspace & file routes | `backend/docs/deepspace/15-workspaces-and-files.md` |
| Configuration reference | `backend/docs/deepspace/22-configuration-reference.md` |
| Manual acceptance checks | `backend/docs/deepspace/42-manual-e2e-test-playbook.md` (case S05) |
| In-app help | `frontend/app/documentation/deepspace/page.tsx` (Workflows section) |

---

## TL;DR

- **Named workflow:** Save JSON in `.averqel/workflows/<name>.json`, run with `workflow_run(name="name")`
- **Inline script:** Pass `script` to `workflow_run` with `workflow.agent/parallel/pipeline` hooks
- **Editor:** Side panel → Workflow Editor to manage documents
- **Background:** `run_in_background: true` returns job_id immediately
- **Node.js scripts need:** `deepspace_ptc_node_enabled: true`
- **Max 20 steps, max 20 concurrent** — bounded by design