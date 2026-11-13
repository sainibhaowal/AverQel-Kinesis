---
name: office-recalculate
description: Recalculate workbook formulas with LibreOffice when available.
when-to-use: Formula results are stale and recalculation is requested.
model-invocable: true
user-invocable: true
---
# Recalculate workbook formulas

Use `office_process` with `recalculate_workbook`. It works on a private copy and
writes the recalculated workbook to the requested output path.

Open the returned workbook with `data_only=True` and confirm the saved values.
Never claim formulas were recalculated when the runtime is unavailable or the
output was not validated. The source workbook must remain unchanged.

See `references/recalculation-notes.md`.
