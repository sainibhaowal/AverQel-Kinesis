# DeepSpace Directory Picker User Guide

**Date:** 2026-11-19

**Audience:** Users and integrators choosing a workspace directory through the host picker.

---

## Overview

The host directory picker chooses a folder on the machine running the backend. It has two backends:

| Backend | What it does | Where it works |
|---|---|---|
| **Native** | Opens the operating system's folder dialog and returns the chosen path | macOS, Windows, or Linux with a display and `zenity`/`kdialog` |
| **Browse** | Lists directories and creates folders over HTTP for an in-app browser | Every host, including SSH and headless servers |

One backend is resolved per API process at first use. The route that does not match the resolved backend returns `400` with `Native picker not available` or `Browse picker not available`.

---

## How the Backend Is Chosen

`create_auto_directory_picker_service()` resolves once per process:

1. If `SSH_CONNECTION` or `SSH_TTY` is set → **browse**.
2. On macOS or Windows → **native**.
3. On Linux → **native** when `zenity` or `kdialog` is on `PATH` and `DISPLAY` or `WAYLAND_DISPLAY` is set; otherwise **browse**.
4. Any other platform → **browse**.

The remote-authority and bind-address checks are placeholders today: they do not inspect the actual web server configuration. Environment changes (installing zenity, connecting a display, ending an SSH session) need an API restart before the picker re-resolves.

The resolved backend is cached in the process-wide service; there is no `/permission preset` selector for the picker. `/permission` controls the agent permission mode (`auto`, `ask`, `deny`) and has no effect here.

---

## Native Backend

`POST /api/v1/host/directory-picker/pick`

```json
{"signal": null}
```

```json
{"path": "/home/user/projects"}
```

`path` is `null` when the user cancels. The `signal` field is accepted but is a string placeholder; the current routes do not translate an HTTP request cancellation into a dialog abort.

| Platform | Command used |
|---|---|
| macOS | `osascript` with an AppleScript `choose folder` dialog |
| Linux | `zenity --file-selection --directory`, then `kdialog --getexistingdirectory` |
| Windows | PowerShell `System.Windows.Forms.FolderBrowserDialog` |

On Linux, if neither zenity nor kdialog can be launched, the call fails with `No directory chooser available (install zenity or kdialog)`.

---

## Browse Backend

### List one level

`GET /api/v1/host/directory-picker/list?path=/home/user`

```json
{
  "entries": [
    {"path": "/home/user/projects", "name": "projects", "hidden": false},
    {"path": "/home/user/.config", "name": ".config", "hidden": true}
  ],
  "crumbs": [
    {"path": "/", "name": "/", "hidden": false},
    {"path": "/home", "name": "home", "hidden": false},
    {"path": "/home/user", "name": "user", "hidden": false}
  ],
  "truncated": false,
  "home": "/home/user"
}
```

- Only directories are listed; files are ignored.
- Entries are sorted case-insensitively by name.
- Dot-prefixed directories are marked `hidden`; the API always returns them and the UI decides whether to show them.
- Symlinked directories are followed; broken links are skipped by the scanner.
- Without `path`, the listing starts at the backend process's home directory.
- The path must be absolute. A relative path, a missing path, or an unreadable path returns `DIRECTORY_UNREADABLE`.
- At most 1,000 entries are returned per level (`BrowseConfig.max_entries`); `truncated` says whether the list was cut.

### Create a directory

`POST /api/v1/host/directory-picker/create`

```json
{"path": "/home/user", "name": "new-project"}
```

```json
{"path": "/home/user/new-project", "hidden": false}
```

Rules:

- `name` must be one path segment: no `/`, no `\`, not `.` or `..`, no leading or trailing whitespace.
- The parent directory must already exist; the route does not create intermediate directories.
- The directory is created with `mkdir(parents=False, exist_ok=False)`.
- An existing child returns `DIRECTORY_EXISTS`.

---

## Errors

Picker failures use the API's standard error envelope with status `400`:

```json
{
  "error": {
    "code": "DIRECTORY_UNREADABLE",
    "message": "directory-unreadable: /home/user/missing",
    "details": {}
  }
}
```

| Code | Meaning |
|---|---|
| `DIRECTORY_UNREADABLE` | The path is relative, missing, or cannot be read |
| `DIRECTORY_EXISTS` | The child directory already exists |
| `DIRECTORY_CREATE_FAILED` | The name is invalid, the parent is missing, or the write failed |

Route-level refusals use the same envelope:

| Status | Message | Cause |
|---|---|---|
| `400` | `Native picker not available` | The process resolved the browse backend |
| `400` | `Browse picker not available` | The process resolved the native backend |
| `503` | `No directory picker backend is available` | No backend was registered |

---

## Using It from the App

The host API is under `/api/v1`:

```typescript
// Native (only when the native backend is resolved)
const picked = await api.post("/host/directory-picker/pick", { signal: null });

// Browse (only when the browse backend is resolved)
const listing = await api.get("/host/directory-picker/list", { path });
const created = await api.post("/host/directory-picker/create", { path, name });
```

All three routes require authentication. The session/event bus does not emit picker events; the response is the only result.

---

## Security Notes

- The browse backend serves whatever the backend process can read. Run the API as a user with the filesystem access you intend to expose.
- Paths must be absolute; `..` is not accepted as a whole path. The backend resolves symlinks before listing.
- Directory creation is confined to one child under an existing absolute parent.
- The picker does not consult the DeepSpace sandbox mode. It is a host-level chooser, separate from the sandbox that bounds agent tools.
- Native dialogs run as the backend process and inherit its display and permissions.

---

## Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| `Native picker not available` | The process resolved browse (SSH, headless Linux, or no chooser) | Use the browse routes, or install zenity/kdialog and restart with a display |
| `Browse picker not available` | The process resolved native | Use `/pick`, or start the API in an SSH/headless context |
| `No directory chooser available` | Linux has a display but neither zenity nor kdialog | Install one and restart the API |
| macOS dialog fails | `osascript` cannot drive the UI | Grant Automation/Accessibility permission to the backend process |
| A directory is missing from the list | It is a file, a broken symlink, or beyond the 1,000-entry bound | Check `truncated`; list a narrower path |
| `DIRECTORY_CREATE_FAILED` | Parent missing, invalid name, or permission denied | Create the parent first and use a single valid segment |

---

## Related Guides

- [DeepSpace Open In App Guide](DEEPSPACE_OPEN_IN_APP_USER_GUIDE.md) — Launching applications on the host
- [DeepSpace Filesystem Guide](DEEPSPACE_FILESYSTEM_USER_GUIDE.md) — Agent file tools and workspace scope
- [DeepSpace Permission Guide](DEEPSPACE_PERMISSION_USER_GUIDE.md) — Sandbox and approval modes

---

*Last updated: 2026-11-19*
