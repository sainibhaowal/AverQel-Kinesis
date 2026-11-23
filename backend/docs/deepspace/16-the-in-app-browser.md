# 16. The in-app browser

The in-app browser is a panel beside the chat. In the Electron desktop app it embeds a live Chromium page with normal pointer, keyboard, scrolling, responsive layout, and browser history. In a web deployment with the browser runtime enabled, the panel drives headless Chromium through its API and displays an interactive screenshot. Without that runtime, the web deployment falls back to a sandboxed frame and explains the limitation.

This chapter describes what the panel does in each case, which pages it opens
without asking, and the limits that come from what it is.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
 - [Where the panel lives](#where-the-panel-lives)
 - [The desktop browser](#the-desktop-browser)
 - [The browser runtime](#the-browser-runtime)
 - [The address bar](#the-address-bar)
 - [The navigation buttons](#the-navigation-buttons)
 - [The page controls](#the-page-controls)
 - [Which pages open without a prompt](#which-pages-open-without-a-prompt)
 - [The screenshot](#the-screenshot)
 - [The routes](#the-routes)
 - [The settings](#the-settings)
 - [The embedded view](#the-embedded-view)
- [What to watch for](#what-to-watch-for)

## Use case

- The agent starts a development server, and the user wants to see the page
 it serves without leaving the chat.
- The user wants to look at a page that runs on their own machine, such as
 the admin panel of a service running on `localhost`.
- The user wants to check a public documentation page while they talk to the
 agent about it.
- The user wants to open a page the agent mentions, in the same panel they
 use for the files.
- The user wants to open a site that refuses to be framed, which an embedded
 frame cannot show at all.

## Why it matters

- A site that refuses framing shows nothing in an embedded frame, and the
 panel cannot explain why.
- An embedded frame has no session of its own. It carries no cookies, it
 keeps no history, and the page it shows is not the page a browser would
 show.
- A history the panel keeps itself moves between the addresses the user
 typed. A redirect, a link inside the page, and a form that posts are steps
 in a browser's history, and a panel that keeps its own list loses them.
- A browser that opens every host without asking is a hole the user cannot
 see.

## How it works

### Where the panel lives

The side panel beside the DeepSpace chat has three tabs: **Files**,
**Terminal**, and **Browser**. The Browser tab holds the whole browser.

The Start screen of an empty conversation offers a **Browser** card, and
choosing it opens the side panel on that tab. The panel is a fixed-width
column beside the chat, so the chat keeps its own width while the panel is
open.

### The desktop browser

The Electron desktop app embeds its page with `WebContentsView`; the webpage
is real Chromium content, not a screenshot or an iframe. Pointer movement,
clicks, text selection, keyboard input, wheel scrolling, responsive layouts,
forms, and browser history work directly in the page. The address bar and
back, forward, and reload buttons control that same view.

The view lives in Electron's main process and is sized to the Browser tab's
visible content area. The renderer reports only its bounds and user-entered
HTTP(S) address through a narrow preload API. The main process accepts requests
only from the trusted Kinesis renderer, clamps the view to the app window, and
rejects non-HTTP(S) navigation and URLs containing credentials. New-window
links navigate in the same browser surface.

The remote page has no Node integration or app preload. It runs with context
isolation and Chromium sandboxing in the dedicated persistent
`persist:averqel-deepspace-browser` partition. Its cookies and site storage
belong to this local desktop browser profile, not to a DeepSpace tenant or the
Kinesis app's own renderer. User-entered sites can reach networks available
from the desktop host, including local services, as they can in a normal
desktop browser.

### The browser runtime

The runtime is the part of the server that owns a browser. It lives in
`backend/app/deepspace/services/browser_runtime.py`.

**One browser for the deployment.** A browser process costs hundreds of
megabytes, and the panel's pages do not need processes of their own. The
runtime starts one Chromium for the deployment and gives every conversation a
page target inside a browser context of its own. Two conversations therefore
have separate cookies and separate storage, while the host carries one
browser.

**How the browser is started.** The runtime starts the browser headless with
`--headless=new --no-sandbox --disable-gpu --remote-debugging-port=0` and a
temporary profile directory of its own. The browser writes the port it
listens on into `DevToolsActivePort` in that directory, and the runtime reads
the port from there.

**How the runtime talks to the browser.** The runtime opens the DevTools
WebSocket the browser published, creates a target with `Target.createTarget`,
attaches to it with `flatten: true`, and sends the page commands on that
attached session.

**A session belongs to one tenant, one user, and one conversation.** The
manager answers a request for a session that belongs to somebody else
exactly as it answers a request for a session that does not exist, so a
caller cannot learn that another tenant's session exists.

**The browser gets a reduced environment.** The process receives the paths it
needs and none of the deployment's secrets, in the same way a terminal
session's child receives a reduced environment. The browser's profile and its
scratch files live in a private directory of their own.

**The browser is stopped with the runtime.** The application stops the
browser when it shuts down, and the process also stops if the server process
ends without a shutdown, so no browser is left running.

**An idle session is closed.** A session that nobody uses for
`deepspace_browser_idle_timeout_seconds` is closed and its page is dropped,
the way an idle terminal session is. The panel opens a new session when the
user acts on a tab whose session was closed.

### The address bar

The address bar takes a full address and has a **Go** button. The empty state
reads "Enter an HTTP(S) address to start browsing" until a page is loaded.

Three things are refused, and each one says why in the panel.

| Input | What the panel says |
|---|---|
| Nothing | "Enter a URL to open." |
| Something that is not a URL | "Enter a full URL, for example http://localhost:3000." |
| A URL whose scheme is not `http` or `https` | "Only http:// and https:// URLs can be opened." |

The scheme rule is what keeps a `javascript:` address out of the browser. The
server applies the same rule, and refuses an address that carries a user name
or a password.

### The navigation buttons

Three buttons sit to the left of the address bar.

| Button | What it does |
|---|---|
| **Go back** | Moves to the earlier page in the browser's own history. |
| **Go forward** | Moves to the later page in the browser's own history. |
| **Reload the page** | Loads the current page again. |

**Go back** and **Go forward** are disabled when there is nowhere to go. The
reload button is disabled until a page is loaded.

The history is the browser's own. The runtime reads it with
`Page.getNavigationHistory` and moves through it with
`Page.navigateToHistoryEntry`, so a redirect, a link inside the page, and a
form that posts are steps in the history like any other step. The blank page
a session starts on is not a step the user can go back to.

### The web runtime page controls

These controls apply only to the web deployment's headless screenshot view.
The Electron desktop app uses the live page described above instead.

The screenshot is a button. Clicking it sends one click to the page at the point the user chose. Pressing Enter or Space on the focused screenshot clicks the centre of the page, so the control also works without a pointer.

The image shows a 1280 by 900 page viewport scaled to fit the panel with its aspect ratio kept. A click is measured against the part of the panel the image occupies, not the letterbox around it, and is then divided by the scale to produce the page coordinate. A click in the letterbox is ignored, and the server refuses a point outside the viewport.

A text field and a **Type** button insert text at the focused field of the page. Pressing Enter in the field does the same. The runtime inserts the whole text with `Input.insertText`; it does not press each character.

The key buttons press **Enter**, **Tab**, **Backspace**, and **Esc**. The runtime also accepts Delete, the four arrow keys, Home, End, PageUp, PageDown, and Space, with Control, Alt, Shift, or Meta held. A key or a modifier the runtime does not know is refused.

Two scroll buttons move the page from its centre. Each press moves 600 pixels, and one request may move at most 4000 pixels in either direction; a larger request is clamped.

### Which pages open without a prompt

The Electron desktop browser opens user-entered HTTP(S) sites directly. In the
web runtime and iframe fallback, the external-site control described below
still applies.

Loopback hosts and the app's own origin open without asking. The loopback
hosts are `localhost`, `127.0.0.1`, and `::1`.

Any other host waits for the **Allow external sites** checkbox. Until the
checkbox is ticked, the panel says "`<host>` is outside this app. Allow
external sites to open it." and the page does not open.

The checkbox is reset every time the address bar changes, so a permission
given for one address does not carry over to the next one.

The rule is deliberately narrow. A local development server is the common
case, and a page on the app's own origin is the app itself.

### The web runtime screenshot

Only the web deployment's headless browser path shows a picture of the page instead of embedding the page itself. The runtime asks Chromium for a PNG of its 1280 by 900 viewport.

In that web path, a click, text, key, or scroll is sent to Chromium and the next screenshot replaces the old one. It does not provide live pointer movement or hover. The desktop path does not use this screenshot control.

### The routes

The routes live under the conversation and carry the same answers.

| Route | Request | Answer |
|---|---|---|
| `POST /api/v1/deepspace/chats/{conversation_id}/browser/session` | none | Opens the conversation's session, or returns the one it holds. |
| `POST /api/v1/deepspace/chats/{conversation_id}/browser/navigate` | `{"url": "..."}` | Opens an address. |
| `POST /api/v1/deepspace/chats/{conversation_id}/browser/back` | none | Moves to the earlier page. |
| `POST /api/v1/deepspace/chats/{conversation_id}/browser/forward` | none | Moves to the later page. |
| `POST /api/v1/deepspace/chats/{conversation_id}/browser/reload` | none | Loads the page again. |
| `POST /api/v1/deepspace/chats/{conversation_id}/browser/click` | `{"x": 100, "y": 200}` | Clicks one point of the page. |
| `POST /api/v1/deepspace/chats/{conversation_id}/browser/type` | `{"text": "..."}` | Inserts text at the focused field. |
| `POST /api/v1/deepspace/chats/{conversation_id}/browser/key` | `{"key": "Enter", "modifiers": []}` | Presses one key. |
| `POST /api/v1/deepspace/chats/{conversation_id}/browser/scroll` | `{"delta_x": 0, "delta_y": 600}` | Scrolls the page. |
| `GET /api/v1/deepspace/chats/{conversation_id}/browser/screenshot` | none | Reads the current screenshot. |
| `DELETE /api/v1/deepspace/chats/{conversation_id}/browser/session` | none | Closes the session. |

Every route except the session route and the screenshot route accepts an
optional `session_id`, and a session that belongs to another tenant is
refused exactly as one that does not exist.

Every answer that carries a page has the same fields.

```json
{
 "session_id": "6f1c...",
 "url": "http://localhost:3000/docs",
 "title": "Docs",
 "screenshot_base64": "iVBORw0KGgo...",
 "can_go_back": true,
 "can_go_forward": false,
 "viewport_width": 1280,
 "viewport_height": 900
}
```

The routes answer with a reason when they refuse.

| Status | When |
|---|---|
| `400` | The address may not be opened, or the key or modifier is not supported. |
| `404` | The conversation holds no browser session. |
| `409` | The deployment has no browser runtime, or the host has no browser. |
| `422` | The request body is missing a field, or a number or text is outside its declared bounds. |
| `429` | The deployment already holds its maximum number of browser sessions. |
| `502` | The browser did not answer. |

### The settings

| Setting | Type | Default | What it does |
|---|---|---|---|
| `deepspace_browser_enabled` | `bool` | `false` | Whether the deployment runs a browser runtime. |
| `deepspace_browser_executable` | `str` | `""` | The path of the Chromium binary the runtime starts. An empty value makes the runtime look for a browser in the usual places. |
| `deepspace_browser_idle_timeout_seconds` | `float` (30-86 400) | `900.0` | How long an unused browser session stays alive before it is closed. |

The headless web runtime is off until a deployment turns it on. The local
single-machine deployment turns it on explicitly, in `backend/run_backend.py`,
and every other deployment has to make the choice itself. Electron desktop
browsing uses the app's own Chromium view and does not depend on this setting.

### The embedded view

A deployment without a browser runtime keeps the embedded view. The panel
says that the deployment has no browser runtime, and why, and then shows the
page in an `iframe` with `sandbox="allow-scripts allow-forms"`.

The frame does not carry `allow-same-origin`, so the framed page is given an
opaque origin of its own. It cannot read the app's storage, and it cannot
act as the signed-in user against the app's own origin.

The embedded view keeps the list of addresses it opened and an index into that
list. Going back and forward move the index, and the frame shows the address
at that index. The history lives in the panel's own state, and it is not kept
when the panel is closed and opened again.

The panel shows the loaded address at the bottom, together with an **Open in
new tab** link that opens the same address in a real browser tab.

## What to watch for

- The browser runtime is off by default. A deployment that has not turned it
 on keeps the embedded view, and a site that refuses framing still shows
 nothing.
- The host needs a Chromium. The runtime looks for `chromium`,
 `chromium-browser`, `google-chrome`, and `google-chrome` on the program
 path, then in the usual install directories, then in a Playwright or
 Puppeteer cache (Chrome for Testing). When it finds none, it says so and
 names the setting that points at one.
- A browser process costs memory. One browser serves the whole deployment,
 and one browser may hold a small number of sessions at a time.
- The headless web runtime sends a click, text, key, or scroll and shows the next screenshot. It does not provide pointer hover, downloads, file choosers, browser dialogs, or DevTools. The Electron desktop view supports direct pointer and keyboard browsing, but rejects non-HTTP(S) navigation and opens target-blank links in the same view.
- The runtime opens pages and reads them. It does not read the text of a page
 for the agent, and it is not a tool.
- A browser stops before it opens its debugging port when the path of its
 temporary directory is long. The runtime therefore keeps the profile in a
 short directory of its own.
- The panel is separate from the isolated renderer the web tools use. The
 `web_fetch` tool may call a separately deployed renderer
 when `deepspace_research_browser_enabled` is set, and that renderer is
 off by default. It is not the panel.
- The browser session belongs to the conversation, and it is closed when the
 conversation closes it or when it goes idle. The page it holds is not
 written to the session log, and nothing the page does is recorded.
