"""
Local Vector Package - Fully offline vector search and embedding.

This package provides:
- sqlite-vec based vector similarity search
- FTS5 based full-text search (BM25)
- In-process sentence-transformers embeddings
- In-process cross-encoder reranking
- Bundled LibreOffice for document conversion
- Bundled Tesseract for OCR
"""

from __future__ import annotations

__all__ = [
    "models",
    "services",
    "retrieval",
    "binaries",
]

# Version
__version__ = "1.0.0"
