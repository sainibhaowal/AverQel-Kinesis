---
name: office-render
description: Render Office files to inspect their visual layout.
when-to-use: The user asks for visual review or a layout check.
model-invocable: true
user-invocable: true
---
# Render Office files

Use `office_process` with `render_pages` and the smallest page limit that
answers the question. The tool writes page images into the workspace and
returns their paths.

If the packaged LibreOffice or `pdftoppm` runtime is unavailable, report that
visual rendering cannot run in this environment. Never claim a rendered layout
without inspecting the returned images.

See `references/rendering-limits.md`.
