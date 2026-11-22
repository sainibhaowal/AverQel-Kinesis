# AverQel Kinesis Electron Desktop

The AverQel Kinesis desktop app bundles Electron, the shared static Next.js
frontend, and a frozen FastAPI/Python backend. In an installed app, Electron
starts the backend on loopback port `11001`, waits for its health check, then
opens the UI; closing the app stops that backend. No separate Python service,
hosted API server, or manual port startup is required.

The app uses product ID `com.averqel.kinesis` and one app-owned data home,
`~/.averqel-harness`. Chats, sessions, SQLite databases, logs, encrypted
secrets, AverQel-downloaded embedding/reranker models, Hugging Face cache,
and Electron profile live below that directory. The default project workspace
is `~/Kinesis` itself. On first use, old default-workspace files are moved
there when safe; conflicts remain in the old folder. If symlinks exist, regular
files are copied and the old folder stays intact so relative links keep working.
Files from the previous app-data workspace are copied once without deleting
the source. Named-workspace registrations live under
`~/.averqel-harness/workspaces` and are moved from the old Kinesis ID folders
only when all contents can be preserved there; otherwise the old catalogue
remains active. A project folder the user selects stays at its chosen location.
Development uses API port `11000` and renderer port `11030`; packaged builds
use API port `11001` and a private loopback renderer.

## Development

Start the local backend, frontend development server, and Electron together with
one command:

```bash
node scripts/electron.mjs dev
```

The desktop shell allows one Electron instance per user profile. Starting the
development command again focuses the existing window instead of creating a
second renderer.

On Linux Wayland sessions, the shell disables Chromium Vulkan rendering because
Electron 44 reports that Vulkan is incompatible with the active Wayland
compositor; normal Chromium compositing remains enabled.

Run the desktop's database-free source-resolution tests with:

```bash
pnpm --dir applications/desktop test
```

By default Electron opens the local frontend at `http://127.0.0.1:11030` and
uses the development API at `http://127.0.0.1:11000/api/v1`. `ELECTRON_START_URL`
may point only to a loopback or `.localhost` development origin; non-local
origins are rejected.

The frontend runtime file at `frontend/.local/.env.local` supplies local
defaults. Values explicitly provided by the desktop launcher take precedence,
so the development shell keeps its `11000` API and `11030` renderer ports even
when the same checkout was previously used for the web profile on ports
`10000` and `10030`. Restart the desktop command after changing launcher or
runtime environment values.

Local provider API keys are encrypted with a persistent AES keyring at
`~/.averqel-harness/secrets/provider-secret-keyring.json`, created with mode
`0600` on first local startup. Production deployments must provide their own
`AKS_PROVIDER_SECRET_ACTIVE_KID` and `AKS_PROVIDER_SECRET_KEYRING_JSON` (or
the AWS KMS settings) instead of relying on this local keyring.

The renderer uses a restrictive Content Security Policy. Next.js bootstrap
scripts may use inline execution, but dynamic code evaluation is disabled in
development and packaged Electron builds alike. Loopback
HTTP/WebSocket connections are limited to local development ports and remote
connections require HTTPS/WSS.

## Right-click menu and selection actions

Right-clicking in the window opens a native menu instead of the Chromium
default. The menu adapts to what was clicked:

| Context | Actions |
|---|---|
| An editable field | Undo, Redo, Cut, Copy, Paste, Delete, Select All |
| Selected page text | Ask DeepSeek, Copy |
| A misspelled word | Up to five spelling suggestions, Add to Dictionary |
| A link | Open Link in Browser, Copy Link Address |
| An image | Copy Image, Copy Image Address, Save Image As… |
| Every context | Reload, Force Reload |

**Ask DeepSeek** appears only when text is selected while DeepSpace's
composer is mounted. It sends the selected passage to the DeepSpace renderer,
which adds it to the composer as a block quote and focuses the composer. It
never sends the turn automatically. The same action is available from the
floating button that DeepSpace shows over a text selection.

Reload and Force Reload are always present, so the existing recovery actions
remain available from any right-click. Inspect Element and Toggle Developer
Tools are appended only while the shell allows developer tools.

## Packaging

Build the Python backend on the target OS because the freezer and native
machine-learning libraries must match it. The build script requires a Python
3.12 interpreter and creates or reuses an isolated environment only when that
environment also reports Python 3.12; it does not replace a mismatched existing
build environment automatically. Set `AVERQEL_BUILD_PYTHON_ENV` to a new
environment path to preserve and work around an existing mismatched environment.
It installs backend and freezer dependencies, freezes the API, builds the static
frontend, then embeds both in Electron. On Windows,
the frontend build starts the `pnpm.cmd` shim through `ComSpec`; Unix builds
start `pnpm` directly.

```bash
pnpm --dir applications/desktop build:linux:x64
pnpm --dir applications/desktop build:linux:arm64
pnpm --dir applications/desktop build:windows:x64
pnpm --dir applications/desktop build:windows:arm64
pnpm --dir applications/desktop build:mac:x64
pnpm --dir applications/desktop build:mac:arm64
```

Linux commands produce `.deb`, `.rpm`, and `.AppImage`; Windows commands
produce NSIS `.exe` installers; macOS commands produce `.dmg` images. Build
The Windows ARM64 installer uses a native ARM64 Electron shell and an x64
Python/AI backend, because upstream PyTorch does not publish a Windows ARM64
wheel. Windows 11 runs that backend through its built-in x64 emulation; this
preserves Hugging Face model downloads and inference but is slower than a native
ARM inference runtime. Intel Macs use the last supported Intel PyTorch line
(2.2.2) and ONNX Runtime 1.23.2. Their native CI runner builds the current
cryptography release from source with Rust and OpenSSL because upstream no
longer publishes Intel-Mac wheels. Other desktop builds use the current runtime
pins. Windows builds use portable upload-signature detection and exclude the
native `python-magic` module because its Windows loader crashes during
PyInstaller dependency scanning. Linux and macOS retain libmagic detection.
The manual GitHub workflow uploads each installer format and architecture as a
separate workflow artifact; it does not combine Linux packages with each other
or with Windows/macOS installers, and it does not publish a release. Artifact
names follow `AverQel-Kinesis-<platform>-<architecture>-<format>`:

| Platform | Architecture | Separate artifact |
| --- | --- | --- |
| Linux | x64 | `AverQel-Kinesis-linux-x64-deb` |
| Linux | x64 | `AverQel-Kinesis-linux-x64-rpm` |
| Linux | x64 | `AverQel-Kinesis-linux-x64-AppImage` |
| Linux | ARM64 | `AverQel-Kinesis-linux-arm64-deb` |
| Linux | ARM64 | `AverQel-Kinesis-linux-arm64-rpm` |
| Linux | ARM64 | `AverQel-Kinesis-linux-arm64-AppImage` |
| Windows | x64 | `AverQel-Kinesis-windows-x64-exe` |
| Windows | ARM64 | `AverQel-Kinesis-windows-arm64-exe` |
| macOS | x64 | `AverQel-Kinesis-macos-x64-dmg` |
| macOS | ARM64 | `AverQel-Kinesis-macos-arm64-dmg` |

This produces ten independently downloadable artifacts per workflow run.
Packages are large because they include the local API and ML runtime, and are
currently unsigned.

Pull requests run the Electron shell tests and a Linux packaged-backend smoke
test through `.github/workflows/ci.yml`. That merge gate verifies that the
frozen local API starts, reports readiness, serves the expected OpenAPI routes,
and creates its isolated SQLite/storage directories. It does not replace the
manual native packaging matrix above.

Desktop builds bundle the Playwright Python package and Chromium for browser
automation, and the frozen runtime manifest verifies Playwright is present.
Linux `.deb` and `.rpm` packages declare `xdotool`, screenshot, OCR, virtual
display, bubblewrap, and `prlimit` dependencies for desktop automation and
isolated execution. AppImage users need the corresponding host tools installed.
The runtime status page reports missing automation dependencies and
computer-use readiness per operation. The standalone desktop enables
`sandbox_execute` locally: the packaged backend runs Python through its own
frozen runtime, and supported languages execute in a private workspace with
network isolation and bounded resources. No host Python or remote executor is
required. A real display and native packages remain OS prerequisites for
computer use. Browser launch can also depend on native OS libraries available
on the target machine; the build smoke test does not yet exercise a real
Chromium launch.

### AverQel Kinesis product mark

The canonical icon is `frontend/public/brand/AntiAtom_Icon.svg`. The frontend
uses it on the landing, authentication, onboarding, dashboard, and Electron
title-bar surfaces. The title bar loads that same SVG from the local frontend.
The checked-in `applications/desktop/assets/icon.png`, `icon.ico`, and
`icon.icns` are generated from the canonical SVG, trimmed to the artwork, and
centered and optically enlarged vertically for better small-size visibility.
They are embedded in Linux,
Windows, and macOS builds respectively. Regenerate them with
`python applications/desktop/scripts/generate-desktop-icons.py`; this
maintenance command requires ImageMagick (`magick`) and is not needed when
building the app from the checked-in icon files.

The visible wordmark keeps the original `AverQel` artwork on the first line and
places the text `Kinesis` on a second line, rendered through
`frontend/app/components/ui/AverQelWordmark.tsx` on the web surfaces loaded
inside the shell. The Electron preload title bar and the web title bar use the
same lockup.

`pnpm run prepare:brand-assets` stages the canonical AntiAtom SVG into the Electron
resource tree before any desktop build. The Linux AppStream and desktop-entry
display name is `AverQel Kinesis`; the Windows NSIS and macOS DMG product names
use the same value from `package.json`.

When updating the mark, regenerate all three desktop formats from the same SVG
and run `pnpm --dir applications/desktop test` before creating a package.

Linux packages use the Kinesis ID `com.averqel.kinesis`. The AppStream name and
installed application menu both display `AverQel Kinesis`.

The local Kinesis provider owns Codex CLI setup and inference in
`backend/app/providers/services/codex_cli_runtime.py`. In standalone mode, the
backend checks the signed-in ChatGPT account and model catalog through the
Codex CLI app-server, then starts a restricted app-server for each request. The
Codex CLI reads its own local sign-in; the backend does not load or copy CLI
tokens. Provider settings contain an opaque account identifier and the model
catalog, not the CLI email. API-key CLI sessions are not treated as ChatGPT
plan access. The desktop renderer reads this CLI status and catalog from the
backend. Electron does not resolve, inspect, or launch Codex CLI.

ChatGPT browser sign-in, account records, token refresh/revocation, model
discovery, and Responses API inference belong to the local backend provider in
`backend/app/providers/services/codex_chatgpt_oauth.py`. The backend stores
ChatGPT OAuth credentials encrypted with the local provider-secret keyring in
`<AVERQEL_HOME>/secrets/openai-codex-accounts.enc`, scoped to the local tenant
and user. Electron opens the backend-created authorization URL in the system
browser and otherwise only starts and supervises the packaged backend. Both
the CLI and browser sign-in paths are available in the standalone Kinesis app;
neither requires the Electron Codex WebSocket bridge. The local sign-in and
Responses API flow is not a remote-server bridge. Keep the Kinesis app running
unless you started the local backend separately.
Each Codex model's advertised reasoning efforts, including `minimal`, `ultra`,
and other safe catalog values, stay attached to that model and are sent using
their native values. Both the CLI and browser account catalogs provide
account-specific models; browser OAuth uses per-model effort metadata when the
catalog includes it, otherwise DeepSpace uses its existing OpenAI model profile.
Codex CLI subprocesses start outside the project in the OS temporary directory,
and each inference uses a private temporary scratch directory. That directory
is not the selected DeepSpace workspace and does not determine the conversation's
permission mode. Inference requires an
app-server permission profile that denies filesystem access except minimal
runtime paths; shell, MCP, hooks, apps, plugins, subagents, web search, and local
Codex history are disabled. CLI versions that cannot enforce this profile are
not used for inference. API keys and other token-like environment variables
are removed from the Codex child-process environment. DeepSpace function tools
are normalized into one AverQel namespace with unique function names before
the CLI turn starts, matching the app-server contract even when the backend
request uses the OpenAI-compatible function form.
Linux browser sign-in relies on the backend's AES-GCM provider-secret keyring;
Electron `safeStorage` is not part of the current sign-in path. The Codex
app-server RPC surface can change between CLI releases, so older or incompatible
versions can use browser sign-in. The macOS `.dmg` is built with the matching `build:mac:x64` or
`build:mac:arm64` script. OpenAI's current Sign in with
ChatGPT guidance requires paid or remotely hosted apps to complete its
eligibility process.

The desktop pnpm policy allows the `electron-winstaller` install script only.
That reviewed script copies its bundled 7-Zip binaries to the names required by
Windows installer packaging; keep this allowlist narrow rather than enabling
dependency build scripts globally.

## LM Studio and Ollama on the user's computer

The desktop build connects directly to LM Studio or Ollama through the local
AverQel backend. Start LM Studio's OpenAI-compatible server (normally
`http://127.0.0.1:1234/v1`) or start Ollama (normally
`http://127.0.0.1:11434`), then choose the provider in the provider form. Model
discovery, chat, embeddings, and Ollama model operations use those local HTTP
APIs directly; Electron starts and manages AverQel's packaged backend. Ollama
and LM Studio are separate local applications and are not bundled. Their chat
models stay in storage managed by those runtimes; connecting over HTTP cannot
relocate those files. Ollama pulls remain managed by Ollama through the provider
page, and LM Studio model downloads remain managed by LM Studio. Hugging Face
embedding and reranker downloads made inside AverQel are stored under
`~/.averqel-harness/models/`.

Web search needs an Internet connection even though its orchestration and data
storage are local. The packaged desktop does not automatically install the
optional source-cloning SearXNG development runtime; configure a search
provider if web search is needed. A configured local model continues to work
without network access once its model is installed.
