---
name: desktop-release
description: Build, inspect, or troubleshoot AverQel Kinesis desktop artifacts containing the Electron shell, static frontend, and local Python API runtime.
---

# Kinesis desktop artifact workflow

1. Read `AGENT_ENGINEERING_STANDARD.md`, use `production-change` and
   `quality-gates`, and review `applications/desktop/README.md`,
   `.github/RELEASE_SECURITY.md`, `.github/workflows/desktop-artifacts.yml`,
   and the desktop sections of `README.md`. Preserve unrelated work.
2. AverQel Kinesis is a standalone local desktop application. Its package
   contains the Electron shell, static Next.js frontend, and frozen local
   FastAPI/Python backend. It does not bundle, launch, or record a NeoSIS
   runtime. The installed app starts its backend on loopback, waits for health,
   opens the renderer, and stops the backend when Electron closes.
3. Build the Python backend on the target OS and architecture because the
   freezer and native libraries must match that target. Use the exact commands
   in `applications/desktop/README.md`, for example:

   ```bash
   pnpm --dir applications/desktop build:linux:x64
   pnpm --dir applications/desktop build:linux:arm64
   pnpm --dir applications/desktop build:windows:x64
   pnpm --dir applications/desktop build:windows:arm64
   pnpm --dir applications/desktop build:mac:x64
   pnpm --dir applications/desktop build:mac:arm64
   ```

   Linux produces `.deb`, `.rpm`, and AppImage packages; Windows produces
   `.exe` installers; macOS produces `.dmg` images. Windows ARM64 uses an x64
   Python backend under Windows x64 emulation. Intel macOS omits local
   Hugging Face embedding and reranking inference; remote providers remain
   available.
4. The `Desktop artifacts` workflow runs on manual dispatch and pushes to
   `codex/ci/**`. It builds on native OS/architecture runners and uploads
   workflow artifacts. It does not publish a GitHub Release, deploy the VPS,
   or sign installers. Do not trigger a build, release, or deployment unless
   the user explicitly asks for that operation.
5. For runtime or packaging changes, run the focused Electron tests and every
   build check that the affected code can safely support on the current host.
   Cross-platform artifacts require their matching native runners; a local
   Linux build does not verify Windows or macOS packaging. Inspect generated
   package contents and manifests when a package build is requested.
6. Validate modified workflows with the local Actionlint binary, run the
   relevant tests and quality gates, and review the final diff. Before a
   requested push, complete the isolated pre-push checks. Report the exact
   targets built, test results, artifacts inspected, and any target that could
   not be built or verified.
