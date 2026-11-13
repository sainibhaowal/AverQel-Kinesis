---
name: office-pptx
description: Create, inspect, and edit PowerPoint presentations with Python.
when-to-use: Slide decks, PPTX templates, or presentation structure edits.
model-invocable: true
user-invocable: true
---
# PowerPoint presentations

Inspect slide order, layout names, titles, shapes, and speaker notes before
editing. Preserve the template's dimensions and theme.

Validation: reopen the saved deck and verify slide count, every required title,
and that inserted images are present. Use `office_process` with `render_pages`
for a visual check when the user asks for one.

See `references/slide-checklist.md`.
