from __future__ import annotations

import platform
import sys
from pathlib import Path

_BUILTIN_EMBEDDING_MODELS: dict[str, tuple[str, int]] = {
    "BAAI/bge-small-en-v1.5": ("embeddings/BAAI-bge-small-en-v1.5", 384),
    "sentence-transformers/all-MiniLM-L6-v2": (
        "embeddings/sentence-transformers-all-MiniLM-L6-v2",
        384,
    ),
    "intfloat/multilingual-e5-small": (
        "embeddings/intfloat-multilingual-e5-small",
        384,
    ),
    "BAAI/bge-base-en-v1.5": ("embeddings/BAAI-bge-base-en-v1.5", 768),
    "nomic-ai/nomic-embed-text-v1.5": ("embeddings/nomic-ai-nomic-embed-text-v1.5", 768),
    "mixedbread-ai/mxbai-embed-large": ("embeddings/mixedbread-ai-mxbai-embed-large", 1024),
    "Snowflake/snowflake-arctic-embed-m": ("embeddings/Snowflake-snowflake-arctic-embed-m", 768),
    "sentence-transformers/paraphrase-MiniLM-L3-v2": (
        "embeddings/sentence-transformers-paraphrase-MiniLM-L3-v2",
        384,
    ),
    "Alibaba-NLP/gte-small": ("embeddings/Alibaba-NLP-gte-small", 384),
    "sentence-transformers/all-mpnet-base-2": (
        "embeddings/sentence-transformers-all-mpnet-base-2",
        768,
    ),
}

_BUILTIN_RERANKER_MODELS: dict[str, str] = {
    "BAAI/bge-reranker-v2-m3": "rerankers/BAAI-bge-reranker-v2-m3",
    "cross-encoder/ms-marco-MiniLM-L-12-v2": "rerankers/cross-encoder-ms-marco-MiniLM-L-12-v2",
    "BAAI/bge-reranker-base": "rerankers/BAAI-bge-reranker-base",
    "BAAI/bge-reranker-large": "rerankers/BAAI-bge-reranker-large",
    "jinaai/jina-reranker-v2-base-multilingual": "rerankers/jinaai-jina-reranker-v2-base-multilingual",
    "mixedbread-ai/mxbai-rerank-base-v1": "rerankers/mixedbread-ai-mxbai-rerank-base-v1",
    "cross-encoder/ms-marco-TinyBERT-L-2-v2": "rerankers/cross-encoder-ms-marco-TinyBERT-L-2-v2",
}


def local_model_inference_unsupported_reason(
    *, sys_platform: str | None = None, platform_machine: str | None = None
) -> str | None:
    """Return why bundled local ML inference is disabled on this platform."""
    resolved_platform = sys.platform if sys_platform is None else sys_platform
    resolved_machine = platform.machine() if platform_machine is None else platform_machine
    if resolved_platform == "darwin" and resolved_machine.lower() in {"x86_64", "amd64"}:
        return (
            "Local Hugging Face embedding and reranking models are disabled on Intel macOS. "
            "Choose a remote embedding or reranking provider instead."
        )
    return None


def get_builtin_embedding_dimension(model_name: str) -> int | None:
    item = _BUILTIN_EMBEDDING_MODELS.get(model_name)
    if item is None:
        return None
    _, dimension = item
    return dimension


def get_builtin_embedding_path(models_root: str, model_name: str) -> Path | None:
    item = _BUILTIN_EMBEDDING_MODELS.get(model_name)
    if item is None:
        return None
    _, _ = item
    safe_name = model_name.replace("/", "--").replace(":", "--")
    return Path(models_root, "embeddings", safe_name)


def get_builtin_reranker_path(models_root: str, model_name: str) -> Path | None:
    if model_name not in _BUILTIN_RERANKER_MODELS:
        return None
    safe_name = model_name.replace("/", "--").replace(":", "--")
    return Path(models_root, "rerankers", safe_name)
