# Slide checklist

- `prs.slide_width` and `prs.slide_height` define the canvas; do not scale shapes.
- A slide's title placeholder may be absent on custom layouts.
- Text lives in shape text frames; tables are separate shapes.
- Speaker notes are `slide.notes_slide.notes_text_frame`.
- Add pictures from a workspace path and check the file exists first.
- Reopen the deck and assert `len(prs.slides)` before reporting success.
