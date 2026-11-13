---
name: office-export-pdf
description: Export a DOCX, XLSX, or PPTX file to PDF with LibreOffice.
when-to-use: The user requests a PDF copy of an Office document.
model-invocable: true
user-invocable: true
---
# Export Office files to PDF

Use `office_process` with `export_pdf`, then verify the PDF exists and is
nonempty before reporting success.

Preserve the source file. If the packaged runtime is unavailable or the
conversion fails, report a clear limitation. Use `office-render` only when the
user also asks to inspect page images.

See `references/pdf-checklist.md`.
