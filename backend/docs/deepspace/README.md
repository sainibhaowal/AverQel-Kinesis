# The DeepSpace manual

DeepSpace is the conversation-led workspace inside AverQel. This folder is
its manual: one numbered chapter per subject, written so that a person can
find one thing, read it, and act on it.

Every chapter answers the same four questions:

- **Use case** - who needs this, and when.
- **Why it matters** - what goes wrong without it.
- **How it works** - the mechanism, with the real setting names, tool
  names, routes, and event names.
- **What to watch for** - the limits, the failure modes, and the parts that
  are unit-tested but not yet run against a real host.

Nothing in this manual is aspirational. If a feature is not finished, the
chapter says so in **What to watch for**.

A chapter describes the current implementation and its deployment prerequisites;
it is not a certificate that every provider, external service, operating-system
backend, or desktop shell has been exercised on every host. The **What to watch
for** section names those boundaries and the checks that provide evidence.

## The chapters

| # | Chapter | What it covers |
|---|---|---|
| 01 | [What DeepSpace is](01-what-deepspace-is.md) | The product, the actors, the vocabulary, and what makes it different from a plain chat. |
| 02 | [The agentic loop](02-the-agentic-loop.md) | One turn, phase by phase: prepare, model round, tool calls, settle. |
| 03 | [Parallel execution](03-parallel-execution.md) | The parallel tool budget, read and write batching, deadlines, and the result spill. |
| 04 | [Delegation and subagents](04-delegation-and-subagents.md) | Children, one-shot and continuable, depth, the 16-child budget, and the worker. |
| 05 | [Teammates and the swarm](05-teammates-and-swarm.md) | Named teammates, the shared board, messaging a teammate that is away. |
| 06 | [Background work](06-background-work.md) | Jobs, schedules, workers, and the prompt queue. |
| 07 | [The session log and the trajectory](07-the-session-log-and-trajectory.md) | The typed events, the projection, the tools that read them, and compaction. |
| 08 | [Model selection](08-model-selection.md) | How the model is chosen, why the choice is explainable, and the manual override. |
| 09 | [The tool catalogue](09-the-tool-catalogue.md) | The 133 registered native tools, grouped by capability, with the runtime registry as the authoritative inventory. |
| 10 | [Dynamic tool discovery](10-dynamic-tool-discovery.md) | Why the tool list is not fixed, and how the model loads a tool. |
| 11 | [The native tool surface](11-deepspace-native-tool-surface.md) | The long-form reference for the tool surface, the sandbox, and the feature set. |
| 12 | [Hooks](12-hooks.md) | The hook manifest, the two dialects, blocking, asking, and the audit trail. |
| 13 | [The sandbox](13-the-sandbox.md) | The cage and its backends, the permission mode, the approval mode, and the network switch. |
| 14 | [Terminals and the remote runtime](14-terminals-and-remote-runtime.md) | Persistent shell sessions and the SSH runtime. |
| 15 | [Workspaces and files](15-workspaces-and-files.md) | The default and named workspaces, the file tools, the search tools, and the diff. |
| 16 | [The in-app browser](16-the-in-app-browser.md) | The browser panel, its navigation, its rules, and its limits. |
| 17 | [Extensions and plugins](17-extensions-and-plugins.md) | Declarative extensions, plugins with code, and the jail extension code runs in. |
| 18 | [MCP and connectors](18-mcp-and-connectors.md) | The bridge to connected MCP servers, the tool side, and the resource side. |
| 19 | [The composer and the Start screen](19-the-composer-and-start-screen.md) | Every surface in the chat: the cards, the panel, the menus, the buttons, the shortcuts. |
| 20 | [Security and encryption](20-security-and-encryption.md) | Tenant isolation, authentication, the local mode, and what is stored encrypted. |
| 21 | [Presets and agent modes](21-presets-and-agent-modes.md) | Standard, PTC, Minimal, Creator, and Custom. |
| 22 | [Configuration reference](22-configuration-reference.md) | Core deployment settings used across DeepSpace, with feature-specific settings linked to their owning chapters. |
| 24 | [Operations and troubleshooting](24-operations-and-troubleshooting.md) | Running the app, reading the logs, and the common failures. |
| 25 | [Glossary](25-glossary.md) | Every term this manual uses, in one place. |
| 26 | [Browser Automation](26-browser-automation.md) | Real browser (Playwright) driven as external process with 16 tools. |
| 27 | [Computer-Use Provider](27-computer-use.md) | Desktop automation via VNC/Xvfb with 12 tools. |
| 28 | [Session Telemetry + OTel](28-session-telemetry.md) | OpenTelemetry spans for turns, tools, providers. |
| 29 | [Runtime Invariant Registry](29-runtime-invariants.md) | Declarative invariant checks with registry and execution engine. |
| 30 | [Plugin HMR + Configuration Service](30-plugin-hmr.md) | Backend hot reload and JSON-schema validation services; the web client currently shows inventory, not config forms. |
| 31 | [Slash-Command Registry](31-slash-commands.md) | Centralized `/` command registry with parsing, completion, help. |
| 32 | [Permission Presets + Auto-Review](32-permission-presets.md) | Named security bundles + LLM auto-review for tool calls. |
| 33 | [Bundled Skills](33-bundled-skills.md) | Built-in Office (Word/Excel/PPT) and badge (Python/Security/Docs) skills. |
| 34 | [Session Format Migration](34-session-format-migration.md) | Versioned event payload migrations v1→v4 with auto-migration. |
| 35 | [Conversation Fork](35-conversation-fork.md) | Fork conversations at any turn/message for divergent exploration. |
| 36 | [Session Feedback](36-session-feedback.md) | Session-level ratings, categories, comments + aggregate stats. |
| 37 | [Headless Local Run Profile](37-headless-profile.md) | CLI entry point `deepspace` for serverless local runs. |
| 38 | [Workflow Orchestration](38-workflow-orchestration.md) | JavaScript orchestration scripts with subagent fan-out, parallel, pipeline, phases. |
| 39 | [Goal-Driven Conversations](39-goal-driven-conversations.md) | Durable goals with automatic continuation rounds, phase model, round limits. |
| 40 | [Deliverables & Changed Files](40-deliverables-changed-files.md) | Changed file projection from session log + declared deliverables store. |
| 41 | [Local SearXNG web search](41-local-searxng.md) | The Dockerless local search runtime, JSON search, URL reading, status, and limits. |

## Where to start

- **New to DeepSpace** - chapters 01, 02, 19.
- **Building a feature with the agent** - 09, 10, 13, 15.
- **Running many agents** - 04, 05, 06, 03.
- **Setting up a deployment** - 20, 22, 24.
- **Wiring your own tools in** - 12, 17, 18.

## The one-paragraph version

DeepSpace runs a conversation. The model asks for tools, the product runs
them, and the results go back to the model until it answers. Everything it
did is written to a typed log, so the trajectory can be read back. Work
that takes time runs in the background or as a child agent; named
teammates take messages and answer on their own turns. A conversation
works inside one workspace, behind a sandbox, with the tools the
deployment allows, and the user can change the workspace, the agent mode,
the sandbox mode, and the model at any point in the conversation.
