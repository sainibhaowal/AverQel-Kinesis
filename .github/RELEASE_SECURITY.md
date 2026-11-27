# Release security and artifact boundaries

The desktop build workflow uses native runners for Linux, Windows, and macOS
targets. Manual runs upload selected workflow artifacts only. A stable version
tag builds every supported target and publishes a GitHub Release only after
package verification, release-asset checks, and required code-signing steps.
It never starts a VPS deployment.

Pull requests use `.github/workflows/ci.yml` instead. The required `CI Passed`
check covers source quality, dependency audits, backend and frontend tests,
loopback browser E2E, Electron shell tests, and a Linux packaged-backend smoke
test, and reusable Windows native-runtime checks. It does not imply that every
native installer has been built; the release tag pipeline performs that full
six-target check.

## Desktop release contents

| Item | Release behavior |
| --- | --- |
| Linux | Six separate workflow artifacts: x64 and ARM64 each have an independent `.deb`, `.rpm`, and `.AppImage` download |
| Windows | Separate x64 and ARM64 `.exe` workflow artifacts |
| macOS | Separate x64 and ARM64 `.dmg` workflow artifacts |
| Electron package | Kinesis shell, static frontend, and frozen local FastAPI/Python runtime; no NeoSIS runtime or second Electron application |
| Installer size | Includes local API and machine-learning dependencies; inspect built artifacts rather than applying the old 160 MiB frontend-only ceiling |
| Signing | Manual artifacts are unsigned; tag releases require Windows signing and macOS signing/notarization secrets |
| Build workflow | Manual native-architecture artifact builds or complete six-target tag releases |
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

## Signing requirements

Configure `WINDOWS_CSC_LINK`, `WINDOWS_CSC_KEY_PASSWORD`, `MACOS_CSC_LINK`,
`MACOS_CSC_KEY_PASSWORD`, `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, and
`APPLE_TEAM_ID` as GitHub Actions secrets before pushing a release tag. The
workflow fails before building if any required release credential is missing.
Windows release installers are checked with Authenticode; macOS disk images
must pass Apple notarization and stapler validation. Manual artifacts are
unsigned and must be described as such.
