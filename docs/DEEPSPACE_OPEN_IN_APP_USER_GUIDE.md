# DeepSpace Open In App User Guide

**Audience:** End users opening workspace directories in external applications.

---

## Overview

DeepSpace's **Open In App** feature lets you open your workspace directory in your preferred external editor, Git GUI, terminal, or file manager with a single click. The feature automatically detects installed applications and presents them in a convenient split-button menu in the session header.

---

## How It Works

### Automatic Detection
When DeepSpace starts, it scans your system for installed applications from a curated catalog:

| Category | Applications |
|----------|--------------|
| **Editors & IDEs** | Cursor, VS Code, VS Code Insiders, Windsurf, Zed, Sublime Text, Xcode, Android Studio, IntelliJ IDEA, PyCharm, WebStorm, PhpStorm, GoLand, Rider, RustRover |
| **Git GUIs** | Fork, Sourcetree, GitHub Desktop, Tower, GitKraken, SmartGit, Sublime Merge |
| **Terminals** | Ghostty, Warp, iTerm2, kitty, Terminal, Windows Terminal, Git Bash, GNOME Terminal, Konsole |
| **File Managers** | Finder (macOS), File Explorer (Windows), xdg-open (Linux) |

### Detection Methods
| Platform | Method |
|----------|--------|
| **macOS** | Scans `/Applications` and `~/Applications`; `xcode-select -p` for Xcode |
| **Windows** | Reads `App Paths` registry keys, Uninstall records, well-known paths |
| **Linux** | PATH lookup, XDG desktop entries (`TryExec`/`Exec`), `xdg-open` fallback |

### Verification
Only **verified launchers** appear — applications that DeepSpace can actually launch:
- Executable exists on disk
- Launch command works
- Icon extracted successfully (or generic fallback)

---

## User Interface

### Open In App Button
Located in the **session header** (top-right of chat area):

```
[ Open In ▼ ]  ← Split button
```

### Menu Layout
```
┌─────────────────────────────────┐
│  Editors & IDEs                 │
│  ● Cursor          [icon]       │
│  ● Visual Studio Code  [icon]   │
│  ● VS Code Insiders  [icon]     │
│  ─────────────────────────────  │
│  Git GUIs                       │
│  ● Fork              [icon]     │
│  ● GitHub Desktop    [icon]     │
│  ─────────────────────────────  │
│  Terminals                      │
│  ● Ghostty           [icon]     │
│  ● Warp              [icon]     │
│  ─────────────────────────────  │
│  File Managers                  │
│  ● Finder            [icon]     │
│                                 │
│  [ No applications found ]      │  (if none detected)
└─────────────────────────────────┘
```

### Visual Indicators
| Indicator | Meaning |
|-----------|---------|
| ● Green dot | App verified and ready |
| ⚠ Yellow triangle | App detected but unverified |
| No icon | Generic fallback glyph |
| [ Open In ▼ ] disabled | No apps detected / SSH session |

---

## How to Use

### Opening Workspace in an App
1. Click **Open In ▼** in session header
2. Select your desired application
3. Workspace directory opens in that app

### Keyboard Shortcuts
| Shortcut | Action |
|----------|--------|
| `Ctrl/Cmd + Shift + O` | Open "Open In" menu |
| `Esc` | Close menu |
| `↑/↓` | Navigate options |
| `Enter` | Launch selected app |

---

## SSH & Remote Sessions

### Automatic Hiding
When DeepSpace detects an **SSH session** (`SSH_CONNECTION` or `SSH_TTY` environment variables), the Open In App button is **automatically hidden** because:
- Remote apps can't access your local workspace
- Launching local apps from remote session is confusing
- Security: prevents accidental local app launches

### What Triggers SSH Detection
- `SSH_CONNECTION` environment variable
- `SSH_TTY` environment variable
- *Note: Project/user `.env` files do NOT trigger this*

### Forcing Visibility (Advanced)
If you're on a remote machine with display forwarding:
```bash
# Not recommended for typical use
unset SSH_CONNECTION SSH_TTY
# Restart DeepSpace
```

---

## App-Specific Behavior

### Editors & IDEs
| App | Launch Command | Workspace Handling |
|-----|----------------|-------------------|
| VS Code / Cursor / Windsurf | `code .` / `cursor .` / `windsurf .` | Opens folder as workspace |
| Sublime Text | `subl .` | Opens folder |
| Xcode | `open -a Xcode` | Opens project/folder |
| JetBrains (IntelliJ, PyCharm, etc.) | `idea .` / `pycharm .` | Opens as project |
| Zed | `zed .` | Opens folder |
| Android Studio | `studio .` | Opens project |

### Git GUIs
| App | Launch Command | Workspace Handling |
|-----|----------------|-------------------|
| Fork | `fork .` | Opens repo |
| GitHub Desktop | `github open .` | Opens repo |
| Sourcetree | `sourcetree .` | Opens repo |
| Tower | `gittower .` | Opens repo |
| GitKraken | `gitkraken --path .` | Opens repo |

### Terminals
| App | Launch Command | Workspace Handling |
|-----|----------------|-------------------|
| Ghostty / Warp / iTerm2 / Kitty | `app .` | Opens tab in directory |
| Terminal (macOS) | `open -a Terminal .` | New window in directory |
| Windows Terminal | `wt -d .` | New tab in directory |
| Git Bash | `bash --cd .` | Starts in directory |

### File Managers
| App | Launch Command | Behavior |
|-----|----------------|----------|
| Finder (macOS) | `open .` | Opens folder window |
| File Explorer (Windows) | `explorer .` | Opens folder window |
| xdg-open (Linux) | `xdg-open .` | Default file manager |

---

## Icons

### Icon Sources
| Platform | Extraction Method | Size |
|----------|-------------------|------|
| **macOS** | `plutil` + `sips` on `.icns` | 128×128 PNG |
| **Windows** | PowerShell `ExtractAssociatedIcon` | 32×32 PNG |
| **Linux** | Desktop entry `Icon` + hicolor theme | Theme-dependent |

### Fallback
- Missing icon → 404 → Generic glyph rendered by browser
- CLI-only apps (no GUI) → Generic glyph

### Icon Caching
- Icons cached in memory per process
- Cache invalidated on app re-resolution (restart)
- Served via `GET /host/open-in-app/icon/<app_id>`

---

## Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| **No apps shown** | SSH session detected | Check `env \| grep SSH` |
| **App missing** | Not in catalog / not detected | Check installation path; restart DeepSpace |
| **Icon missing** | No icon source / extraction failed | Generic glyph shown; restart to refresh |
| **Launch fails** | App moved / uninstalled | Restart DeepSpace (auto-heals) |
| **Wrong app launches** | Multiple versions installed | Catalog picks first match; rename/uninstall extras |
| **Icon blurry (Windows)** | 32px extracted → upscaled | Known limitation (ExtractAssociatedIcon limit) |
| **New install not shown** | Resolution runs at startup | Restart DeepSpace |

### Reset & Refresh
```bash
# Force re-resolution of all apps
# (happens automatically on restart)
# Or delete cache file:
rm ~/.config/deepspace/open-in-app-cache.json
# Then restart DeepSpace
```

### Force Re-detection
1. Quit DeepSpace completely
2. Install/uninstall target application
3. Start DeepSpace
3. Open In App menu refreshes automatically

---

## Advanced Configuration

### Catalog Customization (Not Yet Supported)
The catalog is **fixed at build time**. To add custom applications:
1. Fork DeepSpace
2. Extend `OPEN_IN_APP_CATALOG` in `service.py`
3. Add browser-side dictionary entries
4. Rebuild

### Deadline Tuning
```yaml
deepspace_open_in_app:
  probe_timeout_ms: 10000    # Catalog resolution deadline
  icon_timeout_ms: 10000     # Icon extraction deadline
  launch_watch_ms: 1000      # Launch verification window
```

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `probe_timeout_ms` | 10000 | Max time for app detection commands |
| `icon_timeout_ms` | 10000 | Max time for icon extraction |
| `launch_watch_ms` | 1000 | Time to verify launch success |

### Launch Environment
- **Credential Scrubbing**: AWS/GITHUB/DOCKER/KUBE vars stripped
- **Detached Spawn**: `start_new_session=True`
- **Launch Watch**: Process monitored for `launchWatchMs` ms
- **ENOENT Handling**: Missing executable → auto-removed from catalog

---

## API Reference

### Get Catalog
```http
GET /host/open-in-app/apps
```
Response:
```json
[
  { "id": "vscode", "name": "Visual Studio Code", "category": "editor", "icon": "/host/open-in-app/icon/vscode" },
  { "id": "github-desktop", "name": "GitHub Desktop", "category": "git-gui", "icon": null }
]
```

### Get Icon
```http
GET /host/open-in-app/icon/vscode
```
Response: `image/png` (or 404 if no icon)

### Launch App
```http
POST /host/open-in-app/open
Content-Type: application/json

{
  "app_id": "vscode",
  "path": "/home/user/project"
}
```
Response:
```json
{ "success": true }
```

### Authentication
All routes require:
- Valid browser authentication (cookie/token)
- Host/Origin fence (same-origin check)
- Valid `application/json` body (≤64 KiB)

---

## Catalog Reference

### Editors & IDEs (9)
| ID | Name | Platforms |
|----|------|-----------|
| `cursor` | Cursor | macOS, Linux, Windows |
| `vscode` | Visual Studio Code | macOS, Linux, Windows |
| `vscode-insiders` | VS Code Insiders | macOS, Linux, Windows |
| `windsurf` | Windsurf | macOS, Linux, Windows |
| `zed` | Zed | macOS, Linux |
| `sublime-text` | Sublime Text | macOS, Linux, Windows |
| `xcode` | Xcode | macOS |
| `android-studio` | Android Studio | macOS, Linux, Windows |
| `intellij` | IntelliJ IDEA | macOS, Linux, Windows |
| `pycharm` | PyCharm | macOS, Linux, Windows |
| `webstorm` | WebStorm | macOS, Linux, Windows |
| `phpstorm` | PhpStorm | macOS, Linux, Windows |
| `goland` | GoLand | macOS, Linux, Windows |
| `rider` | Rider | macOS, Linux, Windows |
| `rustrover` | RustRover | macOS, Linux, Windows |

### Git GUIs (7)
| ID | Name | Platforms |
|----|------|-----------|
| `fork` | Fork | macOS, Windows |
| `sourcetree` | Sourcetree | macOS, Windows |
| `github-desktop` | GitHub Desktop | macOS, Windows |
| `tower` | Tower | macOS, Windows |
| `gitkraken` | GitKraken | macOS, Linux, Windows |
| `smartgit` | SmartGit | macOS, Linux, Windows |
| `sublime-merge` | Sublime Merge | macOS, Linux, Windows |

### Terminals (8)
| ID | Name | Platforms |
|----|------|-----------|
| `ghostty` | Ghostty | macOS, Linux |
| `warp` | Warp | macOS, Linux, Windows |
| `iterm2` | iTerm2 | macOS |
| `kitty` | Kitty | macOS, Linux |
| `terminal` | Terminal | macOS |
| `windows-terminal` | Windows Terminal | Windows |
| `git-bash` | Git Bash | Windows |
| `gnome-terminal` | GNOME Terminal | Linux |
| `konsole` | Konsole | Linux |

### File Managers (3)
| ID | Name | Platforms |
|----|------|-----------|
| `finder` | Finder | macOS |
| `file-explorer` | File Explorer | Windows |
| `xdg-open` | xdg-open | Linux |

---

## Extending the Catalog (Developers)

To add a new application:

1. **Add to `OPEN_IN_APP_CATALOG`** in `service.py`:
```python
AppEntry(
    id="my-editor",
    name="My Editor",
    category="editor",
    platforms=["macos", "linux", "windows"],
    launcher_sources=["app", "cli"],
    icon_source="my-editor",
)
```

2. **Add icon handling** in `_resolve_entry` / `_resolve_source`

3. **Add browser-side display** in frontend package

4. **Test on all platforms**

---

## Related Guides

- [Web Server Guide](DEEPSPACE_WEB_SERVER_USER_GUIDE.md)
- [Directory Picker Guide](DEEPSPACE_DIRECTORY_PICKER_USER_GUIDE.md)
- [Permission Presets Guide](DEEPSPACE_PERMISSION_USER_GUIDE.md)

---

*Last updated: 2026-11-16*
*Version: 1.0*