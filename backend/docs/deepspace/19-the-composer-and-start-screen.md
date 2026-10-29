# 19. The composer and the Start screen

This is the whole DeepSpace interface, from the empty conversation to the
answer. The Start screen offers three ways into the tools, the side panel
holds the workspace files, a terminal, and a browser, and the composer row
holds every choice a turn depends on: the model, the reasoning effort, the
agent mode, the sandbox mode, the workspace, and the attachments.

## Use case

- A new user opens DeepSpace and needs to know what to do first.
- A user wants to see the files of a workspace without asking the agent.
- A user wants to run a command in a real shell and watch it run.
- A user wants to change the model, the agent mode, the sandbox mode, or the
  workspace mid-conversation and know that the change takes effect.
- A user wants to attach a file, a screenshot, a photo, or a Library file to
  a message.
- A user wants to know what the agent changed, approve a call, and manage the
  prompts queued behind the running one.

## Why it matters

- The model, the mode, the sandbox, and the workspace decide what the agent
  may do. A user who cannot see them cannot tell why an answer came out the
  way it did.
- The sandbox mode is a security control. A user who cannot see which mode
  is in force cannot tell whether a command ran contained.
- A destructive call that needs approval is only safe when the approval
  prompt says what will run. The card names the tool, the risk level, and
  the exact input.
- A second prompt typed while a turn runs must not be lost. The queue strip
  shows the pending prompts and what happened to each of them.

## How it works

### The Start screen

An empty conversation shows a section headed **Start** with three cards.

| Card | What it does |
|---|---|
| **Workspace files** | Opens the side panel on the Files tab, showing the workspace's own files. |
| **New terminal** | Opens the side panel on the Terminal tab and starts a real shell session in the workspace. |
| **Browser** | Opens the side panel on the Browser tab. |

The **New terminal** card carries a shell menu on its right with the supported
shell kinds `bash`, `pwsh`, and `cmd`. The Terminal tab asks the backend which
of those shells the current host can actually start, uses the host's preferred
default (`bash` on POSIX; PowerShell, then `cmd`, on Windows), and only enables
the returned choices. A choice whose executable is unavailable is refused by
the backend rather than silently replaced.

The three cards are disabled until the conversation exists. A conversation
that has not been created has no directory to list, no shell to run in, and
no workspace for the agent to work in.

### The side panel

The side panel is a single surface with three tabs, marked by a tab list
labelled **Conversation tools**.

| Tab | What it shows |
|---|---|
| **Files** | The files of the conversation's own workspace. Folders expand in place, a file opens with its content, and the path above the list is the directory being read. |
| **Terminal** | The live shell sessions of the conversation, their output, and the input box. |
| **Browser** | The page the deployment's browser is showing, or an embedded page view when the deployment runs no browser runtime. |

A button at the top of the panel closes it. The composer's panel toggle
opens it again.

The **Files** tab has a refresh button, labelled **Refresh the workspace
files**, and a close button on the file preview.

The **Browser** tab carries a back button, a forward button, a reload
button, an address field labelled **Browser URL** with the placeholder
**Enter an HTTP(S) address**, and a **Go** button. Below the field is a
checkbox labelled **Allow external sites**. A loaded page offers **Open in
new tab**.

A deployment that runs a browser runtime drives a real browser: the tab
opens a page, shows the browser's own screenshot of it, and the back and
forward buttons move through the browser's own history. A deployment that
runs no runtime says so in the panel, and shows the page in an embedded
frame instead. The embedded view keeps the pages it visited, so back and
forward work there too.

These rules decide whether an address opens:

- A loopback host and the app's own origin open without a prompt.
- Every other host is refused with the message `<host> is outside this app.
  Allow external sites to open it.` until the checkbox is ticked.
- Typing a new address clears the permission again.

The framed page runs without access to the app's own origin. A site that
refuses to be framed does not display in the embedded view.

### The composer row

The composer is a text field with a row of controls above it and a status
line below it.

#### Model picker and Let DeepSpace choose

The model button shows the model in force, or the text **Select Model** when
none is chosen. Opening it shows a menu with:

- A **Let DeepSpace choose** switch at the top, labelled **Let DeepSpace
  choose the model**. It reads "The model is picked for each turn." when it
  is on, and "This conversation uses the model you pick." when it is off.
- A search field with the placeholder **Search models...**.
- The models grouped by provider. Each group heading shows the provider
  name, its type, and how many models it holds, and expands to list them.
- The model marked with a check when it is the one in force.

The switch puts the deployment's `deepspace_model_selection_mode` in the
conversation's hands: `auto` picks a model per turn, and `manual` uses the
one the user picked. The choice is per conversation and is written to the
typed log as a `model/selection` event.

#### Reasoning effort

The **Think** button sits beside the model picker and is labelled **Choose
reasoning effort**. It shows one of:

- **Auto**, when the model provider's reasoning capabilities are not known.
- **On**, when the model always reasons and offers no separate levels.
- **Off**, when the model does not reason.
- The effort name, such as **Medium** or **High**.

The menu offers the same list, with **Provider default** standing in for
**Auto**. The button is disabled while a response is streaming.

#### Agent mode menu

The agent mode button is labelled **Agent mode** and shows the mode in
force. The menu lists every mode the deployment offers, each with its own
one-line explanation, and the mode in force is marked with a check.

| Mode | What it does |
|---|---|
| **Standard** | Every tool the policy allows, with delegation available and no programmatic calling. |
| **Programmatic tool calling** | The agent writes one program that calls the native tools as ordinary function calls. |
| **Minimal** | Read-only tools for a quick question, with no shell and no delegation. |
| **Creator** | Notes, Library files, artifacts, and memory, with no shell and no delegation. |
| **Custom** | Whatever the user configured, with the same shape. |

The mode in force is written to the typed log as a `preset/mode` event.

#### Sandbox mode menu

The sandbox mode is a select control labelled **Sandbox mode**, with a
shield icon beside it.

| Option | Value | What it allows |
|---|---|---|
| **Read-only** | `read-only` | The agent may read but not change the host. |
| **Workspace** | `workspace-write` | The agent may change files inside the workspace. This is the default. |
| **Full access** | `danger-full-access` | The checks are removed. |

The choice is per conversation and is written to the typed log as a
`sandbox/mode` event.

#### Workspace menu and Add workspace…

The workspace button is labelled **Workspace** and shows the workspace name.
The menu lists the workspaces the account holds, each with its path, and
marks the one in force. An item at the bottom reads **Add workspace…**; it
opens a field with the placeholder **Workspace name** and two buttons,
**Cancel** and **Add workspace**.

The choice is written to the typed log as a `workspace/selected` event, and
every file-touching tool runs in the workspace that is in force.

#### File, clipboard, camera, and Library buttons

Four buttons sit at the end of the row, after the menus. Each has a
tooltip.

| Button | Label | What it does |
|---|---|---|
| Paperclip | **Attach files from your device** | Opens the file picker. The tooltip reads "Attach files or paste a screenshot". |
| Clipboard | **Paste from the clipboard** | Reads the system clipboard. An image becomes an attachment, text becomes part of the message. |
| Camera | **Take a photo** | Opens the camera capture. |
| Folder | **Attach an existing Library file** | Opens the Library picker. The tooltip reads "Attach from Library". |

A browser that refuses the page access to the clipboard says so and asks the
user to paste instead. Dragging a file onto the composer and pressing
Ctrl+V work the same way as the paperclip button.

#### Plan mode

The plan mode button is labelled **Enter plan mode** when it is off and
**Leave plan mode** when it is on. Its tooltip reads "Plan mode. DeepSpace
designs the work and you approve the plan first."

While a conversation is in plan mode, the runtime offers read-only tools
plus the plan submission tool, and the plan is reviewed before any work
starts. The plan card in the thread carries the review buttons.

#### Agent tree

The agent tree button is labelled **Show the agent tree** and
**Hide the agent tree**. Its tooltip reads "Show the child agents of this
conversation".

The tree shows the child agents of the conversation and the shape of their
delegation, so a user can see which work ran in parallel and which agent
started it. The rows come from the conversation's projection endpoint and
carry the agent's label, mode, origin, status, depth, and turn count.

#### Panel toggle

The panel toggle is labelled **Show the workspace panel** and
**Hide the workspace panel**. Its tooltip reads "Show the workspace files, a
terminal, and the in-app browser".

#### The rest of the row

Two more controls sit on the right of the row:

- A voice control with a **Voice Dictation (STT)** button and a **Jarvis
  Mode** button.
- A status legend button labelled **Show DeepSpace status legend**, which
  explains what the composer's own icons mean.

### The status line

Under the text field, a button labelled **Show context usage details**
sits beside the numbers. The line reads the estimated context use, the
model's input and output token counts, the percentage of the window used,
and, while a response is streaming, the output speed in tokens per second.

### The text field

The placeholder reads **Message DeepSpace...**, or **Edit queued prompt…**
while a queued prompt is being edited. Enter sends the message and
Shift+Enter starts a new line. The field holds at most 4000 characters.

### Keyboard shortcuts

| Shortcut | What it does | Where it is handled |
|---|---|---|
| Ctrl+\ | Switches the split view on and off. It does not fire while the user is writing in a field. | `frontend/app/dashboard/deepspace/_components/DeepSpacePageClient.tsx` |
| Ctrl+Alt+Enter | Fills the screen with the workspace and leaves it again. The header button next to split view carries the same shortcut and shows a full-screen icon. | `frontend/app/dashboard/deepspace/_components/DeepSpacePageClient.tsx` |
| Ctrl+Alt+B | Collapses or opens the sidebar. | `frontend/app/dashboard/layout.tsx` |

The header buttons show the same shortcuts in their tooltips. The split view button, the full-screen button, and the collapse-sidebar button are the three that carry a shortcut.

### The thread

The thread is the conversation itself. It shows the user's messages, the
agent's answers, and the work between them. The work appears as steps: a
tool call and its result, a thinking panel, a task plan, an artifact, a
media block, and a chart or diagram when the answer carries one.

A completed reply also carries its measurements: the output speed, the
end-to-end time to the first visible answer text, and the provider's own
first-output time. The values are saved with the reply and are still there
when the conversation is reopened.

### The approval dialog

When a call needs the user's decision, an amber card appears in the thread
with the heading **Approval required**. The card carries:

- The message the runtime wrote, or the default "This action needs your
  approval before DeepSpace runs it."
- The MCP server name, the tool name, or **DeepSpace**.
- The tool the model asked for.
- The risk level, or **protected**.
- The exact `tool_input` as formatted JSON.
- Two buttons: **Approve** and **Deny**.

The card is answered through
`/api/v1/deepspace/chats/{conversation_id}/approvals/{approval_id}` with a
decision of `approved` or `denied`.

### The queue

A prompt typed while a turn runs is queued rather than lost. The composer
shows the queue in a strip above the text field.

- The strip reads **Queue active** or **Queue paused**, and a paused queue
  shows the reason.
- **Pause queue** stops new queued prompts from starting. A paused queue
  offers **Resume queue**, or **Continue queued prompts** when a queued
  prompt failed.
- The clear button is labelled **Clear pending queue**, and its tooltip
  says the chat history is kept.
- Each queued prompt shows its text and a status: **Queued**, **Running**,
  **Waiting for answer**, **Waiting for approval**, **Failed**, or
  **Stopping**.
- A queued prompt can be **Edit**ed, which keeps its place in line, moved
  ahead of ordinary items with **↳ Steer**, or removed.
- A failed prompt shows the error and a **Retry prompt** button. The retry
  resumes from the saved checkpoint when one is still there, and restarts
  the prompt from the beginning when it is not, so completed actions may
  repeat.

A failed prompt is not run again on its own. A failure or an interrupt
pauses the queue and keeps the items, and the failed prompt stays in the
strip with its error and its **Retry prompt** button.

### The notification of what changed

When the agent changed files in the workspace, a strip above the composer
reads **N files changed**, with the number of added lines in green and the
number of removed lines in red. A **Review** button opens the per-file
list, and the same button becomes **Hide review** while it is open.

The numbers come from the workspace diff, which compares the current files
against the state the turn started from.

## What to watch for

- The three Start cards stay disabled until the conversation exists. A
  conversation that was not created in the browser has no directory yet, so
  the cards do nothing.
- The sandbox mode select always offers the same three options, whatever the
  deployment allows. The effective mode for a call is the grant on that
  call when it has one, then the conversation's last recorded mode, then
  the deployment's default.
- **Full access** removes the containment checks. It is per conversation,
  so it lasts until the conversation is changed back.
- The model list shows the models the tenant may use, which is not only the
  provider the workspace is assigned to. A model that is not listed cannot
  be chosen from the picker.
- The reasoning effort list follows the selected model. A model whose
  reasoning capabilities the deployment has not verified shows **Auto**,
  and picking a level may not change what the model does.
- The clipboard button needs the browser to grant clipboard read access.
  Where it does not, the composer says so and asks for a paste.
- A very large image set can be limited, and a model that does not accept
  images receives the extracted or recognised text instead.
- The approval card only appears when a call actually needs a decision.
  With `deepspace_approval_mode` set to `never`, no card appears at all.
- The retry button restarts a prompt from the beginning when its
  checkpoint has expired, so an action that already ran may run again.
- The split-view shortcut does not fire while the user is writing in a
  field, so typing a backslash in a message does not change the layout.
- The panel's browser shows a screenshot of the page when the deployment
  runs a browser runtime, so the page cannot be clicked and a form in the
  page cannot be filled in. A deployment that runs no runtime keeps the
  embedded view, which has no session of its own, and a site that refuses
  framing does not display there.
