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
| 10 | [Direct native tool schemas](10-dynamic-tool-discovery.md) | How DeepSpace supplies preset-authorized schemas up front and rechecks every call. |
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
| 26 | [Browser Automation](26-browser-automation.md) | Playwright browser server and browser descendants launched through shared process containment, with 16 tools. |
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
| 39 | [Native Utilities](39-native-utilities.md) | Native Python implementations of atomic writes, data structures, output retention, market metadata, project manifests, and desktop commands. |
| 40 | [Goal-Driven Conversations](40-goal-driven-conversations.md) | Durable goals with automatic continuation rounds, phase model, round limits. |
| 41 | [Deliverables & Changed Files](41-deliverables-changed-files.md) | Changed file projection from session log + declared deliverables store. |
| 42 | [Local SearXNG web search](42-local-searxng.md) | The local search runtime, JSON search, URL reading, status, and limits. |
| 43 | [Manual end-to-end test playbook](43-manual-e2e-test-playbook.md) | Isolated, repeatable acceptance checks for tools, agents, teams, security, browser surfaces, and the other DeepSpace capabilities. |
| 44 | [Manual end-to-end run ledger](44-manual-e2e-run-ledger.md) | Copyable run records, evidence fields, repeat counts, and a coverage checklist. |
| 45 | [Tool errors and model recovery](45-tool-errors-and-model-recovery.md) | Recoverable tool-operation failures, fatal runtime failures, approval denial, and the queue/composer boundary. |
| 46 | [Native file drag-out](46-native-file-drag.md) | Authenticated Library export through a bounded, sender-authorized Electron drag lifecycle. |
| 47 | [Application and shared agent paths](47-home-and-agent-paths.md) | Separate app and `.agents` roots, workspace path helpers, and canonical watcher paths. |
| 48 | [Source-aware launch environment](48-launch-environment.md) | Immutable process/project/user environment precedence, provenance, and SSH detection. |
| 49 | [Outbound HTTP proxy policy](49-outbound-http-proxy.md) | Validated proxy selection, bypass rules, HTTPX setup, child environment, and isolation boundaries. |
| 50 | [Lazy optional imports](50-lazy-optional-imports.md) | Success-cached, retryable optional Python imports and the wired DeepSpace call sites. |
| 51 | [Native subprocess, terminal, telemetry](51-native-subprocess-terminal-telemetry.md) | Shared subprocess launches, Windows PTY host, process containment, terminal readiness, and live telemetry channels. |
| 52 | [Storage domains](52-storage-domains.md) | The persistent JSON/SQLite storage hub, mounted domain facility, component ownership, and boundaries with the application database. |
| 53 | [Python capability transport and generation](53-python-capability-generation.md) | Authenticated unary and duplex capability calls plus reproducible Python clients and JSON Schema artifacts. |
| 54 | [Per-turn Skill Catalog Injection](54-skill-catalog-and-injection.md) | Model-facing skill catalog rebuilt every turn with budget, scope, and incomplete state. |
| 55 | [Skill Scope and Delegation](55-skill-scope-and-delegation.md) | Child agent skill visibility with grant/deny chains and cold-resume persistence. |
| 56 | [Skill Authoring and Validation](56-skill-authoring-and-validation.md) | Frontmatter schema, duplicate checks, resource reachability, and `skill_validate` tool. |
| 57 | [Signed Remote Skill Catalog](57-remote-skill-catalog.md) | HTTPS Ed25519-signed skill bundles with injectable fetcher and incomplete state. |
| 58 | [Runtime Skill Provider](58-runtime-skill-provider.md) | Rank 250 ephemeral skills published by plugins/workflows without touching disk. |
| 59 | [Skill Load Telemetry](59-skill-telemetry.md) | Structured events for load/list/resource/validate with session telemetry integration. |
| 60 | [Skill Asset Bundles](60-skill-asset-bundles.md) | Office skills as self-contained bundles with `SKILL.md` + `references/` served by `skill_resource`. |
| 61 | [Workspace Dependencies](61-workspace-dependencies.md) | Bundled Python/Node/pnpm/LibreOffice/Poppler paths with atomic payload install. |
| 62 | [Workspace Registry Internals](62-workspace-registry-internals.md) | Storage schema, bootstrap, session activity events, archive/pin mechanics. |
| 63 | [Workflow PTC Runtime](63-workflow-ptc-runtime.md) | Guest/host architecture, caps, hooks, background execution, progress recording. |
| 64 | [Terminal Session Lifecycle](64-terminal-session-lifecycle.md) | PTY management, sanitize rules, job streaming, signal handling. |
| 65 | [OTel Span Attributes](65-otel-span-attributes.md) | Turn/provider/tool/skill span hierarchy, attributes, events, custom exporters. |
| 66 | [Subprocess Scope](66-subprocess-scope.md) | Linux namespace, Windows Job Objects, runner protocol, resource limits. |
| 67 | [Subagent External Backends](67-subagent-external-backends.md) | ACP, Claude Code, Codex, DSH SDK, and KineSis SDK configuration and filtering differences. |
| 68 | [Storage Domain API](68-storage-domain-api.md) | JSON vs SQLite backends, atomic writes, migration, component ownership. |
| 69 | [SSH Helper Runtime](69-ssh-helper-runtime.md) | Remote sandbox, stream security, helper protocol, remote filesystem/terminal. |
| 70 | [Spill Policy](70-spill-policy.md) | Thresholds, retention, object storage, spill read/session tools. |

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
