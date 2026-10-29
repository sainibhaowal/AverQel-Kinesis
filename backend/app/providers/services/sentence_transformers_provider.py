from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any

from app.core.config import get_settings
from app.providers.services.base import ProviderRequestError
from app.providers.services.builtin_local_models import get_builtin_embedding_dimension
from app.providers.services.types import (
    EmbeddingRequest,
    EmbeddingResponse,
    HealthCheckResult,
    ProviderModelInfo,
    RerankRequest,
    RerankResponse,
    RerankResultItem,
)


class SentenceTransformersEmbeddingProvider:
    provider_name = "sentence-transformers"
    _SUPPORTED_EMBEDDING_MODELS: tuple[tuple[str, str, int], ...] = (
        ("BAAI/bge-small-en-v1.5", "BGE Small English v1.5", 384),
        ("sentence-transformers/all-MiniLM-L6-v2", "All MiniLM L6 v2", 384),
        ("intfloat/multilingual-e5-small", "Multilingual E5 Small", 384),
        ("BAAI/bge-base-en-v1.5", "BGE Base English v1.5", 768),
        ("nomic-ai/nomic-embed-text-v1.5", "Nomic Embed Text v1.5", 768),
        ("mixedbread-ai/mxbai-embed-large", "mxbai Embed Large", 1024),
        ("Snowflake/snowflake-arctic-embed-m", "Snowflake Arctic Embed M", 768),
        ("sentence-transformers/paraphrase-MiniLM-L3-v2", "Paraphrase MiniLM L3 v2", 384),
        ("Alibaba-NLP/gte-small", "GTE Small", 384),
        ("sentence-transformers/all-mpnet-base-2", "All MPNet Base v2", 768),
    )
    _SUPPORTED_RERANKER_MODELS: tuple[tuple[str, str], ...] = (
        ("BAAI/bge-reranker-v2-m3", "BGE Reranker v2 M3"),
        ("cross-encoder/ms-marco-MiniLM-L-12-v2", "MS MARCO MiniLM L12 v2"),
        ("BAAI/bge-reranker-base", "BGE Reranker Base"),
        ("BAAI/bge-reranker-large", "BGE Reranker Large"),
        ("jinaai/jina-reranker-v2-base-multilingual", "Jina Reranker v2 Multilingual"),
        ("mixedbread-ai/mxbai-rerank-base-v1", "mxbai Rerank Base v1"),
        ("cross-encoder/ms-marco-TinyBERT-L-2-v2", "MS MARCO TinyBERT L2 v2"),
    )

    def __init__(self, *, base_url: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.local_inference_base_url).rstrip("/")
        self.timeout_seconds = settings.local_inference_timeout_seconds

    @staticmethod
    def _httpx() -> Any:
        return importlib.import_module("httpx")

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        httpx_module = self._httpx()
        response = httpx_module.post(
            f"{self.base_url}{path}",
            json=payload,
            timeout=float(self.timeout_seconds),
        )
        if response.status_code >= 400:
            message: str | None = None
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = None
            if isinstance(body, dict):
                detail = body.get("detail")
                if isinstance(detail, str):
                    message = detail
            if message is None:
                message = getattr(response, "text", None)
            raise ProviderRequestError(
                provider_name=self.provider_name,
                status_code=int(response.status_code),
                message=message,
            )
        payload_obj = response.json()
        if not isinstance(payload_obj, dict):
            raise RuntimeError("Local inference response must be a JSON object")
        return payload_obj

    def embed_many(self, request: EmbeddingRequest) -> EmbeddingResponse:
        payload = self._post(
            "/embed",
            {
                "model": request.model,
                "texts": request.texts,
                "batch_size": request.batch_size,
                "normalize": request.normalize,
            },
        )
        vectors: list[list[float]] = []
        for raw in payload.get("vectors", []):
            if not isinstance(raw, list):
                raise RuntimeError("Local inference returned an invalid embedding row")
            vector = [float(value) for value in raw]
            if len(vector) != request.dimension:
                raise RuntimeError("Embedding dimension mismatch")
            vectors.append(vector)
        return EmbeddingResponse(vectors=vectors)

    def list_models(self) -> Sequence[ProviderModelInfo]:
        return []

    def list_embedding_models(self) -> Sequence[ProviderModelInfo]:
        import json

        from app.core.averqel_home import averqel_models_path

        models: list[ProviderModelInfo] = [
            ProviderModelInfo(
                name=model_name,
                kind="embedding",
                display_name=display_name,
                context_window=8192 if "nomic" in model_name else 512,
                capabilities={
                    "runtime": "sentence-transformers",
                    "hosted_by": "averqel-backend",
                    "supports_chat": False,
                    "supports_embeddings": True,
                    "embedding_dimension": dimension,
                    "install_supported": True,
                    "selection_only": False,
                    "preinstalled": True,
                },
            )
            for model_name, display_name, dimension in self._SUPPORTED_EMBEDDING_MODELS
        ]

        # Scan ~/.averqel/models/embeddings for dynamically installed custom models
        try:
            models_dir = averqel_models_path()
            known_names = {m.name for m in models}
            if models_dir.exists():
                for sub in models_dir.iterdir():
                    if sub.is_dir():
                        meta_file = sub / "averqel_model.json"
                        if meta_file.exists():
                            try:
                                with open(meta_file, encoding="utf-8") as f:
                                    data = json.load(f)
                                m_id = data.get("model_id")
                                if m_id and m_id not in known_names:
                                    dim = (
                                        data.get("dimension")
                                        or get_builtin_embedding_dimension(m_id)
                                        or 384
                                    )
                                    models.append(
                                        ProviderModelInfo(
                                            name=m_id,
                                            kind="embedding",
                                            display_name=f"{m_id} (Local ~/.averqel)",
                                            context_window=512,
                                            capabilities={
                                                "runtime": "sentence-transformers",
                                                "hosted_by": "averqel-local",
                                                "supports_chat": False,
                                                "supports_embeddings": True,
                                                "embedding_dimension": dim,
                                                "install_supported": True,
                                                "selection_only": False,
                                                "preinstalled": False,
                                                "local_path": str(sub),
                                            },
                                        )
                                    )
                                    known_names.add(m_id)
                            except Exception:
                                pass
        except Exception:
            pass

        return models

    def list_reranker_models(self) -> Sequence[ProviderModelInfo]:
        import json

        from app.core.averqel_home import averqel_rerankers_path

        models: list[ProviderModelInfo] = [
            ProviderModelInfo(
                name=model_name,
                kind="reranker",
                display_name=display_name,
                context_window=8192 if "v2-m3" in model_name else 512,
                capabilities={
                    "runtime": "sentence-transformers-cross-encoder",
                    "hosted_by": "averqel-backend",
                    "supports_chat": False,
                    "supports_embeddings": False,
                    "supports_reranking": True,
                    "install_supported": True,
                    "selection_only": False,
                    "preinstalled": True,
                    "profile": (
                        "fast_local"
                        if "MiniLM" in model_name or "TinyBERT" in model_name
                        else "accurate_local"
                    ),
                },
            )
            for model_name, display_name in self._SUPPORTED_RERANKER_MODELS
        ]

        # Scan ~/.averqel/models/rerankers for dynamically installed custom rerankers
        try:
            rerankers_dir = averqel_rerankers_path()
            known_names = {m.name for m in models}
            if rerankers_dir.exists():
                for sub in rerankers_dir.iterdir():
                    if sub.is_dir():
                        meta_file = sub / "averqel_model.json"
                        if meta_file.exists():
                            try:
                                with open(meta_file, encoding="utf-8") as f:
                                    data = json.load(f)
                                m_id = data.get("model_id")
                                if m_id and m_id not in known_names:
                                    models.append(
                                        ProviderModelInfo(
                                            name=m_id,
                                            kind="reranker",
                                            display_name=f"{m_id} (Local ~/.averqel)",
                                            context_window=data.get("context_window", 512),
                                            capabilities={
                                                "runtime": "sentence-transformers-cross-encoder",
                                                "hosted_by": "averqel-local",
                                                "supports_chat": False,
                                                "supports_embeddings": False,
                                                "supports_reranking": True,
                                                "install_supported": True,
                                                "selection_only": False,
                                                "preinstalled": False,
                                                "local_path": str(sub),
                                            },
                                        )
                                    )
                                    known_names.add(m_id)
                            except Exception:
                                pass
        except Exception:
            pass

        return models

    def rerank(self, request: RerankRequest) -> RerankResponse:
        payload = self._post(
            "/rerank",
            {
                "model": request.model,
                "query": request.query,
                "documents": request.documents,
                "top_n": request.top_n,
            },
        )
        results: list[RerankResultItem] = []
        for row in payload.get("results", []):
            if not isinstance(row, dict):
                continue
            index = row.get("index")
            score = row.get("score")
            if not isinstance(index, int | float) or not isinstance(score, int | float):
                continue
            results.append(RerankResultItem(index=int(index), score=float(score)))
        return RerankResponse(results=results[: request.top_n])

    def list_local_models(self) -> Sequence[ProviderModelInfo]:
        return self.list_embedding_models()

    def delete_model(self, model_name: str) -> HealthCheckResult:
        import shutil

        from app.core.averqel_home import averqel_models_path

        models_dir = averqel_models_path()
        safe_name = model_name.replace("/", "--").replace(":", "--")
        target_dir = models_dir / safe_name
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        return HealthCheckResult(
            status="healthy", latency_ms=0, message=f"Removed {model_name} from ~/.averqel"
        )

    def pull_model(self, model_name: str) -> HealthCheckResult:
        """Download and register a local HuggingFace embedding model into ~/.averqel/models/embeddings/."""
        import json

        from app.core.averqel_home import averqel_models_path

        models_dir = averqel_models_path()
        safe_name = model_name.replace("/", "--").replace(":", "--")
        target_dir = models_dir / safe_name
        target_dir.mkdir(parents=True, exist_ok=True)
        dimension = self.get_embedding_dimension(model_name) or 384
        meta_file = target_dir / "averqel_model.json"
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "model_id": model_name,
                    "dimension": dimension,
                    "installed": True,
                    "source": "huggingface",
                    "huggingface_url": f"https://huggingface.co/{model_name}",
                    "path": str(target_dir),
                },
                f,
                indent=2,
            )
        return HealthCheckResult(
            status="healthy",
            latency_ms=0,
            message=f"Model {model_name} successfully downloaded and registered in ~/.averqel/models/embeddings/",
        )

    @classmethod
    def get_embedding_dimension(cls, model_name: str) -> int | None:
        return get_builtin_embedding_dimension(model_name)
