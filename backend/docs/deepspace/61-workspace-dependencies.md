# 61. Workspace dependencies and runtime payload

The `load_workspace_dependencies` tool reports the absolute paths and versions
of the bundled Python runtime, Node.js, pnpm, LibreOffice, and Poppler
binaries.  In the desktop build, the Python runtime payload is atomically
installed on first use.

## Use case

- A skill needs to know the exact Python interpreter path for subprocess calls.
- A workflow checks whether `soffice` is available before attempting conversion.
- A deployment verifies the installed payload matches the expected `runtime.json` digest.

## Why it matters

- The desktop app bundles its own Python, Node.js, pnpm, LibreOffice, and Poppler.
- Paths differ between development (host), frozen desktop (embedded), and VPS deployments.
- Atomic install with rollback ensures a partial copy never breaks the app.

## How it works

### Runtime manifest (`runtime_manifest.py`)

`runtime.json` (packaged with the desktop build):
```json
{
  "schema_version": 1,
  "platform": "linux",
  "architecture": "x64",
  "python": { "version": "3.12.5", "path": "dependencies/python/bin/python3" },
  "packages": {
    "numpy": "1.26.0",
    "pandas": "2.2.0",
    "python-docx": "1.1.0",
    "python-pptx": "1.0.0",
    "openpyxl": "3.1.0",
    "Pillow": "10.0.0",
    "lxml": "5.0.0",
    "XlsxWriter": "3.2.0"
  },
  "executables": {
    "soffice": "dependencies/libreoffice/program/soffice",
    "pdftoppm": "dependencies/poppler/bin/pdftoppm"
  }
}
```

### Payload install (`tools/dependencies.py`)

`ensure_runtime_payload(settings)`:
- Reads `deepspace_runtime_payload_source` (bundled payload directory).
- Reads `deepspace_runtime_payload_root` (installation directory under app home).
- If `root` exists with matching `runtime.json` → returns `{ "status": "present" }`.
- If `root` missing or mismatched → copies to staging, validates, atomic rename.
- On failure: restores previous version from `{root}.previous`.
- Returns `{ "status": "installed" | "present" | "repaired", "path": "...", "source": "..." }`.

### Tool: `load_workspace_dependencies`

```json
{
  "name": "load_workspace_dependencies",
  "arguments": {}
}
```

Returns:
```json
{
  "python": "/absolute/path/to/python3",
  "node": "/absolute/path/to/node",
  "pnpm": "/absolute/path/to/pnpm.mjs",
  "pythonPackages": "/absolute/path/to/site-packages",
  "nodePackages": "/absolute/path/to/node_modules",
  "pythonDistributions": {
    "numpy": "1.26.0",
    "pandas": "2.2.0",
    "python-docx": "1.1.0",
    "python-pptx": "1.0.0",
    "openpyxl": "3.1.0",
    "Pillow": "10.0.0",
    "lxml": "5.0.0",
    "XlsxWriter": "3.2.0"
  },
  "officeRuntimeSources": {
    "soffice": "manifest",
    "libreoffice": "unavailable",
    "pdftoppm": "manifest"
  }
}
```

- `officeRuntimeSources`: `"manifest"` (from runtime.json), `"host-path"` (host binary), or `"unavailable"`.

### Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_runtime_payload_source` | `str` | `""` | Absolute path to bundled payload directory (carries `runtime.json` + `dependencies/`). |
| `deepspace_runtime_payload_root` | `str` | `""` | Absolute path to installation directory under app home. When set, payload is copied here on first use. |
| `deepspace_runtime_manifest_path` | `str` | `""` | Optional explicit `runtime.json` path; overrides payload discovery. |

### Desktop vs. development

| Environment | Python path | Node/pnpm | LibreOffice/Poppler |
|---|---|---|---|
| Development (host) | `sys.executable` | Host `node`/`pnpm` | Host PATH |
| Desktop (frozen) | Embedded payload | Embedded payload | Embedded payload |
| VPS (explicit manifest) | From manifest | From manifest | From manifest |

## What to watch for

- The payload install is atomic: staging → validate → rename.  A crash during copy leaves the previous version intact.
- `deepspace_runtime_payload_root` must be writable by the app user.
- In the desktop build, `collect_data_files("app")` in `averqel-backend.spec` includes `skill_assets/` and the payload.
- `load_workspace_dependencies` does not modify `PATH` or package-manager settings — it only reports paths.

## Related chapters

- [Bundled Skills](33-bundled-skills.md) — Office skills use the bundled LibreOffice/Poppler.
- [Office Processing](33-bundled-skills.md#office-processing) — `office_process` tool.
- [Configuration Reference](22-configuration-reference.md) — all `deepspace_runtime_*` settings.
