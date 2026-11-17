# DeepSpace Subagents — User Guide

**Date:** 2026-11-14

---

## What It Is

A **subagent** is a delegated child agent that the main agent spawns to do a piece of work. Think of it as hiring a specialist for a specific task — the child has its own context, tools, and model, runs to completion (or stays alive for follow-up), and reports back.

**Use it when:**
- The work splits into independent pieces (research 5 topics in parallel)
- You want a different model/persona/tools for a specific job
- A task is long and you want to check on it later (continuable mode)

**Skip it when:**
- You only need one or two extra opinions — the agent delegates those directly without the subagent tool
- The work is a single linear chain — just let the agent do it

---

## What You See in the UI

| Location | What It Shows |
|---|---|
| **Side Panel → Agent Team** | Live child agents: ID, label, status, depth, busy flag |
| **Chat** | Agent narrates what it's delegating; you see child results inline |
| **Jobs panel** | Background continuable children appear here with handles |

---

## How to Use It (Natural Language)

Just ask. The agent picks the right tool.

| You Say | Agent Does |
|---|---|
| *"Research Acme, Bolt, and Cirrus in parallel"* | 3 × `subagent` (one-shot), each with `tool_allow=["web_search"]` |
| *"Start a deep analysis I can check on later"* | `subagent_fork` (continuable) → returns `child_id` |
| *"Tell that child to focus on pricing"* | `subagent_send_message(child_id, "Focus on pricing")` |
| *"Show me all my child agents"* | `subagent_list()` |
| *"What did that child produce?"* | `subagent_get(child_id)` |
| *"Stop that child"* | `subagent_control(child_id, action="terminate")` |

---

## Two Modes

| Mode | Tool | Behavior | Use For |
|---|---|---|---|
| **One-shot** | `subagent` | Runs task → returns result → dies | Independent pieces, parallel research, fixed jobs |
| **Continuable** | `subagent_fork` | Starts child → returns handle → stays alive | Long tasks, interactive follow-up, multi-stage work |

**One-shot** is the default. Ask for *"a child I can talk to later"* or *"continuable"* to get the other.

---

## What the Child Inherits (and What It Doesn't)

| Inherited | Not Inherited |
|---|---|
| Tenant, user, conversation | Parent's full context/history |
| Workspace (cwd) | Server credentials/secrets |
| Sandbox mode | Parent's tool calls |
| Depth limit (max 3) | — |

**Each child gets a reduced environment** — no API keys, no secrets from the parent process.

---

## Limits (Configurable by Admin)

| Limit | Default | Purpose |
|---|---|---|
| `deepspace_subagent_max_depth` | 3 | Max nesting (child of child of child) |
| `deepspace_subagent_max_children` | 8 | Max simultaneous children per conversation |

Hit a limit? The agent will tell you and suggest alternatives.

---

## What "Working" Looks Like

1. **You ask** → Agent decides how many children, what mode, what tools
2. **Children spawn** → **Agent Team panel** shows them appear (label, status, depth)
3. **Work happens** → Children run in parallel (one-shot) or wait for messages (continuable)
4. **Results return** → Agent synthesizes, shows you the combined answer
5. **Continuable children** → Stay in **Agent Team** panel; you can message them anytime

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| *"Subagent limit reached"* | Too many simultaneous children | Wait for some to finish, or ask agent to batch |
| *"Depth limit reached"* | Nested delegation too deep | Flatten: ask parent to do the work directly |
| *"Child not found"* | Wrong `child_id` or already finished | Use `subagent_list` to see live ones |
| *"Tool not allowed"* | Child's `tool_allow` too narrow | Ask agent to widen `tool_allow` for that child |
| *"Provider not available"* | Requested model/provider not configured | Agent falls back to default; specify a different one |

---

## Providers (What Powers the Children)

| Provider | What It Runs | Use When |
|---|---|---|
| **In-process** (default) | Same Python process, new agent loop, **queued to background worker** | Durable background work, long tasks, continuable children |
| **In-process inline** | Same Python process, **runs directly in your call**, returns answer immediately | Fast same-turn answers, quick lookups, no background wait |
| **ACP** | External ACP-compatible agent | Interop with other systems |
| **Codex CLI** | OpenAI Codex CLI subprocess | Codex-specific workflows |
| **Claude Code** | Anthropic Claude Code CLI | Claude-specific workflows |
| **DSH SDK** | DeepSeek Harness SDK subprocess | Harness-native child agents |

The agent picks the best one automatically. You can request: *"Use in-process inline for this quick check"* or *"Use Codex for this child."*

### In-Process vs In-Process Inline

| | In-Process (default) | In-Process Inline |
|---|---|---|
| **Runs in** | Background worker (Celery/local pool) | Your same tool call |
| **Returns** | Child ID immediately (`status: "queued"`) | Answer immediately (`status: "ok"`) |
| **Get result** | Call `get_agent(child_id)` later | Answer in same response |
| **Continuable** | ✅ Full support | ✅ Full support |
| **Use for** | Long tasks, background research, follow-up | Quick lookups, same-turn answers |

---

## Technical Details (If You're Curious)

- **Durable runs** — `DeepSpaceSubagentRun` model persists every child; survives backend restart
- **Session events** — `subagent/spawn`, `subagent/start`, `subagent/end`, `subagent/result`, `subagent/descriptor` logged for replay
- **Depth tracking** — `delegation_depth` increments each level; max 3 by default
- **Continuable resume** — Child keeps its `turn_history`; follow-up turns have full context
- **Archive admission** — Long-running children can be archived and resumed later

---

## Related Documentation

| Topic | Document |
|---|---|
| Subagent API routes | `backend/docs/deepspace/11-deepspace-native-tool-surface.md` |
| Tool catalogue | `backend/docs/deepspace/09-the-tool-catalogue.md` |
| Presets & tool allowlists | `backend/docs/deepspace/22-configuration-reference.md` |
| In-app help | `frontend/app/documentation/deepspace/page.tsx` (Agent Team section) |

---

**TL;DR:** Say "run X in parallel" or "start a child I can talk to later." The agent handles the rest. Check **Agent Team** panel to watch live children.