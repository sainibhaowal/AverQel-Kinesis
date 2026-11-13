# Recalculation notes

- Supported inputs: XLSX and ODS.
- The operation copies the source, recalculates in LibreOffice, and atomically
  moves the result to the requested output.
- A zero-byte or missing output is a failure, not an empty result.
- Macros are not executed; only cell formulas are recalculated.
- If the source has external links that cannot resolve, report the limitation
  instead of presenting cached values as fresh.
