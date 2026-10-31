# Release security and artifact boundaries

The desktop-artifact workflow builds native installers from each target OS and
architecture. It is manual, uploads workflow artifacts only, and never starts a
VPS deployment or publishes a GitHub Release.

## Desktop release contents

| Item | Release behavior |
| --- | --- |
| Linux | Native x64 and ARM64 `.deb`, `.rpm`, and `.AppImage` artifacts |
| Windows | Native x64 and ARM64 `.exe` installers |
| macOS | Native x64 and ARM64 `.dmg` images |
| Electron package | Kinesis shell, static frontend, and frozen local FastAPI/Python runtime; no NeoSIS runtime or second Electron application |
| Installer size | Includes local API and machine-learning dependencies; inspect built artifacts rather than applying the old 160 MiB frontend-only ceiling |
| Signing | Installers are unsigned until platform-specific signing and notarization are configured |
| Build workflow | Manual native-architecture artifact builds; no GitHub Release is published |
| VPS | Not built, changed, or deployed by this workflow |

The desktop artifact contains AverQel Kinesis, its local static frontend, and
the backend process Electron starts and stops. No external application
repository is checked out or included in the desktop package. App-owned data
uses one default root, `~/.averqel-harness`; selected coding projects stay at
the paths chosen by the user.

## Signing status

The desktop-artifact workflow does not sign installers. Windows may display an
unknown-publisher warning, and macOS Gatekeeper may warn or block the DMG until
the app is signed and notarized. Do not describe these packages as signed.

## VPS deployment boundary

The manual VPS workflow builds and verifies server images, then deploys them to
the configured VPS. It is independent from the desktop release workflow. A
successful desktop release does not prove that the production website or
backend was deployed or updated.
