# DOCX checklist

- Open with `Document(path)` and distinguish an empty document from a missing file.
- Enumerate `document.paragraphs`, `document.tables`, and `document.sections`.
- Preserve `paragraph.style` when replacing text in place.
- A table cell's text is the concatenation of its paragraph runs.
- Insert a page break with a run-level break, not extra empty paragraphs.
- Save with `document.save(target)` and reopen the target to verify.
- Do not assume a template's placeholder text is absent; search for it.
