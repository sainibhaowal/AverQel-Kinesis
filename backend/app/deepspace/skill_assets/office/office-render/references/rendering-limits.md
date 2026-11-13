# Rendering limits

- Office input is converted to PDF first; PDF input is rendered directly.
- `render_pages` is bounded so one call cannot fill the workspace with images.
- Page images are written under the requested output directory.
- Rendering a huge workbook renders the printed page range, not every sheet tab.
- LibreOffice availability is separate from `pdftoppm` availability; check the
  returned limitation instead of assuming both are missing.
