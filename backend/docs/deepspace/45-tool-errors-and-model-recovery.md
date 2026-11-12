# 44. Tool errors and model recovery

This chapter explains how DeepSpace reports a tool-operation problem without
turning it into a fake success or an unrelated chat-composer failure.

## Use case

- A filesystem search reaches a directory the operating-system user cannot
  read.
- A requested source is unavailable, a target does not exist, or a bounded
  tool call times out.
- A user declines an approval request and the model must explain what was not
  run.
- The provider, stream, durable runtime, or security boundary fails and a
  direct retryable system error is still required.

## Why it matters

A tool result is evidence for the model, not proof that the requested action
succeeded. Sending a bounded operation failure to the next model round lets
the model say what did and did not happen and offer a safe alternative. Sending
an orchestration or security failure to the model would hide a problem that
DeepSpace must surface directly.

Tool activity and the chat composer are separate surfaces. A recoverable tool
failure remains in the activity timeline and can be described in the assistant
answer. It does not create `message.error`, a terminal `error` SSE event, a
red system card, or a queue-pause banner.

## How it works

The execution path is:

```text
model tool call
  -> schema and policy validation
       -> invalid arguments: reject before execution, emit validation activity,
          pass bounded validation guidance to the next model round
       -> valid arguments: execute through ToolPipeline and return a bounded result
  -> next model round when safe
  -> ordinary assistant answer and done
```

For invalid arguments, DeepSpace removes the malformed tool call and its
validation result from the replayable tool exchange, then gives the model a
bounded system instruction with the validation details. The model can choose a
corrected or different authorized tool call. No browser or file operation is
started for the rejected call.

### Recoverable operation failures

Invalid tool arguments are rejected before the operation starts and returned
to the model as validation feedback. For example, `browser_navigate` refuses a
`file://` URL without starting a browser session. The model must use an
authorized file/document reader for local files; if a PDF has no extracted
Library text, it must ask the user to import or upload the document. It must
not retry the local path in the browser or claim to have read the PDF.

The chat service classifies these as recoverable when the provider and runtime
can still make a safe model request:

- operating-system permission denial or an unreadable path;
- protected or unsuitable target rejected by the tool;
- a user denial recorded by the approval flow;
- safe policy or hook denial;
- missing target, unavailable source, or bounded tool timeout;
- a bounded shared-execution busy result.

The result is attached to the original tool call id and includes a bounded
`error`, `error_category`, `recoverable`, and safe `recovery` instruction. The
instruction tells the model not to claim success, not to repeat a blocked call
indefinitely, and not to bypass the operating system or DeepSpace policy.
Permission guidance may suggest an allowed location such as `/home`, `/mnt`,
or `/media`, or ask the user to run a command and paste its output. DeepSpace
does not run `sudo`, change permissions, or broaden filesystem access.

The model may receive the failure, but the tool is not automatically replayed.
The per-turn recovery bound stops repeated recoverable failures before they can
create an unbounded model/tool loop.

### User-denied approval

When an approval is denied, the action body is never run. On a valid resume,
DeepSpace appends the original assistant tool call and a structured
`permission_denied` tool result, emits `permission_denied` and `tool_error`
activity, then lets the model answer. No hidden retry or approval bypass is
performed. If the saved approval data is incomplete, DeepSpace keeps the
existing blocked safety response.

### Fatal failures

Provider/model requests, broken streams, event or checkpoint persistence,
queue/worker failures, authentication or tenant-integrity failures, encryption
failures, cancellation, and unexpected internal runtime failures remain
terminal. They continue to use the existing terminal `error` event and retry
contract so the UI can show a real system failure rather than invented model
prose.

### Frontend behavior

`frontend/app/dashboard/deepspace/_lib/deepspace-thread.ts` treats
`tool_error` as activity and preserves its structured metadata. Only a terminal
`error` event sets `message.error` and `streamError`. Therefore a recoverable
tool failure can be visible in the activity panel while the assistant message
settles normally and the composer remains usable.

## What to watch for

- A model response after a failed tool is only as trustworthy as the returned
  tool evidence. It must say when the requested action did not complete.
- Full access still means the local OS account's permissions. It does not mean
  that `/proc`, `/sys`, `/dev`, `/run`, or other virtual/system trees should be
  scanned or that permissions should be escalated.
- A red terminal card is expected for provider, stream, persistence, queue,
  security, and other fatal failures. Suppressing those cards would make
  diagnosis and retry less reliable.
- The activity event is backward-compatible: existing clients can continue to
  consume `tool_error`, while newer clients can inspect `recoverable`,
  `error_category`, and `recovery`.
- Tests for this behavior use in-memory/fake providers and database-free
  fixtures. A real host permission boundary, provider outage, and queue
  deployment still require an isolated environment before release.
