from __future__ import annotations

import importlib
import time
from collections.abc import Sequence
from typing import Any

from app.core.averqel_home import averqel_models_path, averqel_rerankers_path
from app.core.config import get_settings
from app.providers.services.base import ProviderCapabilityError, ProviderRequestError
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

    def __init__(self, *, base_url: str | None = None, local_runtime: bool = False) -> None:
        self.settings = get_settings()
        self.local_runtime = local_runtime
        # The desktop runtime is in-process.  ``base_url`` remains supported
        # only for legacy deployments that still call this adapter directly;
        # the desktop registry always sets ``local_runtime=True``.
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_seconds = self.settings.local_inference_timeout_seconds

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
        if self.local_runtime:
            from app.local_vector.services import load_local_embedding_model

            texts = list(request.texts)
            if request.input_type == "query" and (
                "e5" in request.model.lower() or "bge" in request.model.lower()
            ):
                texts = [f"query: {text}" for text in texts]
            model = load_local_embedding_model(request.model, self.settings)
            encoded = model.encode(
                texts,
                batch_size=request.batch_size,
                normalize_embeddings=request.normalize,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            vectors = []
            for raw in encoded:
                vector = raw.tolist() if hasattr(raw, "tolist") else list(raw)
                if len(vector) != request.dimension:
                    raise RuntimeError(
                        f"Embedding dimension mismatch: expected {request.dimension}, got {len(vector)}"
                    )
                vectors.append([float(value) for value in vector])
            return EmbeddingResponse(vectors=vectors)

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

        models: list[ProviderModelInfo] = []
        for model_name, display_name, dimension in self._SUPPORTED_EMBEDDING_MODELS:
            if self.local_runtime and not self._local_model_exists(model_name, kind="embedding"):
                continue
            models.append(
                ProviderModelInfo(
                    name=model_name,
                    kind="embedding",
                    display_name=display_name,
                    context_window=8192 if "nomic" in model_name else 512,
                    capabilities={
                        "runtime": "sentence-transformers",
                        "hosted_by": "averqel-local",
                        "supports_chat": False,
                        "supports_embeddings": True,
                        "embedding_dimension": dimension,
                        "install_supported": False,
                        "selection_only": True,
                        "preinstalled": True,
                        "local_path": str(self._local_model_path(model_name, kind="embedding")),
                    },
                )
            )

        # Scan the app-owned models directory for downloaded custom models.
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
                                if m_id and m_id not in known_names and (
                                    not self.local_runtime
                                    or self._is_loadable_model_dir(sub)
                                ):
                                    dim = (
                                        data.get("dimension")
                                        or get_builtin_embedding_dimension(m_id)
                                        or 384
                                    )
                                    models.append(
                                        ProviderModelInfo(
                                            name=m_id,
                                            kind="embedding",
                                            display_name=f"{m_id} (Local AverQel model)",
                                            context_window=512,
                                            capabilities={
                                                "runtime": "sentence-transformers",
                                                "hosted_by": "averqel-local",
                                                "supports_chat": False,
                                                "supports_embeddings": True,
                                                "embedding_dimension": dim,
                                                "install_supported": False,
                                                "selection_only": True,
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

        models: list[ProviderModelInfo] = []
        for model_name, display_name in self._SUPPORTED_RERANKER_MODELS:
            if self.local_runtime and not self._local_model_exists(model_name, kind="reranker"):
                continue
            models.append(
                ProviderModelInfo(
                    name=model_name,
                    kind="reranker",
                    display_name=display_name,
                    context_window=8192 if "v2-m3" in model_name else 512,
                    capabilities={
                        "runtime": "sentence-transformers-cross-encoder",
                        "hosted_by": "averqel-local",
                        "supports_chat": False,
                        "supports_embeddings": False,
                        "supports_reranking": True,
                        "install_supported": False,
                        "selection_only": True,
                        "preinstalled": True,
                        "profile": (
                            "fast_local"
                            if "MiniLM" in model_name or "TinyBERT" in model_name
                            else "accurate_local"
                        ),
                        "local_path": str(self._local_model_path(model_name, kind="reranker")),
                    },
                )
            )

        # Scan the app-owned models directory for downloaded custom rerankers.
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
                                if m_id and m_id not in known_names and (
                                    not self.local_runtime
                                    or self._is_loadable_model_dir(sub)
                                ):
                                    models.append(
                                        ProviderModelInfo(
                                            name=m_id,
                                            kind="reranker",
                                            display_name=f"{m_id} (Local AverQel model)",
                                            context_window=data.get("context_window", 512),
                                            capabilities={
                                                "runtime": "sentence-transformers-cross-encoder",
                                                "hosted_by": "averqel-local",
                                                "supports_chat": False,
                                                "supports_embeddings": False,
                                                "supports_reranking": True,
                                                "install_supported": False,
                                                "selection_only": True,
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
        if self.local_runtime:
            from app.local_vector.services import load_local_reranker_model

            model = load_local_reranker_model(request.model, self.settings)
            pairs = [(request.query, document) for document in request.documents]
            raw_scores = model.predict(pairs, show_progress_bar=False)
            scored = [
                RerankResultItem(index=index, score=float(score))
                for index, score in enumerate(raw_scores)
            ]
            scored.sort(key=lambda item: item.score, reverse=True)
            return RerankResponse(results=scored[: request.top_n])

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
        return [*self.list_embedding_models(), *self.list_reranker_models()]

    @staticmethod
    def _safe_model_name(model_name: str) -> str:
        return model_name.replace("/", "--").replace(":", "--")

    @classmethod
    def _local_model_path(cls, model_name: str, *, kind: str):
        root = averqel_models_path() if kind == "embedding" else averqel_rerankers_path()
        return root / cls._safe_model_name(model_name)

    @classmethod
    def _is_loadable_model_dir(cls, path) -> bool:
        if not path.is_dir() or not (path / "config.json").is_file():
            return False
        return any(
            (path / filename).is_file()
            for filename in ("model.safetensors", "pytorch_model.bin", "model.onnx")
        )

    @classmethod
    def _local_model_exists(cls, model_name: str, *, kind: str) -> bool:
        return cls._is_loadable_model_dir(cls._local_model_path(model_name, kind=kind))

    def health_check(self) -> HealthCheckResult:
        if not self.local_runtime:
            raise ProviderCapabilityError("The legacy sentence-transformers HTTP runtime is disabled")
        started = time.perf_counter()
        embedding_count = sum(
            self._local_model_exists(name, kind="embedding")
            for name, _, _ in self._SUPPORTED_EMBEDDING_MODELS
        )
        reranker_count = sum(
            self._local_model_exists(name, kind="reranker")
            for name, _ in self._SUPPORTED_RERANKER_MODELS
        )
        if embedding_count + reranker_count == 0:
            return HealthCheckResult(
                status="unhealthy",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code="local_model_missing",
                error_message_redacted=(
                    "No local embedding or reranker model is installed yet. Download one from Hugging Face."
                ),
            )
        return HealthCheckResult(
            status="healthy",
            latency_ms=int((time.perf_counter() - started) * 1000),
            metadata={"embedding_models": embedding_count, "reranker_models": reranker_count},
        )

    def delete_model(self, model_name: str) -> HealthCheckResult:
        import shutil

        from app.core.averqel_home import averqel_models_path

        models_dir = averqel_models_path()
        safe_name = model_name.replace("/", "--").replace(":", "--")
        target_dir = models_dir / safe_name
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        return HealthCheckResult(
            status="healthy", latency_ms=0, message=f"Removed {model_name} from the local model directory"
        )

    def pull_model(self, model_name: str) -> HealthCheckResult:
        if not self.local_runtime:
            raise ProviderCapabilityError("The legacy sentence-transformers HTTP runtime is disabled")

        from app.local_vector.services import download_embedding_model, download_reranker_model

        if any(model_name == supported for supported, _, _ in self._SUPPORTED_EMBEDDING_MODELS):
            path = download_embedding_model(model_name, self.settings)
            return HealthCheckResult(
                status="healthy",
                latency_ms=0,
                message=f"Downloaded {model_name} from Hugging Face",
                metadata={"model_name": model_name, "local_path": str(path)},
            )
        if any(model_name == supported for supported, _ in self._SUPPORTED_RERANKER_MODELS):
            path = download_reranker_model(model_name, self.settings)
            return HealthCheckResult(
                status="healthy",
                latency_ms=0,
                message=f"Downloaded {model_name} from Hugging Face",
                metadata={"model_name": model_name, "local_path": str(path)},
            )
        raise ProviderCapabilityError(f"Unsupported Hugging Face model: {model_name}")

    @classmethod
    def get_embedding_dimension(cls, model_name: str) -> int | None:
        return get_builtin_embedding_dimension(model_name)
