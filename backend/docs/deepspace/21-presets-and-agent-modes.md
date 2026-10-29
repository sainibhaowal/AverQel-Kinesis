# 21. Presets and agent modes

An agent mode is a named set of rules about how the agent runs: which tools it
may call, whether it may delegate, how deep it may delegate, and which sandbox
it starts in. DeepSpace ships five built-in modes and lets each user save
multiple named custom modes. The composer carries a menu that switches a
conversation between them, and the conversation records the mode it runs in.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
 - [The five modes](#the-five-modes)
 - [What each mode changes](#what-each-mode-changes)
 - [The tool lists](#the-tool-lists)
 - [The preset a conversation runs in](#the-preset-a-conversation-runs-in)
 - [The custom mode](#the-custom-mode)
 - [The menu in the composer](#the-menu-in-the-composer)
 - [How the mode changes the turn](#how-the-mode-changes-the-turn)
 - [Plan mode and the modes](#plan-mode-and-the-modes)
- [What to watch for](#what-to-watch-for)

## Use case

Read this page when a user asks what the difference is between Standard and
Minimal, when you want a conversation to start with a narrower tool set, or
when you are writing a deployment default that every new conversation starts
from.

## Why it matters

- A mode is a named, repeatable choice. A user does not have to remember a
 list of tools to withhold.
- The mode a conversation runs in is recorded, so the same conversation keeps
 the same mode after a restart and after an operator looks at the log.
- The mode's tool list is enforced at call time, so a tool that is not in the
 list is refused rather than merely undocumented.
- The deployment sets the default, so an installation can make every new
 conversation start in the same mode.

## How it works

### The five modes

| Mode | Name in the menu | What it is for |
|---|---|---|
| `standard` | Standard | Every tool the policy allows, with delegation available and no programmatic calling. The default. |
| `ptc` | Programmatic tool calling | The agent writes one program that calls the native tools as ordinary function calls. |
| `minimal` | Minimal | A quick question answered with read-only tools, with no shell and no delegation. |
| `creator` | Creator | Notes, Library files, artifacts, and memory, with no shell and no delegation. |
| `custom` | Custom | The built-in editable template for whatever the user configures. |
| `custom_<id>` | User-named custom mode | A saved user configuration that can be selected independently from other custom modes. |

### What each mode changes

| Mode | Tools | Programmatic calling | Delegation depth | Sandbox mode it starts in | Reasoning effort | Editable |
|---|---|---|---|---|---|---|
| `standard` | Every tool the policy allows, less the program tool | off | the deployment's | the deployment's | the deployment's | no |
| `ptc` | The native catalogue | on | 1 | the deployment's | `medium` | no |
| `minimal` | The read-only tools | off | 0 | `read-only` | the deployment's | no |
| `creator` | Notes, Library files, artifacts, memory, and the readers that go with them | off | 0 | the deployment's | the deployment's | no |
| `custom` | What the user configures | off by default | the deployment's | the deployment's | the deployment's | yes |
| `custom_<id>` | The saved user's configuration | saved value | saved value | saved value | saved value | yes |

The deployment's delegation depth is `deepspace_subagent_max_depth`, which
defaults to 3. The deployment's sandbox mode is `deepspace_sandbox_policy`, or
`deepspace_permission_mode` when the first is not set; it defaults to
`workspace-write`. The deployment's reasoning effort is
`deepspace_reasoning_effort`, or `deepspace_default_reasoning_effort`.

A mode may only narrow what the deployment allows. The delegation depth a
mode names is the smaller of the mode's depth and the deployment's, and the
sandbox mode a mode names is the strictest of the mode's mode and the
deployment's.

The settings page treats the four built-in modes as read-only catalogue
entries. Their cards expose **Mode details** and **How to use** guidance; only
the `custom` card exposes **Customize**. The server applies the same rule, so
settings supplied with a built-in mode are refused instead of being silently
ignored.

### The tool lists

- `standard` names no list of its own. It permits every tool the policy
 allows, which is the whole native catalogue plus the general-purpose tools
 the product registers, less the program tool, because programmatic calling
 is off.
- `minimal` permits the read-only tools: the native tools whose mode is
 `read`, together with the read-only general-purpose tools. `bash`, `pwsh`,
 `fs_write`, `fs_edit`, `fs_delete`, and `fs_str_replace` are all withheld.
- `creator` permits the content tools: the note, Library, memory, and artifact
 writers, the readers that go with them, the discovery pair, and the question
 tool. There is no shell, no delegation, and no programmatic calling.
- `ptc` permits the native catalogue, which keeps `run_code` in it. The model
 writes the program instead of calling the tools one at a time, and the
 program reaches the other native tools as ordinary function calls.
- `custom` starts with no list, which means the same as `standard`, and the
 user can narrow it by switching each available registered tool on or off in
 the settings page. An explicit empty list disables every optional tool; the
 discovery pair remains available so the agent can resolve the selected
 catalogue. Each selected tool still uses its existing DeepSpace validator,
 permission checks, runtime, and handler.
- A saved `custom_<id>` mode uses the same rules as `custom`, but its tool
 switches and runtime settings are stored separately. Users can create,
 modify, select, and delete several named custom modes.

### The preset a conversation runs in

A new conversation resolves its preset through a three-level fallback:

1. **Conversation's own record** - if the conversation recorded a preset via
 `PUT /api/v1/deepspace/chats/{id}/preset`, that preset is used.
2. **User's default** - if the conversation has no record, the user's default
 preset is used. The user sets this in the DeepSpace settings page; it is
 stored in the typed session log on a pseudo-conversation
 hidden settings conversation for that tenant/user pair and travels with any
 custom settings the user configured.
3. **Deployment default** - `deepspace_default_preset` (defaults to `standard`).

| Setting | Default | What it does |
|---|---|---|
| `deepspace_default_preset` | `standard` | The preset a conversation runs in when neither it nor the user has recorded one. |

The response of `GET /api/v1/deepspace/chats/presets/default` surfaces all three
levels and the recorded custom settings so the settings page can show the
deployment default separately from the user's choice and restore the custom
controls accurately.

| Route | What it does |
|---|---|
| `GET /api/v1/deepspace/chats/presets` | Lists the presets with their id, their name, their description, whether programmatic calling is on, whether a turn without an explicit plan-mode choice starts in plan mode, whether the user may edit them, the tools each one permits, and the sandbox mode, delegation depth, reasoning effort, and spill threshold each one resolves to. The response also carries the deployment's default. |
| `GET /api/v1/deepspace/chats/{id}/preset` | Reads the mode in force, the mode the conversation recorded, the effective custom settings (including settings inherited from the user's default), and the deployment's default. |
| `PUT /api/v1/deepspace/chats/{id}/preset` | Records the conversation's mode and returns the deployment default. A mode the catalogue does not hold is refused with a 400, and so is a setting the product does not accept. A mode the user has hidden is also refused. |
| `GET /api/v1/deepspace/chats/presets/default` | Reads the user's default preset (if any), the deployment default, and which level is currently in force for new conversations. |
| `PUT /api/v1/deepspace/chats/presets/default` | Records the user's default preset and returns the recorded settings. Optional `settings` object stores the custom preset settings so every new conversation that inherits the default also inherits the custom settings. A hidden mode or a built-in mode with settings is refused. |
| `POST /api/v1/deepspace/chats/presets/custom` | Creates a named custom preset for the authenticated user. Deployment-disabled tools cannot be enabled by the saved settings. |
| `PUT /api/v1/deepspace/chats/presets/custom/{id}` | Updates one saved custom preset's name, description, tool list, and runtime settings. |
| `DELETE /api/v1/deepspace/chats/presets/custom/{id}` | Deletes one saved custom preset. If it is the user's default, new conversations fall back to `standard`. |
| `GET /api/v1/deepspace/chats/presets/enablement` | Lists all presets with their per-user enabled flag. Absent rows are enabled by default. |
| `PUT /api/v1/deepspace/chats/presets/enablement` | Toggles a preset's availability in the mode selector. The deployment default cannot be hidden (returns 400). Hidden presets are removed from the composer's agent mode menu and cannot be recorded on a conversation. |

### Custom modes

The built-in `custom` mode is an editable template. A user can create several
named custom modes from it. Each saved mode has its own tool switches and
runtime settings; selecting one as the default applies it to new conversations,
while selecting it in an existing conversation records that mode for the
conversation.

| Setting | What it does |
|---|---|
| `name`, `description` | The label and the one-sentence description the menu shows. |
| `tools` | The registered tools the mode permits. An omitted list means every deployment-allowed tool; an empty list means no optional tools. |
| `ptc_enabled` | Whether the agent may write and run a program that calls the tools. |
| `subagent_max_depth` | How many levels of delegation the mode allows. |
| `plan_mode_by_default` | Whether a turn with no explicit plan-mode choice starts in plan mode. An explicit choice always wins. |
| `sandbox_mode` | The boundary the mode starts in. |
| `reasoning_effort` | The reasoning effort the mode requests. |
| `spill_threshold_chars` | The result size above which a tool result spills. |

A setting the product does not accept is refused with a 400, and the saved mode
is left alone. A tool disabled by deployment remains off even when a user
switches it on in a custom mode.

### The menu in the composer

The composer carries an **agent mode** menu. It shows the enabled modes with
each mode's own description, and it marks the mode the conversation runs in.
Choosing a mode sends the preset id to the conversation's preset route, and
the mode takes effect on the next turn.

A mode the user has hidden via `PUT /api/v1/deepspace/chats/presets/enablement`
does not appear in the menu and cannot be recorded on a conversation. The
deployment default is always visible and cannot be hidden.

### How the mode changes the turn

The turn applies the mode's tool list in two places at once.

1. The list narrows the set of names the discovery step offers, so a withheld
 tool never reaches the catalogue the model sees.
2. The list is the policy's allowlist for the turn, so a call to a withheld
 tool is refused with the reason that the tool is not part of the agent
 preset this conversation runs.

Both are replaced on every turn, so a mode that is changed takes effect on
the next turn and a mode that is removed stops withholding anything.

The rest of the mode is applied in the same place.

| Value | How the turn applies it |
|---|---|
| Programmatic calling | When the mode does not ask for it, the tool that writes a program is withheld, so the model cannot reach the tools through a program. |
| Delegation depth | The depth is the smaller of the mode's depth and the deployment's. A call that would start a child deeper than that is refused, and a child at the limit is offered no delegation tools. |
| Sandbox mode | The boundary the turn runs in is the strictest of the mode's mode, the conversation's own `sandbox/mode` record, and the deployment's mode. |
| Reasoning effort | The mode's effort applies when the request names none. A request that names an effort keeps it. |
| Spill threshold | The mode's threshold applies when it names one, so a mode may spill a result earlier than the deployment would. |

A delegated child inherits the mode its parent runs in, so a mode that
withholds the write tools withholds them from the child too. Chapter 04
covers the child case.

### Plan mode and the modes

No built-in mode starts a turn in plan mode. A custom mode may request that
turns without an explicit plan-mode choice start there. An explicit choice in
the composer always wins; plan mode remains a separate per-conversation
switch recorded as a `plan/mode` event and applied on top of the agent mode.
While it is on, the turn offers the read-only tools plus the plan tools and
withholds every write tool.

## What to watch for

- A mode that names a tool list keeps the two discovery tools, `discover_tools` and
 `get_tool_result`, whatever else it withholds. They are how the model finds
 out which tools the mode allows, so withholding them would leave the mode
 with no way to load a schema.
- Changing the mode does not change the tools a deployment switched off. The
 deployment's own settings are applied first and a mode can only narrow what
 is left.
- A mode is per conversation. Two conversations of the same user may run in
 different modes, and the change is recorded rather than applied in place.
- The tightest boundary wins. A conversation that records a wider sandbox
 mode than the deployment allows still runs in the deployment's mode, and a
 mode that names a wider mode than the deployment allows is narrowed to it.
- Custom modes are editable. Each saved mode's settings are stored separately,
 read back at the start of the next turn, and applied. A custom mode that
 omits its tool list is the same as `standard`; an explicit empty list is
 narrower and leaves only the discovery pair.
- The user's default preset and its custom settings are inherited by every
 new conversation that has not recorded its own preset. Changing the user
 default does not retroactively update existing conversations.
- A hidden preset is removed from the composer's agent mode menu and cannot
 be recorded on a conversation. The deployment default cannot be hidden.
- Attempting to record a hidden preset on a conversation via
 `PUT /api/v1/deepspace/chats/{id}/preset` returns 400.
- The modes are checked by unit tests. Nothing in this release measures how
 often a user picks one, and no test compares two modes in a live
 conversation.
