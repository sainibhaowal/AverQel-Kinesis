# PDF export checklist

- Supported inputs: DOCX, XLSX, PPTX.
- The PDF is written to the requested workspace path.
- Verify the file exists and `stat().st_size > 0`.
- A PDF that opens but has zero pages is a failed conversion.
- Embedded fonts depend on the packaged LibreOffice, not the viewer.
- Keep the Office source; export never replaces it.
