---
name: office-xlsx
description: Create, inspect, and edit Excel workbooks with Python.
when-to-use: Spreadsheets, workbook templates, formulas, or tabular analysis.
model-invocable: true
user-invocable: true
---
# Excel workbooks

Inspect sheet names, dimensions, merged ranges, formulas, and number formats
before editing. Preserve formulas and workbook structure.

Remember that `openpyxl` does not calculate formulas. When the user needs
computed values, call `office_process` with `recalculate_workbook`, then reopen
the recalculated copy and check the saved values.

Validation: confirm sheet names, the changed cell range, and that formulas
remain formulas rather than being replaced by their last cached value.

See `references/formulas-and-recalculation.md`.
