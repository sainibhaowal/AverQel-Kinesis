# Office sidecar

DeepSpace ships these libraries in the workspace runtime:

| Library | Used for |
|---|---|
| `python-docx` | DOCX paragraphs, tables, styles, sections |
| `openpyxl` | XLSX cells, formulas, merged cells, styles |
| `python-pptx` | PPTX slides, shapes, text, notes |

`office_process` operations:

| Operation | Input | Output |
|---|---|---|
| `export_pdf` | DOCX, XLSX, PPTX | PDF next to the requested path |
| `recalculate_workbook` | XLSX, ODS | Recalculated workbook copy |
| `render_pages` | Office file or PDF | Page images with a bounded count |

If LibreOffice (`soffice`) or `pdftoppm` is not installed, `export_pdf`,
`recalculate_workbook`, and Office `render_pages` report that the runtime is
unavailable. PDF `render_pages` still works when only `pdftoppm` is present.
