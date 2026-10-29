from __future__ import annotations

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
    relative_path, _ = item
    return Path(models_root, relative_path)


def get_builtin_reranker_path(models_root: str, model_name: str) -> Path | None:
    relative_path = _BUILTIN_RERANKER_MODELS.get(model_name)
    if relative_path is None:
        return None
    return Path(models_root, relative_path)
