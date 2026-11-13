---
name: office-docx
description: Create, inspect, and edit DOCX files with Python.
when-to-use: Word documents, DOCX templates, or document structure edits.
model-invocable: true
user-invocable: true
---
# Word documents

Inspect before editing: paragraph count, tables, sections, styles, headers and
footers. Preserve existing formatting where possible. Save to a new path when
replacing the source would risk data loss.

Validation: reopen the saved file, then check the required text, paragraph and
table counts, and that every edited table still has a header row.

Use `office_process` with `render_pages` when the user asks for a visual check;
inspect the returned page images rather than claiming the layout is correct.

See `references/python-docx-checklist.md`.
