# 10. Dynamic tool discovery

The agent does not receive the full schema of every tool it may call. It
receives one small schema, the discovery schema, and the exact schema of a
tool is loaded only when the agent names that tool. The catalogue the agent
sees is built per request, so a tool the deployment switched off, the
conversation's preset withheld, or the turn has no connection for never
appears in it.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [The compact discovery schema](#the-compact-discovery-schema)
  - [The first call returns a catalogue](#the-first-call-returns-a-catalogue)
  - [The second call loads the schemas](#the-second-call-loads-the-schemas)
  - [What happens to the offered tools afterwards](#what-happens-to-the-offered-tools-afterwards)
  - [The rules that keep a tool out of the catalogue](#the-rules-that-keep-a-tool-out-of-the-catalogue)
  - [Reading the runtime](#reading-the-runtime)
  - [The MCP broker](#the-mcp-broker)
- [What to watch for](#what-to-watch-for)

## Use case

Read this page when you want to know why a tool is not offered to the agent,
when you are writing a plugin or an extension that adds tools, or when you
are debugging an agent that called a tool it should not have known about.

## Why it matters

- Eighty-four tools with full schemas would cost more context than most
  turns need. Loading a handful keeps the request small.
- A tool that is not offered cannot be called, so a disabled capability is
  never discovered and then refused.
- The catalogue is built from the request, not from a static list, so the
  deployment's settings, the conversation's preset, and the conversation's
  connections all apply at once.
- The names are the product's own, so a caller can read the catalogue and know
  exactly which tool it will get.

## How it works

### The compact discovery schema

The request starts with one tool definition.

| Field | Value |
|---|---|
| `name` | `discover_tools` |
| `query` | A natural-language description of the capability or the next action needed, 2 to 1000 characters. Required on every call, including the schema-loading call. |
| `tool_names` | Exact names copied from a returned catalogue, 1 to 8 of them. Omitted on the first call. |

The description the model is given says to call it in two steps: first with a
query and no names, then again with the exact names from the catalogue it
returned.

### The first call returns a catalogue

The first call answers with the names the agent may call and a short
description of each.

| Field | What it holds |
|---|---|
| `status` | `catalog` on the first call. |
| `requested_capability` | The query, with the whitespace collapsed and the length cut to 1000 characters. |
| `available_tools` | One entry per name, each with the name and the first 160 characters of its description. |
| `catalog_truncated` | `true` when the request could call more tools than the catalogue lists. |
| `message` | A sentence telling the agent to choose exact names and call again. |

The catalogue holds at most 40 tools. A request that can call more than 40
tools gets a truncated catalogue and the flag that says so, and the agent has
to search again with a narrower query.

### The second call loads the schemas

The second call repeats the same `query` and carries `tool_names`; the schemas
of exactly those tools become callable on the next request to the model.

| Field | What it holds |
|---|---|
| `status` | `loaded` when the names were accepted. |
| `loaded_tools` | One entry per name, each with the name and the first 240 characters of its description. |
| `message` | A sentence saying the schemas are available on the next provider round. |

Two answers refuse the selection instead of loading it.

| `status` | What it means |
|---|---|
| `invalid_selection` | One of the names is not in this request's authorised catalogue. The names are listed back so the agent can correct them. |
| `selection_too_large` | More than 8 names were asked for. |

### What happens to the offered tools afterwards

When the agent loads a set of tools, the tools offered for the next round are
replaced. What stays available is:

- `discover_tools`, so the agent can look for something else.
- `get_tool_result`, so the agent can read a result the request pruned.
- The MCP broker's own small set of tools.
- The tools that were just loaded.

The tools that were not loaded are no longer offered. The agent has to load
them again if it needs them.

### The rules that keep a tool out of the catalogue

The catalogue is the request's own set of authorised names. A name is removed
from it when any of these hold.

| Rule | What removes the tool |
|---|---|
| The deployment switched it off | `bash` and `pwsh` follow `deepspace_shell_enabled`. The thirteen remote-runtime tools follow `deepspace_ssh_runtime_enabled`. |
| The conversation's preset withheld it | The preset's tool list replaces the deployment's list for that conversation. The two discovery tools stay in every mode, because they are how the model finds out what the mode allows. |
| A plugin that offered the tool is switched off | The plugin's tools leave the catalogue when the plugin is not enabled for the conversation. |
| The turn has no web provider | `web_search` leaves the catalogue when the deployment has no web search provider. |
| The turn has no connected service | The three MCP resource tools leave the catalogue, because without a server they would only be refused. |
| The turn is a planning turn | Every write tool is withheld, and the plan tools are added. |
| The model does not support tool calls | The whole native catalogue is withheld and the turn runs without tools. |

The names are checked again at call time. A name that is not a tool at all is
refused with the message that it is not available in DeepSpace, so a model
that invents a name does not reach a handler. A tool the deployment switched
off is not in the catalogue, and the shell tools also refuse the call
themselves when the shell is switched off.

### Reading the runtime

Three tools report what the runtime holds, for an agent that wants to look
before it acts.

| Tool | What it reports |
|---|---|
| `runtime_discover` | The tools the agent may call and the mode each one runs under, the plugins, the model routes a child may run under, and the installed extensions. `kind` narrows it to one of `tools`, `plugins`, `extensions`, or `routes`. |
| `averqel_inspect_list` | The six capabilities of the runtime: `plugins`, `tools`, `sandbox`, `providers`, `terminal`, and `schedule`. |
| `averqel_inspect_query` | One capability in detail: the tools it owns, the mode each one runs under, the settings that configure it, and its current state. |

All three are read-only. None of them runs a plugin, a command, or a tool.

### The MCP broker

A connected MCP service works the same way. The model sees one small tool,
`mcp_broker`, instead of the server's whole catalogue, and the exact schema of
one upstream tool is loaded after the model selects it.

| Operation | What it does |
|---|---|
| `search` | Finds the upstream tools of a connected server. |
| `schema` | Loads the compact schema of one upstream tool. |
| `call` | Runs one exact upstream tool, by the reference that was returned to it. |
| `result` | Reads the result of a call that produced more than the turn's limit allows. |

The full catalogue and the exact schemas stay on the server side. A limit
applies to the whole turn: `deepspace_mcp_max_calls_per_turn` is 8, and
`deepspace_mcp_max_result_chars_per_turn` is 60 000.

## What to watch for

- The catalogue is truncated at 40 tools and the selection is capped at 8.
  A request that needs more than 8 tools at once needs two rounds.
- A model that skips discovery and calls a tool directly is refused. There is
  one narrow exception: a model that emits `url_read` or `image_read` straight
  after `web_search` has the public web schema loaded for it, so it does not
  get a policy error and a retry. `web_search` itself is in the same set.
- A search that matches nothing returns a catalogue that lists every tool the
  request may call. The query narrows the description the agent reads, not the
  set of names it can be offered.
- The catalogue the model sees and the catalogue `runtime_discover` reports
  are built at different moments and from different sets. The first is the
  request's authorised names. The second is everything the runtime holds,
  including the tools the deployment has switched off.
- A tool that is loaded can still be refused later, by the deployment's
  approval mode, by the sandbox mode, or by a hook. Loading a schema says the
  call may be attempted, not that it will run.
- The discovery step is checked by unit tests. Nothing in this release
  measures how often a model skips it, and no test drives a real provider
  through a multi-step discovery.
