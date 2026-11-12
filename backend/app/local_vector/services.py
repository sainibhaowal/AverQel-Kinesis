"""
Local in-process embedding and reranking services.

Replaces the external inference server with direct sentence-transformers usage.
Supports both PyTorch and ONNX Runtime for maximum compatibility.
"""

from __future__ import annotations

import hashlib
import logging
import struct
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.core.averqel_home import averqel_models_path, averqel_rerankers_path
from app.core.config import Settings
from app.core.errors import ApiError
from app.system.services.metrics_service import (
    EMBEDDING_PROVIDER_FAILURES_TOTAL,
    EMBEDDING_PROVIDER_LATENCY_SECONDS,
)

logger = logging.getLogger(__name__)

UTC = getattr(datetime, "UTC", UTC)

# ---------------------------------------------------------------------------
# Model Management
# ---------------------------------------------------------------------------

_EMBEDDING_MODELS: dict[str, Any] = {}
_RERANKER_MODELS: dict[str, Any] = {}
_MODEL_LOAD_LOCKS: dict[str, Any] = {}


def _get_model_lock(model_id: str) -> Any:
    import threading

    if model_id not in _MODEL_LOAD_LOCKS:
        _MODEL_LOAD_LOCKS[model_id] = threading.Lock()
    return _MODEL_LOAD_LOCKS[model_id]


def _resolve_embedding_model_path(model_name: str) -> Path | None:
    """Check if model exists under the app's local embedding model directory."""
    models_dir = averqel_models_path()
    for safe_name in (
        model_name.replace("/", "--").replace(":", "--"),
        model_name.replace("/", "-").replace(":", "-"),
    ):
        model_dir = models_dir / safe_name
        if _is_loadable_model_dir(model_dir):
            return model_dir
    return None


def _resolve_reranker_model_path(model_name: str) -> Path | None:
    """Check if reranker exists under the app's local reranker model directory."""
    models_dir = averqel_rerankers_path()
    for safe_name in (
        model_name.replace("/", "--").replace(":", "--"),
        model_name.replace("/", "-").replace(":", "-"),
    ):
        model_dir = models_dir / safe_name
        if _is_loadable_model_dir(model_dir):
            return model_dir
    return None


def _is_loadable_model_dir(path: Path) -> bool:
    if not path.is_dir() or not (path / "config.json").is_file():
        return False
    return any(
        (path / filename).is_file()
        for filename in ("model.safetensors", "pytorch_model.bin", "model.onnx")
    )


def load_local_embedding_model(model_name: str, settings: Settings) -> Any:
    """Load a sentence-transformers embedding model locally.

    Only the application-managed local model directory is allowed. Runtime
    Hugging Face resolution is deliberately disabled for the offline desktop.
    """
    global _EMBEDDING_MODELS

    if model_name in _EMBEDDING_MODELS:
        return _EMBEDDING_MODELS[model_name]

    lock = _get_model_lock(f"embed:{model_name}")
    with lock:
        if model_name in _EMBEDDING_MODELS:
            return _EMBEDDING_MODELS[model_name]

        # Try local model first
        local_path = _resolve_embedding_model_path(model_name)
        model_kwargs = {
            "device": "cpu",
            "trust_remote_code": False,
        }

        try:
            # Import here to avoid hard dependency at module level
            from sentence_transformers import SentenceTransformer

            if local_path:
                logger.info(f"Loading local embedding model from {local_path}")
                model = SentenceTransformer(str(local_path), **model_kwargs)
            else:
                raise ApiError(
                    code="EMBEDDING_MODEL_NOT_INSTALLED",
                    message=(
                        f"The local embedding model {model_name} is not installed in "
                        "~/.averqel-harness/models/embeddings."
                    ),
                    status_code=503,
                )

            _EMBEDDING_MODELS[model_name] = model
            return model

        except Exception as exc:
            logger.warning(f"Failed to load sentence-transformers model {model_name}: {exc}")
            # Try ONNX Runtime
            try:
                from optimum.onnxruntime import ORTModelForFeatureExtraction
                from transformers import AutoTokenizer

                if local_path:
                    onnx_path = local_path / "onnx"
                    if onnx_path.exists():
                        logger.info(f"Loading ONNX embedding model from {onnx_path}")
                        tokenizer = AutoTokenizer.from_pretrained(str(local_path))
                        model = ORTModelForFeatureExtraction.from_pretrained(str(onnx_path))
                        _EMBEDDING_MODELS[model_name] = (model, tokenizer)
                        return _EMBEDDING_MODELS[model_name]
            except Exception:
                pass

            if isinstance(exc, ApiError):
                raise
            raise ApiError(
                code="EMBEDDING_MODEL_LOAD_FAILED",
                message=f"Could not load local embedding model {model_name}",
                status_code=503,
            ) from exc


def load_local_reranker_model(model_name: str, settings: Settings) -> Any:
    """Load a cross-encoder reranker model locally."""
    global _RERANKER_MODELS

    if model_name in _RERANKER_MODELS:
        return _RERANKER_MODELS[model_name]

    lock = _get_model_lock(f"rerank:{model_name}")
    with lock:
        if model_name in _RERANKER_MODELS:
            return _RERANKER_MODELS[model_name]

        local_path = _resolve_reranker_model_path(model_name)
        model_kwargs = {
            "device": "cpu",
            "trust_remote_code": False,
        }

        try:
            from sentence_transformers import CrossEncoder

            if local_path:
                logger.info(f"Loading local reranker model from {local_path}")
                model = CrossEncoder(str(local_path), **model_kwargs)
            else:
                raise ApiError(
                    code="RERANKER_MODEL_NOT_INSTALLED",
                    message=(
                        f"The local reranker model {model_name} is not installed in "
                        "~/.averqel-harness/models/rerankers."
                    ),
                    status_code=503,
                )

            _RERANKER_MODELS[model_name] = model
            return model

        except Exception as exc:
            logger.warning(f"Failed to load cross-encoder model {model_name}: {exc}")
            if isinstance(exc, ApiError):
                raise
            raise ApiError(
                code="RERANKER_MODEL_LOAD_FAILED",
                message=f"Could not load local reranker model {model_name}",
                status_code=503,
            ) from exc


def download_embedding_model(model_name: str, settings: Settings) -> Path:
    """Explicitly download an embedding model into the user's local model store."""
    import json

    target_dir = averqel_models_path() / model_name.replace("/", "--").replace(":", "--")
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=model_name,
            local_dir=str(target_dir),
            ignore_patterns=["*.msgpack", "*.h5", "*.ot", "*.tflite", "flax_model*"],
            max_workers=4,
        )
        from app.providers.services.builtin_local_models import get_builtin_embedding_dimension

        with (target_dir / "averqel_model.json").open("w", encoding="utf-8") as stream:
            json.dump(
                {
                    "model_id": model_name,
                    "dimension": get_builtin_embedding_dimension(model_name) or 384,
                    "installed": True,
                    "source": "huggingface",
                    "huggingface_url": f"https://huggingface.co/{model_name}",
                    "path": str(target_dir),
                },
                stream,
                indent=2,
            )
        return target_dir
    except Exception as exc:
        raise ApiError(
            code="MODEL_DOWNLOAD_FAILED",
            message=f"Failed to download embedding model '{model_name}' from Hugging Face: {exc}",
            status_code=502,
        ) from exc


def download_reranker_model(model_name: str, settings: Settings) -> Path:
    """Explicitly download a reranker model into the user's local model store."""
    import json

    target_dir = averqel_rerankers_path() / model_name.replace("/", "--").replace(":", "--")
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=model_name,
            local_dir=str(target_dir),
            ignore_patterns=["*.msgpack", "*.h5", "*.ot", "flax_model.msgpack", "tf_model.h5"],
            max_workers=4,
        )
        with (target_dir / "averqel_model.json").open("w", encoding="utf-8") as stream:
            json.dump(
                {
                    "model_id": model_name,
                    "architecture": "cross-encoder",
                    "installed": True,
                    "source": "huggingface",
                    "huggingface_url": f"https://huggingface.co/{model_name}",
                    "path": str(target_dir),
                },
                stream,
                indent=2,
            )
        return target_dir
    except Exception as exc:
        raise ApiError(
            code="MODEL_DOWNLOAD_FAILED",
            message=f"Failed to download reranker model '{model_name}' from Hugging Face: {exc}",
            status_code=502,
        ) from exc


# ---------------------------------------------------------------------------
# Embedding Service
# ---------------------------------------------------------------------------


class RetryableEmbeddingError(Exception):
    pass


class NonRetryableEmbeddingError(Exception):
    pass


class EmbeddingProviderNotConfiguredError(NonRetryableEmbeddingError):
    pass


@dataclass(slots=True, frozen=True)
class EmbeddingRunMetadata:
    provider: str
    model: str
    provider_config_id: uuid.UUID | None = None
    source: str = "local"
    fallback_used: bool = False
    failure_code: str | None = None
    failure_message_redacted: str | None = None


@dataclass(slots=True, frozen=True)
class EmbeddingRunResult:
    vectors: list[list[float]]
    metadata: EmbeddingRunMetadata


class LocalEmbeddingService:
    """In-process embedding service using sentence-transformers."""

    _state: dict[str, Any] = {"failures": 0, "opened_until": None}

    def __init__(self, settings: Settings, db: Session | None = None) -> None:
        self.settings = settings
        self.db = db
        self._last_run_metadata: EmbeddingRunMetadata | None = None
        self._last_failure_context: dict[str, Any] | None = None

    def embed_many(
        self,
        texts: list[str],
        *,
        tenant_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        input_type: Literal["query", "passage"] = "passage",
    ) -> list[list[float]]:
        self._last_run_metadata = None
        self._last_failure_context = None

        try:
            self._guard_circuit_state()
            return self._embed_with_retry(
                texts,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                actor_user_id=actor_user_id,
                input_type=input_type,
            )
        except Exception as exc:
            self._record_failure()
            EMBEDDING_PROVIDER_FAILURES_TOTAL.labels(reason="unexpected").inc()
            raise ApiError(
                code="EMBEDDING_PROVIDER_UNAVAILABLE",
                message="Local embedding provider request failed.",
                status_code=503,
                details=self._last_failure_context or {},
            ) from exc

    def embed_many_with_metadata(
        self,
        texts: list[str],
        *,
        tenant_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        input_type: Literal["query", "passage"] = "passage",
    ) -> EmbeddingRunResult:
        vectors = self.embed_many(
            texts,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            input_type=input_type,
        )
        metadata = self._last_run_metadata or EmbeddingRunMetadata(
            provider="local",
            model=self.settings.embedding_model,
            source="local",
        )
        return EmbeddingRunResult(vectors=vectors, metadata=metadata)

    def _embed_with_retry(
        self,
        texts: list[str],
        *,
        tenant_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        input_type: Literal["query", "passage"] = "passage",
    ) -> list[list[float]]:
        from tenacity import (
            Retrying,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential_jitter,
        )

        retryer = Retrying(
            retry=retry_if_exception_type(RetryableEmbeddingError),
            stop=stop_after_attempt(self.settings.provider_retry_attempts),
            wait=wait_exponential_jitter(initial=1, max=8),
            reraise=True,
        )
        for attempt in retryer:
            with attempt:
                return self._embed_local(
                    texts,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    actor_user_id=actor_user_id,
                    input_type=input_type,
                )
        raise ApiError(
            code="EMBEDDING_PROVIDER_UNAVAILABLE",
            message="Local embedding retry policy exhausted.",
            status_code=503,
        )

    def _embed_local(
        self,
        texts: list[str],
        *,
        tenant_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        input_type: Literal["query", "passage"] = "passage",
    ) -> list[list[float]]:
        start = time.monotonic()

        # Load model
        model = load_local_embedding_model(self.settings.embedding_model, self.settings)

        # Prepare inputs
        if input_type == "query":
            # Some models benefit from query prefix
            if (
                "e5" in self.settings.embedding_model.lower()
                or "bge" in self.settings.embedding_model.lower()
            ):
                texts = [f"query: {t}" for t in texts]

        # Encode
        embeddings = model.encode(
            texts,
            batch_size=self.settings.embedding_batch_size,
            normalize_embeddings=self.settings.embedding_normalize,
            show_progress_bar=False,
            convert_to_numpy=True,
        )

        # Validate dimensions
        expected_dim = self.settings.embedding_dimension
        vectors = []
        for vec in embeddings:
            vec_list = vec.tolist() if hasattr(vec, "tolist") else list(vec)
            if len(vec_list) != expected_dim:
                raise NonRetryableEmbeddingError(
                    f"Embedding dimension mismatch: expected {expected_dim}, got {len(vec_list)}"
                )
            vectors.append(vec_list)

        elapsed = time.monotonic() - start
        EMBEDDING_PROVIDER_LATENCY_SECONDS.labels(
            provider="local",
            model=self.settings.embedding_model,
        ).observe(elapsed)

        self._record_success()
        self._last_run_metadata = EmbeddingRunMetadata(
            provider="local",
            model=self.settings.embedding_model,
            source="local",
        )
        return vectors

    def _guard_circuit_state(self) -> None:
        opened_until = self._state.get("opened_until")
        if opened_until is None:
            return
        if datetime.now(tz=UTC) < opened_until:
            raise ApiError(
                code="PROVIDER_CIRCUIT_OPEN",
                message="Local embedding circuit breaker is open.",
                status_code=503,
            )
        self._state["opened_until"] = None
        self._state["failures"] = 0

    def _record_failure(self) -> None:
        self._state["failures"] = self._state.get("failures", 0) + 1
        if self._state["failures"] >= self.settings.provider_circuit_breaker_threshold:
            self._state["opened_until"] = datetime.now(tz=UTC) + timedelta(
                seconds=self.settings.provider_circuit_breaker_reset_seconds
            )

    def _record_success(self) -> None:
        self._state["failures"] = 0
        self._state["opened_until"] = None


# ---------------------------------------------------------------------------
# Reranker Service
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class RerankRunMetadata:
    enabled: bool
    applied: bool
    provider: str | None = None
    model: str | None = None
    source: str | None = None
    provider_config_id: uuid.UUID | None = None
    candidate_count: int = 0
    selected_count: int = 0
    latency_ms: float | None = None
    failure_reason: str | None = None
    selection_notes: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class RerankRunResult:
    chunks: list[Any]
    metadata: RerankRunMetadata


class LocalRerankerService:
    """In-process reranking service using cross-encoder models."""

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def rerank_chunks(
        self,
        *,
        tenant_id: uuid.UUID,
        workspace_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        query: str,
        chunks: list[Any],
        top_n: int,
    ) -> RerankRunResult:
        if not self.settings.reranking_enabled or not chunks or top_n <= 0:
            return RerankRunResult(
                chunks=chunks[:top_n] if top_n > 0 else [],
                metadata=RerankRunMetadata(
                    enabled=bool(self.settings.reranking_enabled),
                    applied=False,
                    candidate_count=len(chunks),
                    selected_count=min(len(chunks), max(top_n, 0)),
                    failure_reason=(
                        None if self.settings.reranking_enabled else "reranking_disabled"
                    ),
                ),
            )

        if self.settings.reranking_provider == "disabled":
            return RerankRunResult(
                chunks=chunks[:top_n],
                metadata=RerankRunMetadata(
                    enabled=False,
                    applied=False,
                    candidate_count=len(chunks),
                    selected_count=min(len(chunks), top_n),
                    failure_reason="reranking_provider_disabled",
                ),
            )

        # Load local reranker model
        try:
            model = load_local_reranker_model(self.settings.reranking_model, self.settings)
        except Exception as exc:
            logger.warning(f"Reranker model load failed: {exc}")
            return RerankRunResult(
                chunks=chunks[:top_n],
                metadata=RerankRunMetadata(
                    enabled=True,
                    applied=False,
                    candidate_count=len(chunks),
                    selected_count=min(len(chunks), top_n),
                    failure_reason="reranker_model_load_failed",
                ),
            )

        started = time.perf_counter()
        try:
            # Prepare pairs for cross-encoder
            pairs = [(query, chunk.content) for chunk in chunks]

            # Score
            scores = model.predict(pairs, show_progress_bar=False)

            # Sort by score
            scored = list(zip(chunks, scores, strict=False))
            scored.sort(key=lambda x: float(x[1]), reverse=True)

            reranked = [chunk for chunk, _ in scored]
            if len(reranked) < top_n:
                reranked_ids = {chunk.chunk_id for chunk in reranked}
                reranked.extend(chunk for chunk in chunks if chunk.chunk_id not in reranked_ids)

            final_chunks = reranked[:top_n]
            final_chunk_ids = {chunk.chunk_id for chunk in final_chunks}

            # Update similarity scores
            for chunk, score in scored:
                if chunk.chunk_id in final_chunk_ids:
                    chunk.similarity_score = float(score)

            latency_ms = round((time.perf_counter() - started) * 1000, 2)

            return RerankRunResult(
                chunks=final_chunks,
                metadata=RerankRunMetadata(
                    enabled=True,
                    applied=True,
                    provider="local",
                    model=self.settings.reranking_model,
                    source="local",
                    candidate_count=len(chunks),
                    selected_count=len(final_chunks),
                    latency_ms=latency_ms,
                ),
            )
        except Exception as exc:
            logger.warning(f"Reranking failed: {exc}")
            return RerankRunResult(
                chunks=chunks[:top_n],
                metadata=RerankRunMetadata(
                    enabled=True,
                    applied=False,
                    provider="local",
                    model=self.settings.reranking_model,
                    source="local",
                    candidate_count=len(chunks),
                    selected_count=min(len(chunks), top_n),
                    failure_reason=type(exc).__name__,
                ),
            )


# ---------------------------------------------------------------------------
# Deterministic Fallback (hash-based)
# ---------------------------------------------------------------------------


def deterministic_embedding(text: str, dimension: int = 384) -> list[float]:
    """Deterministic hash-based embedding for testing/fallback."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    for idx in range(dimension):
        start = (idx * 4) % len(digest)
        chunk = digest[start : start + 4]
        integer_value = struct.unpack("!I", chunk)[0]
        normalized = (integer_value / 2**32) * 2 - 1
        values.append(round(normalized, 7))
    return values
