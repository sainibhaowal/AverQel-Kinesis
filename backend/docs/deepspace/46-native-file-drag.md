# 46. Native file drag-out

## Use case

A person wants to drag a file from the DeepSpace Library directly to the
desktop, a file manager, or another application instead of downloading it and
finding it again.

## Why it matters

Browser drag data cannot safely turn a Library record into an operating-system
file. The native shell must create that file without giving the renderer a
host path or bypassing the Library's conversation ownership checks.

## How it works

In the desktop app, drag the Library file's download button out of the
DeepSpace Library drawer. The renderer obtains the bytes through the existing
authenticated route:

```text
GET /api/v1/deepspace/library/{conversation_id}/files/{file_id}/content?download=true
```

The route verifies the signed-in user owns the conversation and file. The
renderer stages the response bytes, content type, and display name through the
preload `fileDrag` API when the download button is hovered or keyboard-
focused. Electron's main process verifies the primary AverQel renderer,
sanitizes the filename, and writes a private short-lived temporary file. The
drag event then starts the already-staged handle with `webContents.startDrag`;
if staging is still in progress, the web drag is cancelled and the UI asks the
person to try again once preparation finishes.

The lifecycle uses `file-drag:prepare`, `file-drag:start`,
`file-drag:cancel`, and `file-drag:release`. Opaque handles are bound to the
preparing renderer's `webContents` id; another renderer cannot start or
release them. Each window can hold at most four handles; they expire after
60 seconds, and are removed on drag end, renderer destruction, or application
quit. The renderer refreshes a staged handle before that deadline. On POSIX hosts, the temporary
directory and file use modes `0700` and `0600`; Windows uses the current
user's inherited temporary-directory ACL.

The export is bounded to 32 MiB to keep IPC and renderer memory bounded.
Filename handling removes path components and unsafe/control characters,
handles Windows device names and trailing dots, and keeps the output within
255 UTF-8 bytes. Files with a known MIME type but no extension receive a safe
extension. A normal click still uses the existing browser download flow; web
builds do not expose the Electron API.

## What to watch for

- Native operating-system drag behavior must still be acceptance-tested on
  each packaged platform. The automated suite tests filename policy, byte
  limits, lifecycle cleanup, and renderer authorization, not every external
  drop target.
- Files larger than 32 MiB must use **Download** instead.
- Drag-out is wired to files in the DeepSpace Library. It never accepts a
  renderer-supplied filesystem path and does not expose arbitrary host files.
- Focused tests are in `applications/desktop/tests/file-drag.test.cjs`.
