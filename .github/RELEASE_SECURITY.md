# Release security and artifact boundaries

The desktop-artifact workflow builds native installers from each target OS and
architecture. It is manual, uploads workflow artifacts only, and never starts a
VPS deployment or publishes a GitHub Release.

## Desktop release contents

| Item | Release behavior |
| --- | --- |
| Linux | Six separate workflow artifacts: x64 and ARM64 each have an independent `.deb`, `.rpm`, and `.AppImage` download |
| Windows | Separate x64 and ARM64 `.exe` workflow artifacts |
| macOS | Separate x64 and ARM64 `.dmg` workflow artifacts |
| Electron package | Kinesis shell, static frontend, and frozen local FastAPI/Python runtime; no NeoSIS runtime or second Electron application |
| Installer size | Includes local API and machine-learning dependencies; inspect built artifacts rather than applying the old 160 MiB frontend-only ceiling |
| Signing | Installers are unsigned until platform-specific signing and notarization are configured |
| Build workflow | Manual native-architecture builds; each OS/architecture/installer-format combination is uploaded separately, with no GitHub Release published |
| Runtime | Fully local app process; no Docker/VPS runtime is part of the release |

The desktop artifact contains AverQel Kinesis, its local static frontend, and
the backend process Electron starts and stops. No external application
repository is checked out or included in the desktop package. App-owned data
uses one default root, `~/.averqel-harness`; selected coding projects stay at
the paths chosen by the user.

Workflow artifact names use `AverQel-Kinesis-<platform>-<architecture>-<format>`.
For example, Linux x64 `.deb` and `.rpm` files are separate artifacts named
`AverQel-Kinesis-linux-x64-deb` and `AverQel-Kinesis-linux-x64-rpm`. A workflow
run uploads ten artifacts total: six Linux, two Windows, and two macOS.

## Signing status

The desktop-artifact workflow does not sign installers. Windows may display an
unknown-publisher warning, and macOS Gatekeeper may warn or block the DMG until
the app is signed and notarized. Do not describe these packages as signed.
