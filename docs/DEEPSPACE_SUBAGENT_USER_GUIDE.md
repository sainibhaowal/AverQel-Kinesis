# DeepSpace Subagents — User Guide

**Date:** 2026-11-19

---

## What It Is

A **subagent** is a delegated child agent that the main agent spawns to do a piece of work. The child has its own context, tools, and model, runs its own turns, and reports its answer back to the parent.

**Use it when:**
- The work splits into independent pieces (research 5 topics in parallel)
- You want a different model, persona, or tool set for a specific job
- A task is long and you want to check on it later (`mode="continuable"`)

**Skip it when:**
- You only need one or two extra opinions
- The work is a single linear chain — just let the agent do it

---

## What You See in the UI

| Location | What It Shows |
|---|---|
| **Side Panel → Agent Team** | Live child agents: id, label, status, depth, result availability |
| **Chat** | The parent narrates what it delegates and reports child results inline |
| **Jobs panel** | Queued background work, including queued child turns |

---

## How to Use It (Natural Language)

Just ask. The agent picks the right tool.

| You Say | Agent Does |
|---|---|
| *"Research Acme, Bolt, and Cirrus in parallel"* | Three `subagent` calls (`mode="one-shot"`), each with a complete task |
| *"Start a child I can check on later"* | `subagent` (default `mode="continuable"`) → returns a child id |
| *"Tell that child to focus on pricing"* | `send_message(agent_id, "Focus on pricing")` |
| *"Show me all my child agents"* | `list_agents()` |
| *"What did that child produce?"* | `get_agent(agent_id)` |
| *"Wait for that child to finish"* | `get_agent(agent_id, wait_timeout_seconds=60)` |
| *"Stop that child"* | `interrupt_agent(agent_id=...)` |

---

## Two Tools, Two Shapes

| Shape | Tool | Behavior | Use For |
|---|---|---|---|
| **Continuable** (default) | `subagent` | Starts a child that stays available for more messages | Long tasks, follow-up, multi-stage work |
| **One-shot** | `subagent` with `mode="one-shot"` | Runs the task once and ends | Independent pieces, parallel research, fixed jobs |
| **Forked one-shot** | `subagent_fork` | One-shot child that inherits recent conversation history | Work that needs the context already in the chat |

`subagent_fork` always runs `one-shot`; it adds `history_messages` (0–40) so the child can see the recent conversation instead of only the task text.

---

## What the Child Inherits (and What It Doesn't)

| Inherited | Not Inherited |
|---|---|
| Tenant, user, conversation | The parent's full context (unless `subagent_fork`) |
| Workspace and sandbox mode | Server credentials and secrets |
| Provider/model route (unless overridden) | The parent's completed tool results |
| Tool and skill set (unless restricted) | — |

Each child gets a reduced environment — no API keys, no server secrets from the parent process.

---

## Limits (Configurable by Admin)

| Limit | Default | Purpose |
|---|---|---|
| `deepspace_subagent_max_depth` | 3 | Max nesting (child of child of child) |
| `deepspace_subagent_max_children` | 16 | Max child agents per conversation |
| `deepspace_fork_max_depth` | 10 | Max depth for forked children |

Hit a limit? The agent will tell you and suggest alternatives.

---

## What "Working" Looks Like

1. **You ask** → The agent decides how many children, which mode, and which tools
2. **Children spawn** → **Agent Team** shows them appear (label, status, depth)
3. **Work happens** → Children run in parallel (one-shot) or stay available for messages (continuable)
4. **Results return** → The parent synthesizes the combined answer
5. **Continuable children** → Stay in **Agent Team**; you can message them later

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| *"Subagent limit reached"* | Too many children | Wait for some to finish, or ask the agent to batch |
| *"Depth limit reached"* | Nested delegation too deep | Flatten: do the work at the top level |
| *"Child not found"* | Wrong id or already removed | Use `list_agents` to see the live roster |
| *"Tool not allowed"* | The child's `tool_allow` is too narrow | Ask the agent to widen `tool_allow` for that child |
| *"Provider not available"* | Requested route or model is not configured | Use `list_subagent_models` or a different route |
| Child never answers | No worker consuming the queue, or the child turn failed | In the local default Celery eager mode child turns run inline; otherwise start a worker and read the child's error with `get_agent` |

---

## Providers (What Powers the Children)

| Provider | What It Runs | Use When |
|---|---|---|
| **In-process** (default) | Same Python process, queued to the background worker | Durable background work, long tasks |
| **In-process inline** | Same Python process, runs in the tool call | Fast same-turn answers |
| **ACP** | External ACP-compatible agent | Interop with other agent systems |
| **Codex CLI** | OpenAI Codex CLI subprocess | Codex-specific workflows |
| **Claude Code** | Anthropic Claude Code CLI | Claude-specific workflows |
| **DSH SDK** | DeepSeek Harness SDK subprocess | Harness-native child agents |
| **Kinesis SDK** | Kinesis SDK subprocess | Kinesis-native child agents |

The agent picks automatically. You can request: *"Use in-process inline for this quick check"* or *"Use Codex for this child."*

External backends run one-shot children only. **In-process** returns a child id and runs the child's turn in the background worker; **in-process inline** runs the turn inside the parent's tool call and returns the answer immediately.

---

## Technical Details (If You're Curious)

- **Durable runs** — child rows persist in the database and survive a backend restart
- **Session events** — spawn, start, end, result, and descriptor events are recorded for replay
- **Depth tracking** — `delegation_depth` increments each level; max 3 by default
- **Continuable resume** — a continuable child keeps its turn history for follow-up messages
- **Wait discipline** — `get_agent` accepts a bounded wait of 1–90 seconds; prefer that to repeated polling

---

## Related Documentation

| Topic | Document |
|---|---|
| Tool catalogue | `backend/docs/deepspace/09-the-tool-catalogue.md` |
| Delegation chapter | `backend/docs/deepspace/04-delegation-and-subagents.md` |
| Presets & tool allowlists | `backend/docs/deepspace/22-configuration-reference.md` |
| In-app help | `frontend/app/documentation/deepspace/page.tsx` (Agent Team section) |

---

**TL;DR:** Say "run X in parallel" for one-shot children, or "start a child I can talk to later" for a continuable one. The Agent Team panel shows live children.
