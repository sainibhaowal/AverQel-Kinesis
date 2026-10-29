# 08. Model selection

A DeepSpace conversation runs on one model at a time. The deployment decides
whether DeepSpace may choose that model or whether the user names it. The
choice belongs to the conversation, and the conversation records it, so it
survives a restart.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [Two modes](#two-modes)
  - [The automatic rule](#the-automatic-rule)
  - [The manual override](#the-manual-override)
  - [The per-conversation record](#the-per-conversation-record)
  - [The subagent model list](#the-subagent-model-list)
  - [The "Let DeepSpace choose" switch](#the-let-deepspace-choose-switch)
- [What to watch for](#what-to-watch-for)

## Use case

Read this page when a user asks why a conversation ran on a particular model,
when you want a conversation pinned to a known model for a test or a
reproduction, or when you are writing a tool that starts a child agent and
need to know which models that child may run on.

## Why it matters

- The rule that picks a model is fixed and its result is written down, so the
  answer to "why this model" is a sentence rather than a guess.
- A user can override the choice for one conversation without changing the
  deployment or any other conversation.
- The choice is recorded in the typed log, so it cannot be changed out of
  band, and a later reader can see that the model was chosen rather than
  pinned.
- A deployment that does not want a model chosen on its behalf can say so,
  and the switch in the interface then refuses to turn on.

## How it works

### Two modes

| Mode | What decides the model |
|---|---|
| `auto` | The turn's own content decides. DeepSpace picks the model that fits the work. |
| `manual` | The conversation names the model. The deployment's assigned model is the fallback when the conversation names none. |

The deployment sets the default with two settings.

| Setting | Default | What it does |
|---|---|---|
| `deepspace_model_auto_choice_enabled` | on | Whether DeepSpace may choose the model at all. When it is off, every conversation runs in manual mode, even one that recorded `auto`. |
| `deepspace_model_selection_mode` | `auto` | The mode a conversation runs in when it has recorded none. |

When no database provider route is assigned, the deployment-level
`llm_provider`, `llm_model`, `llm_api_base_url`, and `llm_api_key` settings
form an ephemeral chat candidate. Explicit tenant, workspace, and
conversation assignments still take precedence. This fallback is useful for
headless and single-provider local runs; it does not create a provider row or
persist a secret.

The conversation's own recorded mode wins over the deployment's default, with
one exception: a deployment that has switched `deepspace_model_auto_choice_enabled`
off always runs conversations in manual mode.

### The automatic rule

The rule is applied to the list of models the deployment can offer. The list
is ordered first, and the order is fixed: a higher provider priority wins,
then a larger context window, then the provider id, then the model name. The
same turn therefore always produces the same choice.

The rule then narrows the list in four steps.

| Step | What it requires | What happens when no model qualifies |
|---|---|---|
| 1 | A turn that carries an image wants a model that accepts images. | The models that accept images stay. When none do, the reason says so and the list is unchanged. |
| 2 | The conversation has to fit in the model's context window. | The models that fit stay. When none do, the reason says so and the list is unchanged. |
| 3 | A turn that calls tools wants a model that supports tool calls. | The models that support tools stay. When none do, the reason says so and the list is unchanged. |
| 4 | The deployment's assigned model wins among the models that qualify. | The highest-priority model is chosen instead. |

A requirement that no model meets never empties the field. The models that
qualify for the earlier requirements stay, and the reason names the
requirement that could not be met.

The result carries a reason in plain words, for example
`accepts images; fits 8123 tokens; supports tools; the assigned model`.

What the turn tells the rule:

- The size of the conversation, in tokens, taken from the context the turn has
  already used.
- Whether the turn will call tools. A turn that offers any tool to the model
  counts as a turn that calls tools.
- Whether the turn carries an image.

### The manual override

The composer lists every model the caller's account may run a conversation on.
That list spans providers: a conversation may name a model of any provider the
tenant may use, not only the provider the workspace is assigned to.

A provider is refused when it belongs to another tenant, when it is disabled,
or when it does not serve chat.

The choice is per conversation. It is written to the typed log as a
`model/selection` event, and the payload carries the provider id, the model
name, and the mode.

| Route | What it does |
|---|---|
| `GET /api/v1/deepspace/chats/models` | Lists every model the caller's account may run a conversation on, each with its provider, its context window, and whether it accepts images and tool calls. |
| `GET /api/v1/deepspace/chats/{id}/model` | Reads the conversation's mode, the model it runs on, the model it recorded, and the deployment's default mode. |
| `PUT /api/v1/deepspace/chats/{id}/model` | Records the conversation's model, or switches it back to automatic with `{"mode": "auto"}`. |

The `PUT` route refuses a mode other than `auto` with a 400, and refuses
`auto` with a 403 when the deployment does not let DeepSpace choose.

### The per-conversation record

The last `model/selection` event decides the conversation's choice. Two shapes
are recorded.

| Payload | What it means |
|---|---|
| `{"mode": "manual", "provider_config_id": "...", "model_name": "..."}` | The conversation is pinned to a named model. |
| `{"mode": "auto"}` | The conversation lets DeepSpace choose. |

A payload that names a provider and a model but no mode is read as a manual
choice, so an older record still works.

### The subagent model list

A delegated child agent runs on its own model, named when the child is
started. The tool `list_subagent_models` reports which routes a child may run
under, and which models each route offers.

| Field | What it holds |
|---|---|
| `provider_id` | The provider the route belongs to. |
| `provider_type` | The provider's type, such as `openai`. |
| `model_name` | The model the route uses. |
| `models` | The chat models that provider offers, with the route's own model first. |

The routes come from the same provider resolution the child itself uses. A
child started with a provider and a model is matched against the same list of
candidates, and falls back to the first candidate when the match fails.

### The "Let DeepSpace choose" switch

The model picker in the composer carries a switch named **Let DeepSpace
choose**. With the switch on, the model is picked for each turn. With the
switch off, the conversation uses the model the user picked. The switch
reflects the conversation's recorded mode, and turning it on sends
`{"mode": "auto"}` to the conversation's model route.

The composer also shows the model in force and the reasoning effort.

## What to watch for

- A deployment that turns `deepspace_model_auto_choice_enabled` off cannot be
  overridden by the switch. The `PUT` route answers 403, and a conversation
  that recorded `auto` still runs in manual mode.
- The rule needs a context window to compare. A model whose window is unknown
  is treated as fitting any conversation, and a model whose published window
  cannot be verified is given the verified window for its name and provider
  type.
- A model that a provider no longer offers falls back to the deployment's
  choice. The turn does not fail on a model that is gone.
- The automatic choice is per turn, not per conversation. Two turns of the same
  conversation can run on different models when their content differs.
- The recorded manual choice is honoured even when the model belongs to a
  different provider than the one the workspace is assigned to. The provider
  still has to be one the tenant may use.
- The routes a child may run on come from the provider's published model list.
  A provider that publishes no list offers its own model and nothing else.
- The tests for the rule are unit tests. Nothing in this release proves the
  choice against a live provider, and no test compares two providers' answers
  for the same turn.
