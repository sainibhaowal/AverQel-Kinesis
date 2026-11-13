# Formulas and recalculation

- `openpyxl.load_workbook(path, data_only=False)` returns formulas.
- `data_only=True` returns cached values, which may be stale or `None`.
- Writing a formula stores the formula string; no engine evaluates it.
- `office_process` with `recalculate_workbook` copies the workbook, asks
  LibreOffice to recalculate and save it, then returns the copy.
- Reopen the recalculated copy with `data_only=True` to confirm values exist.
- Never overwrite the user's original workbook during recalculation.
