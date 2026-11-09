# 10. Direct native tool schemas

Tool-capable provider requests receive the native schemas allowed by the active
deployment, selected preset, and current plan or task stage. The model can call
those tools immediately. Native `discover_tools` is not a model-facing tool.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [Build the effective tool set](#build-the-effective-tool-set)
  - [Cache schemas safely](#cache-schemas-safely)
  - [Recheck execution permission](#recheck-execution-permission)
  - [MCP remains connection-scoped](#mcp-remains-connection-scoped)
- [What to watch for](#what-to-watch-for)

## Use case

Read this page when an authorized native tool is missing from a provider
request, when you are changing a preset or tool definition, or when you are
investigating repeated `discover_tools` calls in a trajectory.

## Why it matters

- Sending authorized native schemas up front lets the model call a known tool
  on its first useful turn instead of spending rounds cataloguing and loading
  schemas.
- The model sees the exact definitions it may use for the current preset and
  task stage. Disabled, denied, or otherwise unavailable tools stay out of the
  request.
- Schema caching reduces repeated definition work and stable ordering helps
  compatible providers reuse prompt-cache prefixes. Neither grants execution
  permission.
- MCP tools have connection-specific schemas and remain behind the separate
  authenticated MCP broker.

## How it works

### Build the effective tool set

At turn setup, DeepSpace combines the native registry with the deployment's
availability, selected preset, active plan mode, task lifecycle stage, enabled
plugins, extensions, and available web provider. It sends the resulting native
schemas directly to the model. Plan mode and managed task lifecycle stages
further narrow each provider request. Child agents receive their own set after
the parent's allowlist and the child-specific delegation rules are applied.

A direct conversational answer can still use no tools. The provider chooses
whether to call one of the supplied schemas. If it invents a disabled or
unoffered native tool, the runtime refuses it. A legacy `discover_tools` call
gets one direct-use correction; if the model repeats it, DeepSpace stops that
turn instead of allowing a discovery loop to grow the trajectory.

### Cache schemas safely

The registry holds canonical typed schemas and a digest for each definition.
DeepSpace caches serialized schema definitions by their schema digests and an
effective policy key, then decodes defensive copies for each provider request.
Stable ordering keeps otherwise identical request prefixes stable. Changes to
the effective tool set or schema digest use a different cache key.

The cache is only a schema optimization. The allowed-name set is recomputed for
each turn, and tool execution checks the current deployment, preset, sandbox,
approval, hook, typed-argument, and tenant or conversation rules again.

### Recheck execution permission

A supplied schema means the model can form a valid call; it does not guarantee
that the call will run. The runtime validates arguments, applies the current
permission mode and tool policy, checks any approval or hook requirement, and
uses the scoped handler. Paths and resources remain limited by the active
workspace and authenticated conversation. A denied call does not reach its
handler.

Tool schemas count as protected request material in context budgeting. History
is compacted first to make room. Providers without a verified context window
use their existing provider defaults; operators should use models with a
context window that can hold their selected preset's native schema set.

### MCP remains connection-scoped

MCP bindings are discovered for the authenticated conversation. The model uses
the compact MCP broker to search the authorized catalog, load a selected
upstream schema, call its exact returned reference, and retrieve a bounded
result when needed. It never receives raw connection credentials or transport
configuration. Each MCP call rechecks ownership, connection status, catalog
revision, policy, risk, approval, audit, and upstream schema validation.

The native `discover_tools` broker remains available to compatible internal
callers, but it is excluded from all presets and is never included in normal
model requests.

## What to watch for

- Check the trajectory's provider request tool names first. A missing schema
  usually means a deployment, preset, provider capability, plan, task-stage,
  plugin, or extension filter removed it.
- A policy refusal after the model received a schema is expected when sandbox,
  approval, hook, or current conversation state disallows the actual call.
- Repeated `discover_tools` calls indicate an older prompt, stale backend, or
  provider continuing from old conversation state. The current backend refuses
  the call and stops repeated attempts.
- Full native schemas consume context. The context estimator accounts for
  them and compacts ordinary history; it does not authorize a schema or weaken
  a policy to make a request fit.
- The dynamic MCP broker is separate. MCP schemas remain tied to the active
  connection and are loaded only after a model selects an exact reference.
