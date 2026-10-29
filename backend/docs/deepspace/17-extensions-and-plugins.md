# 17. Extensions and plugins

An extension is a manifest that changes how the agent works. A plugin is a
manifest that may also carry code. This chapter describes both, the places
they are read from, the jail that extension code runs in, and the switch that
turns extension code off.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [Extensions](#extensions)
  - [The extension tools](#the-extension-tools)
  - [Plugins](#plugins)
  - [The four sources](#the-four-sources)
  - [The manifest](#the-manifest)
  - [The handler kinds](#the-handler-kinds)
  - [The jail](#the-jail)
  - [The switch](#the-switch)
  - [The per-conversation scope](#the-per-conversation-scope)
  - [The plugin tools](#the-plugin-tools)
- [What to watch for](#what-to-watch-for)

## Use case

- A team wants the agent to answer in a house style, and no one wants to
  change the product for it.
- A team wants one tool of their own without a release of the product.
- A user wants the agent to call `fs_read` under a shorter name with a fixed
  argument.
- A deployment wants the agent to stop calling one particular tool, for one
  workspace only.

## Why it matters

- Every request that needs a new tool otherwise waits for a product change.
- Code that runs inside the server process can read the server's memory and
  the server's environment.
- An extension that can widen the deployment's own policy is a way around
  the policy.
- A manifest that is not read on every turn is a manifest the deployment
  cannot rely on.

## How it works

### Extensions

An extension is declarative. It is a JSON document in the conversation's
workspace, under `.deepspace/extensions/`, and its file name is its
extension name in lower case with a `.json` suffix.

| Field | What it does |
|---|---|
| `name` | The extension's name, unique in the workspace. |
| `description` | What the extension changes. |
| `guidance` | Text added to the system prompt while the extension is installed. |
| `aliases` | A mapping from a name the model uses onto a tool it already has. |
| `deny_tools` | Tools the extension refuses. |
| `approval_tools` | Tools the extension always asks the user about. |

The limits on a manifest are fixed. A name may hold letters, digits, dashes,
and underscores, and may not exceed 64 characters. Guidance may not exceed
4 000 characters. A manifest may not exceed 20 000 characters. One
conversation may hold 40 extensions, and one extension may declare 20
aliases.

A manifest that cannot be read or cannot be parsed is skipped rather than
failing the turn, because one broken file must not take the conversation
down.

An alias fixes some of the target tool's arguments. The fixed arguments are
hidden from the model and merged in when the call runs, so the model cannot
change them.

The extensions a conversation runs with are the ones its own workspace holds
and the ones the deployment installed. A conversation that works in a named
workspace runs that workspace's extensions. The deployment's extensions live
in the directory `deepspace_extensions_dir` names, and every conversation
runs them; an empty value uses `<local_storage_path>/extensions`. A name
that both sides declare is taken from the conversation's own copy, because
the conversation's scope is the narrower one. The tools the extensions
refuse and the tools they always ask about are the union of both sides, so
a deployment's restriction is never lost.

The deployment's own policy always wins. An extension may narrow what the
agent may do, and it may add guidance, but it cannot widen the
deployment's approval mode, its sandbox policy, or its tool allowlist.

### The extension tools

| Tool | Mode | What it does |
|---|---|---|
| `extension_list` | read | Lists the extensions the conversation runs with. |
| `extension_write` | write | Installs or changes one extension. |
| `extension_remove` | write | Removes one extension. |
| `plugin_install` | write | Installs a plugin into the conversation's workspace. |
| `plugin_manager` | write | Lists the plugins and switches one on or off. |
| `runtime_discover` | read | Reports what the runtime can do. |

`extension_write` applies its guidance from the next turn, and its tool
restrictions immediately. `extension_remove` says that the guidance no longer
applies.

`runtime_discover` takes a `kind` of `tools`, `plugins`, `extensions`, or
`routes`, or none at all for all of them. It only reads, and it never runs a
plugin, a command, or a tool.

### Plugins

A plugin is a unit of agent behaviour that the deployment can add without
changing the product. It declares tools the model may call, guidance the
model reads, and a policy its calls run under. The product's own behaviour
stays in the product, and a plugin adds to it.

A plugin's manifest is data, and it is validated before the product reads a
single field of it. The file name is `deepspace-plugin.json`, and a
workspace plugin lives in `<workspace>/.deepspace/plugins/<name>/`.

The registry also publishes one built-in native adapter for every tool in
DeepSpace's typed tool registry. These adapters are generated from the real
provider schema, execution mode, validator, and dispatcher entry; they do
not add a second implementation. Their ids use `native.<tool-name>`, and the
inventory marks them with handler kind `native`.

The settings inventory keeps the product runtime separate from optional
extensions. The agent loop, session log, tool policy, sandbox, presets,
workers, MCP bridge, and plugin registry are core DeepSpace capabilities; they
are not counted as user-installed plugins. The inventory reports those core
capabilities separately, then reports registered extensions and the tools
those extensions expose. A preset count describes the extension tools
represented by that inventory, not every dynamic MCP or workspace tool a
conversation may resolve later.

### The four sources

| Source | Where it comes from |
|---|---|
| `builtin` | The plugins that ship with the product. |
| `deployment` | The directory `deepspace_plugin_directory` names. |
| `package` | An installed Python package that declares a `deepspace.plugins` entry point. |
| `workspace` | The conversation's own workspace, at `.deepspace/plugins/<name>/deepspace-plugin.json`. |

A plugin found in a workspace is offered to that conversation only. The
other three sources are process-wide, so the registry holds them.

`deepspace_plugins_enabled` turns the plugin system off. It is on by
default.

### The manifest

| Field | What it is |
|---|---|
| `id` | The plugin's identifier. |
| `name` | The plugin's human name. |
| `version` | The plugin's version. |
| `description` | What the plugin does. |
| `guidance` | The instructions the plugin adds to the system prompt. |
| `tools` | The tools the plugin offers, each with a name, a description, a mode, and a parameter schema. |
| `allow_tools` | The tool names the plugin permits. Empty means no extra permission. |
| `deny_tools` | The tool names the plugin refuses. These always win. |
| `handler` | The handler, written as `module:function`. A command handler holds the command line here. |
| `handler_module` | The workspace file that holds the handler's module. |
| `handler_kind` | `python` or `command` for extension code. Generated built-in adapters use `native`. |
| `handler_timeout_seconds` | The longest a command handler may take. |
| `command` | The command that answers a tool call over stdio JSON-RPC. |
| `settings` | The plugin's own settings and their defaults. |
| `enabled_by_default` | Whether a conversation runs the plugin when the deployment has no opinion. |

The tool modes a plugin may declare are `read`, `write`, and `interactive`.
A plugin's tool is a native tool for the policy, so it is judged by the
same rules as the product's own tools.

The manifest may not exceed 256 000 bytes.

### The handler kinds

A plugin that carries behaviour points at a handler in one of three ways.

| Kind | How the handler is reached | Where it runs |
|---|---|---|
| An installed Python module | `module:function` is imported and called with the same arguments a native tool handler receives. | In the server process. |
| A command over JSON-RPC | The product sends one `tools/call` request on standard input and reads one response line. | In its own process, inside the cage. |
| A workspace handler | A Python module the workspace holds, named by `handler_module`, or a command line with `handler_kind` `command`. | In its own process, inside the cage. |
| A native adapter | Selects the existing DeepSpace registry entry by its tool name. It never calls the external plugin handler path. | In the normal DeepSpace runtime, with its existing validator, policy, permissions, and dispatcher. |

A `handler_module` path that leaves the workspace is refused before the file
is looked for. The module is loaded from the workspace file itself, so an
installed package with the same name never answers for it.

The stdio command is started when the plugin is called, and the ordinary
tool path closes it when the call ends. A command that cannot start, or that
answers with a malformed response, fails the call with a typed error instead
of hanging.

### The jail

Extension code runs in **its own process**, inside the deployment's
operating-system cage, with a reduced environment, under a timeout.

| Part of the jail | What it is |
|---|---|
| Its own process | The handler is imported by a small runner program that lives in the cage, so it never shares a process with the server. |
| Its own cage | The cage is the deployment's own: bubblewrap, Landlock, the macOS seatbelt profile, or a container. |
| The reduced environment | `plugin_environment` carries `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `SYSTEMROOT`, `TEMP`, and `TMP`, with `HOME` set to the workspace and `DEEPSPACE_PLUGIN=1` added. No deployment secret is in it. |
| The system interpreter | The handler runs under the system `python3`, not the deployment's own interpreter, because the cage does not mount the deployment's directories. On Windows the deployment's own interpreter runs it, because Windows has no such mount. |
| The timeout | The manifest's `handler_timeout_seconds`, 60 seconds by default and 300 at most. A handler that overruns is stopped with its process group. The stdio command has no such timeout. |

The arguments reach the handler as JSON on standard input and the result
leaves as JSON on standard output, so a handler that hangs, crashes, or
answers with something unreadable fails its own call instead of the server.

The conversation's containment mode decides how much the cage gives. A
read-only conversation gets a read-only workspace. A conversation that asked
for full access runs without a cage, exactly as its shell commands do.

A host that offers no cage refuses the call with the message "This host
offers no sandbox, so extension code is not run. Install bubblewrap, or turn
extension code off with `deepspace_plugin_code_enabled`."

The jail is proved on the host that runs it. The checks live in
`backend/tests/unit/test_deepspace_sandboxed_extensions.py` and confirm that
the handler runs in its own process, inside the cage, with no deployment
secret in its environment, and that it is stopped when it runs past its
budget. On a host that cannot create the cage those checks skip with the
reason.

### The switch

`deepspace_plugin_code_enabled` turns extension code off entirely. It is on
by default.

When it is off, a plugin the workspace holds does not run, and a command
handler does not run whatever its source. Python code the deployment shipped
or installed itself stays allowed, because the deployment already trusts it.

`plugin_install` refuses the install when the manifest would run code and the
deployment refuses it, with the message "This deployment does not run
extension code."

### The per-conversation scope

Which plugins a conversation runs is a per-conversation state. It is
recorded in the typed session log as a `plugins/enabled` event, so it
survives a restart and cannot be changed out of band.

The routes are `GET` and `PUT /api/v1/deepspace/chats/{conversation_id}/plugins`,
and `GET /api/v1/deepspace/chats/plugins` lists the plugins the deployment
can offer.

The settings page consumes the same catalogue route. Its `global_plugins`
plane contains every process-wide declaration, including disabled declarations
and registry errors. Its `session_presets` plane resolves the plugin tools
against each agent preset's tool allowlist, so the displayed session rows are
the rows that preset can expose on a turn. Inventory membership does not
enable a plugin: conversation activation remains the recorded
`plugins/enabled` state and is still constrained by deployment policy.

Native adapters follow the same preset allowlists as extension plugins. The
Standard and Custom modes expose the full registered catalogue, PTC exposes
the native catalogue plus its discovery pair, Minimal exposes read-mode
tools, and Creator exposes its content-tool allowlist. Turning the extension
plugin system off removes extension plugins but does not remove DeepSpace's
original native tools.

The deployment's policy always wins. A plugin the deployment disabled cannot
be switched on from a conversation, and a plugin's own deny list always wins
over its allow list.

### The plugin tools

`plugin_manager` takes an `action` of `list`, `enable`, `disable`, or
`remove`, and a `plugin_id` for every action except `list`. The `remove`
action drops a plugin from the deployment for good, and the registry keeps
that choice across a reload.

`plugin_install` takes a `name`, and optionally a `description`, a
`guidance`, a `handler`, a `handler_module`, a `handler_kind`, a
`handler_timeout_seconds`, a list of `tools`, and an `enabled_by_default`
flag. It writes the manifest into the conversation's workspace and the
plugin runs from the next turn. The handler file has to be written into the
workspace first, with `fs_write`.

## What to watch for

- The extension jail is proved on the host that runs it. The macOS and
  Windows cages it uses are unit-tested only, so the jail has not been run
  against a real macOS host or a real Windows host in this repository.
- A handler that lives in an installed Python module runs in the server
  process, not in the cage. The workspace handler, the command handler, and
  the stdio command are the three that are jailed.
- A deployment that sets `deepspace_plugin_code_enabled` to false still runs
  the Python code it shipped or installed itself. A command handler is
  refused whatever its source.
- The reduced environment carries no credential, so a handler that needs an
  API key cannot have one. Give it a tool that fetches what it needs.
- The manifest timeout bounds one call. A handler that starts a background
  process outlives the call.
- The stdio command is started and closed around each call, so a plugin that
  keeps state between calls does not keep it.
- A broken extension manifest is skipped, so a typo in one file shows up as
  a missing extension rather than as an error.
- An extension's guidance applies from the next turn, and the model has
  already read the earlier instructions.
- A workspace plugin is offered to the conversation that installed it. It is
  not offered to another conversation in the same account.
- The registry keeps the deployment's own disabled and removed plugins, and
  both survive a reload. A removed plugin does not come back on its own.
