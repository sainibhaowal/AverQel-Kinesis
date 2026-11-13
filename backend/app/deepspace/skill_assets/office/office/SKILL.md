---
name: office
description: Choose and safely complete a Word, spreadsheet, or presentation task.
when-to-use: Office documents, workbooks, presentations, or PDF export.
model-invocable: true
user-invocable: true
---
# Office workflow

1. Inspect the requested file before editing it: type, size, sheets/slides,
   paragraphs, and any existing formulas.
2. Load the type skill: `office-docx`, `office-xlsx`, or `office-pptx`.
3. Keep the source. Write a new file unless the user asked to replace it.
4. Validate the saved result with the same library that wrote it.
5. Use `office_process` for PDF export, workbook recalculation, and page
   rendering. It works on a private copy and keeps outputs in the workspace.
6. Never report a conversion or recalculation as successful until the output
   exists and has been checked.

Do not use shell conversion tools directly when `office_process` supports the
operation; the tool enforces workspace containment and bounded page counts.

For the installed libraries, supported operations, and known limits, read
`references/sidecar.md`.
