# 12. Hooks

A hook is an external command that watches a tool call. The deployment
installs them, the runtime asks them about a call before it runs, and they
answer with a decision: allow the call, block it, or ask the user about it. A
hook is the deployment's own rule, written in whatever language the
deployment prefers, with no change to the product.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [The manifest](#the-manifest)
  - [The four events](#the-four-events)
  - [What a hook receives](#what-a-hook-receives)
  - [The two dialects](#the-two-dialects)
  - [Blocking](#blocking)
  - [Asking](#asking)
  - [The audit trail](#the-audit-trail)
  - [Where the gate sits](#where-the-gate-sits)
  - [Asking the hooks by hand](#asking-the-hooks-by-hand)
  - [A broken hook](#a-broken-hook)
- [What to watch for](#what-to-watch-for)

## Use case

Read this page when you want a rule of your own to run before a tool does
something: a path a deployment refuses to touch, a command that needs a
ticket, a tool that has to be reviewed by a person. The same page explains
what a broken hook does, which matters before you install one in a
production deployment.

## Why it matters

- A deployment can enforce a rule the product knows nothing about, without
  changing the product.
- A hook that refuses a call stops it before the tool runs, so nothing
  happens that the rule meant to prevent.
- A hook that asks does not refuse. The turn pauses and the user decides.
- A broken hook never stops the agent. A guard script that crashes, hangs, or
  answers with nonsense is ignored, and the call proceeds.
- Every invocation is written to the typed log, so a deployment can see which
  hooks ran, how long they took, and what they decided.

## How it works

### The manifest

The deployment names the hooks in one setting, `deepspace_hook_manifest`. The
value is a path. When the path is a directory, every `.json` file in it is
read, in name order. When the path is a file, that file is read. A manifest
that cannot be read leaves the hooks empty, so a deployment without hooks
behaves exactly as it did before.

The manifest is one JSON object with a `hooks` list.

```json
{
  "hooks": [
    {
      "id": "no-secrets",
      "event": "PreToolUse",
      "command": ["/opt/averqel/guards/no-secrets.py"],
      "timeout_seconds": 10,
      "matcher": "fs_write",
      "matcher_kind": "literal"
    }
  ]
}
```

| Field | What it means |
|---|---|
| `id` | The hook's name, unique inside the manifest. Required. |
| `event` | The moment the hook takes part in. Required. |
| `command` | The command to run, as a list of non-empty strings. Required. |
| `timeout_seconds` | How long the command may take. It defaults to 15 and may not exceed 600. |
| `matcher` | Which tool calls the hook applies to. An absent matcher and `*` apply to every call. |
| `matcher_kind` | `literal` for an exact tool name, `regex` for a pattern over the tool name. It defaults to `literal`. |

An entry that cannot be read is skipped, and the rest of the manifest is kept.
The first declaration of an id wins, so a second manifest cannot replace the
first one's hook. One manifest may declare at most 100 hooks, and the registry
holds at most 100.

### The four events

| Event | When it runs | What it may do |
|---|---|---|
| `PreToolUse` | Before a tool call. | Block the call, ask about it, or stop the agent. |
| `PostToolUse` | After a tool call. | Add context for the model, or a message for the user. |
| `Stop` | When the agent finishes a turn. | Add a message for the user, or stop the agent. |
| `SessionStart` | When a conversation starts. | Add context for the model. |

The names are the same ones a Claude Code or Codex guard script uses, so a
script written for one of those can be pointed at DeepSpace.

### What a hook receives

The runtime starts the command and writes one JSON object on its standard
input. The object always carries the event and the conversation id. It also
carries the tool and its arguments before a call, and the tool and its result
after a call.

| Event | The object carries |
|---|---|
| `PreToolUse` | `event`, `conversation_id`, `tool_name`, `arguments` |
| `PostToolUse` | `event`, `conversation_id`, `tool_name`, `result` |
| `Stop` | `event`, `conversation_id` |
| `SessionStart` | `event`, `conversation_id` |

The command's environment is small. It carries `PATH`, `HOME`, and `LANG`, and
a marker `DEEPSPACE_HOOK=1` that tells the script it is running inside
DeepSpace. It carries none of the deployment's secrets.

### The two dialects

The command answers with one JSON object on its standard output. Two spellings
are accepted.

DeepSpace's own dialect names the action directly.

```json
{"decision": "allow", "reason": "the path is inside the workspace"}
```

The Claude Code and Codex dialect names a permission decision inside
`hookSpecificOutput`.

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "no writes here"
  }
}
```

| Decision | DeepSpace dialect | Claude dialect | What it means |
|---|---|---|---|
| Allow | `{"decision": "allow"}` | `{"permissionDecision": "allow"}` or `approve` | The call may run. |
| Block | `{"decision": "block"}` | `{"permissionDecision": "deny"}` or `block` | The call does not run, and the reason is given to the model. |
| Ask | `{"decision": "ask"}` | `{"permissionDecision": "ask"}` | The call does not run for now. The user is asked. |

A hook that answers in both dialects is accepted, and the Claude shape wins.

The Claude dialect carries more than the decision. It can add a message for
the user with `systemMessage`, add context for the model with
`additionalContext`, replace the call's own arguments with `updatedInput`, and
stop the agent with `{"continue": false}` or a `stopReason`. The DeepSpace
dialect carries `additional_context` for the same purpose.

A hook can also block a call without answering at all. A command that exits
with status 2 and writes its reason on standard error blocks the call, and
the reason is what the model sees. This is only read when the Claude dialect
is accepted, which the setting `deepspace_hook_claude_dialect` controls and
which is on by default.

### Blocking

A blocked call never reaches the tool. The pipeline reports the denial, the
reason goes back to the model, and the model may try something else.

When several hooks apply to one call, their answers are folded together.
The fold does not depend on the order: a blocking hook wins over a stopping
hook, a stopping hook wins over an asking hook, and an asking hook wins over
an allowing one. The reason reported is the reason of the first hook that
reached the winning action.

### Asking

An asking hook does not refuse the call. The pipeline has no third answer, so
the gate reports the ask as a cancellation and marks it as needing approval.
The turn then stops with an approval request that carries the hook's own
question, so the user sees why the agent is waiting.

When the user approves, the call runs. The hooks are not asked about it a
second time: the approval is recorded as the signature of that exact call,
and a call with a signature that was already approved is allowed without
another question. An approval that asked again would never end.

### The audit trail

When `deepspace_hook_audit_enabled` is on, which it is by default, every
invocation writes two events to the typed log.

| Event | What it carries |
|---|---|
| `hook/invoked` | The hook's id, the event, the tool, and how long the hook took in milliseconds. |
| `hook/result` | The hook's id, the event, the tool, the decision, and the reason. |

The trail is best effort. A storage problem is logged and swallowed, because a
failure to write a record must never break the tool call it describes.

### Where the gate sits

The hook gate is one participant in the tool pipeline, registered under the
name `deepspace-hooks`. The pipeline runs its pre-dispatch participants in
order, and the product's own policy is the first of them. So a call is judged
in this order: the product's policy, then the hooks, then the tool.

The loop guard sits after both. It counts the calls a turn has made and
refuses the call that crosses a limit, and it counts only the calls that
reached it. A call the hooks blocked never reaches the guard, so it is not
counted. A call the guard refuses was already put to the hooks, because the
gate runs first.

### Asking the hooks by hand

The tool `hook_run` lets the agent put a call to the deployment's hooks
before it makes the call. It names the tool the agent intends to call, so the
hooks that filter on a tool name apply, and it reports the decision, the
reason, and the added context. It reads only, and it changes nothing: the
answer it returns does not block the call.

### A broken hook

A hook that fails never blocks the agent. The failure is logged and the call
is allowed.

| Failure | What happens |
|---|---|
| The command cannot be started | The failure is logged and the call is allowed. |
| The command does not finish in time | The command is killed, the failure is logged, and the call is allowed. |
| The command exits with a status other than 0 or 2 | The failure is logged and the call is allowed. |
| The command answers with more than 64 000 characters | The answer is discarded, the failure is logged, and the call is allowed. |
| The command answers with something that is not JSON, or with a decision that is not one of the three | The answer is discarded, the failure is logged, and the call is allowed. |

The audit trail still records the invocation and the result, so a hook that
keeps failing is visible in the log.

## What to watch for

- The command runs outside the operating-system cage. A hook is a deployment
  command, not a sandboxed tool, and it runs with the deployment's own
  permissions. A hook that must be contained needs to contain itself.
- The hook's environment carries only `PATH`, `HOME`, and `LANG`. A script
  that needs another variable, or a secret, will not find it.
- The command is started as a process and waited for. A hook that forks a
  child and leaves it running is not cleaned up.
- The manifest is read when the chat service is built. Changing the manifest
  file does not change the hooks of a running process, and a second manifest
  that declares an id already held is refused rather than replacing it.
- Exit status 2 blocks the call only while `deepspace_hook_claude_dialect` is
  on. With that setting off, a hook that only exits with 2 is treated as a
  failed hook and the call is allowed.
- The Claude dialect's `updatedInput` replaces the call's arguments. A hook
  that returns it can change what the tool does, including on a tool the
  deployment thinks it is guarding.
- The gate is wired into the tool pipeline for `PreToolUse`. The other three
  events are supported by the hook module and by `hook_run`, but the turn does
  not run `PostToolUse`, `Stop`, or `SessionStart` hooks on its own in this
  release.
- The hooks are checked by unit tests. Nothing in this release runs a hook
  command against a real deployment manifest outside the test suite.
