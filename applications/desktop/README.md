# AverQel Kinesis Electron Desktop

The AverQel Kinesis desktop client is packaged with Electron and uses the shared Next.js
frontend. Desktop development and packaged builds stay local: the renderer uses
the loopback frontend and the local FastAPI API at
`http://127.0.0.1:11000/api/v1`. The desktop shell has no production or VPS URL
fallback.

The AverQel Kinesis shell is intentionally isolated from other AverQel builds. It uses the
application ID `com.averqel.harness`, the data home
`~/.averqel-harness`, the API port `11000`, and the development renderer port
`11030`. Its Electron user data and runtime state are inside that Harness home.

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
uses the local API at `http://127.0.0.1:11000/api/v1`. `ELECTRON_START_URL`
may point only to a loopback or `.localhost` development origin; non-local
origins are rejected.

The frontend runtime file at `frontend/.local/.env.local` supplies local
defaults. Values explicitly provided by the desktop launcher take precedence,
so the desktop shell always keeps its isolated `11000` API and `11030`
renderer ports even when the same checkout was previously used for the web
profile on ports `10000` and `10030`. Restart the desktop command after
changing launcher or runtime environment values.

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

```bash
pnpm --dir applications/desktop build:linux
```

The desktop build creates the static Next.js export with the local API
configuration and stages the brand assets. The packaged shell serves that
export from a private loopback HTTP server instead of loading a hosted web
origin. The local FastAPI backend still must be running at
`127.0.0.1:11000`; use `pnpm electron dev` for the complete development flow or
`pnpm backend` before opening an installed package.

The release workflow builds Linux `.deb` and Windows `.exe` packages containing
only the AverQel Kinesis shell and local frontend.

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

The Linux installer keeps the distinct package ID `com.averqel.harness`. Its
software-center listing uses the AppStream name `AverQel Kinesis` and a
plain-text description; the installed application menu uses the same display
name. The release workflow checks the actual `.deb`
for both the AppStream metadata and an ASCII-only Debian description before it
publishes an installer.

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
browser sign-in. The macOS `.dmg` is built with
`pnpm --dir applications/desktop build:mac`. OpenAI's current Sign in with
ChatGPT guidance requires paid or remotely hosted apps to complete its
eligibility process.

The desktop pnpm policy allows the `electron-winstaller` install script only.
That reviewed script copies its bundled 7-Zip binaries to the names required by
Windows installer packaging; keep this allowlist narrow rather than enabling
dependency build scripts globally.

## LM Studio and Ollama on the user's computer

The desktop build can route a configured LM Studio or Ollama provider to the
same computer that runs AverQel Desktop. Start LM Studio's OpenAI-compatible
server (normally `http://127.0.0.1:1234/v1`) or start Ollama (normally
`http://127.0.0.1:11434`), then choose **This computer** in the provider form.
The Electron main process makes the loopback request and streams normalized
events through the existing authenticated AverQel runtime WebSocket. No model
binary is bundled and no local API key is stored by AverQel. Ollama model pulls
are supported from the provider page; LM Studio model downloads remain managed
by LM Studio.

The desktop local target requires the signed-in desktop app to remain open;
hosted orchestration, authentication, history, and DeepSpace tools still run
through the AverQel backend, so this option does not make the whole product
offline. Remote or server-hosted endpoints remain available through their
hosted provider families; LM Studio and Ollama are intentionally local desktop
connections.
