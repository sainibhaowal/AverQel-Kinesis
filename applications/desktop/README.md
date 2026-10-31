# AverQel Kinesis Electron Desktop

The AverQel Kinesis desktop app bundles Electron, the shared static Next.js
frontend, and a frozen FastAPI/Python backend. In an installed app, Electron
starts the backend on loopback port `11001`, waits for its health check, then
opens the UI; closing the app stops that backend. No separate Python service,
Docker container, hosted API server, or manual port startup is required.

The app uses product ID `com.averqel.kinesis` and one app-owned data home,
`~/.averqel-harness`. Chats, sessions, SQLite databases, logs, encrypted
secrets, AverQel-downloaded embedding/reranker models, Hugging Face cache,
Electron profile, and the default workspace live below that directory. A
project folder the user selects remains at its original location. Development
uses API port `11000` and renderer port `11030`; packaged builds use API port
`11001` and a private loopback renderer.

## Development

Start the local backend, frontend development server, and Electron together with
one command:

```bash
pnpm electron dev
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

## Packaging

Build the Python backend on the target OS because the freezer and native
machine-learning libraries must match it. The build script creates an isolated
Python 3.12 environment, installs backend and freezer dependencies, freezes
the API, builds the static frontend, then embeds both in Electron.

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
pins. The manual GitHub workflow uploads artifacts but does not publish a
release. Packages are large because they include the local API and ML runtime,
and are currently unsigned.

### AverQel Kinesis product mark

The canonical icon is `frontend/public/brand/AntiAtom_Icon.svg`, sourced from
`assets/antiatom-icon-geometry.svg`. The frontend uses it on the landing,
authentication, onboarding, dashboard, and Electron title-bar surfaces. The
packaged shell uses the supplied AntiAtom raster from
`assets/antiatom-icon-1024p.png` in
`applications/desktop/assets/icon.png`, `icon.ico`, and `icon.icns`; these are
the icons embedded in Linux, Windows, and macOS builds respectively.

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

The Electron main process owns the OpenAI Codex runtime used by the OpenAI
provider's desktop-only Codex Login tab. It checks for a signed-in ChatGPT
account in the local Codex CLI and, when found, uses the CLI's app-server over
stdio for account/model discovery and inference. AverQel does not read the CLI
credential file or copy its tokens. If no usable ChatGPT CLI login is available,
the user can sign in through OpenAI in the system browser; those OAuth tokens are
encrypted with Electron `safeStorage` in the current operating-system profile.
API-key CLI sessions are not treated as ChatGPT plan access. Provider settings
store an opaque local-account identifier and discovered model metadata; raw
account email and CLI credentials stay on the desktop. The backend relays
inference over a one-use WebSocket ticket and transient request/response frames.
Each Codex model's advertised reasoning efforts, including `minimal`, `ultra`,
and other safe catalog values, stay attached to that model and are sent using
their native Codex values. Browser OAuth uses per-model effort metadata only if
the account's model catalog returns it; otherwise DeepSpace uses its existing
OpenAI model profile.
Codex subprocesses start outside the project in the OS temporary directory, and
each inference uses a private temporary workspace. Inference requires an
app-server permission profile that denies filesystem access except minimal
runtime paths; shell, MCP, hooks, apps, plugins, subagents, web search, and local
Codex history are disabled. CLI versions that cannot enforce this profile are
not used for inference. API keys and other token-like environment variables
are removed from the Codex child-process environment. DeepSpace function tools
are normalized into one AverQel namespace with unique function names before
the CLI turn starts, matching the app-server contract even when the backend
request uses the OpenAI-compatible function form.
Linux OAuth sign-in requires an encrypted GNOME Keyring or KWallet backend;
Electron's `basic_text` fallback is rejected. The Codex app-server RPC surface
can change between CLI releases, so older or incompatible versions fall back to
browser sign-in. The macOS `.dmg` is built with the matching `build:mac:x64` or
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
