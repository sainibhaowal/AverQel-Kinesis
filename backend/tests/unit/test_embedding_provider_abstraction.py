from __future__ import annotations

import pytest

from app.providers.services.base import ProviderCapabilityError
from app.providers.services.local_deterministic_provider import (
    LocalDeterministicEmbeddingProvider,
)
from app.providers.services.sentence_transformers_provider import (
    SentenceTransformersEmbeddingProvider,
)
from app.providers.services.types import EmbeddingRequest, RerankRequest


def test_local_deterministic_embedding_provider_returns_expected_dimensions():
    provider = LocalDeterministicEmbeddingProvider()
    response = provider.embed_many(
        EmbeddingRequest(
            texts=["alpha", "beta"],
            model="hash-fallback",
            batch_size=2,
            normalize=True,
            dimension=8,
            timeout_seconds=8,
            provider_name="local-deterministic",
        )
    )
    assert len(response.vectors) == 2
    assert len(response.vectors[0]) == 8


def test_sentence_transformers_embedding_provider_uses_loaded_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"vectors": [[0.1, 0.1, 0.1, 0.1]]}

    class _Httpx:
        @staticmethod
        def post(url: str, *, json: dict[str, object], timeout: float):
            assert url.endswith("/embed")
            assert json["texts"] == ["alpha"]
            assert timeout > 0
            return _Response()

    monkeypatch.setattr(
        SentenceTransformersEmbeddingProvider,
        "_httpx",
        staticmethod(lambda: _Httpx),
    )
    provider = SentenceTransformersEmbeddingProvider()
    response = provider.embed_many(
        EmbeddingRequest(
            texts=["alpha"],
            model="demo-model",
            batch_size=1,
            normalize=True,
            dimension=4,
            timeout_seconds=8,
            provider_name="sentence-transformers",
        )
    )
    assert response.vectors == [[0.1, 0.1, 0.1, 0.1]]


def test_local_model_provider_reports_intel_mac_as_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.providers.services.sentence_transformers_provider as provider_module

    reason = "Local Hugging Face inference is disabled on Intel macOS."
    monkeypatch.setattr(
        provider_module,
        "local_model_inference_unsupported_reason",
        lambda: reason,
    )
    provider = SentenceTransformersEmbeddingProvider(local_runtime=True)

    assert provider.list_embedding_models() == []
    assert provider.list_reranker_models() == []
    health = provider.health_check()
    assert health.status == "unhealthy"
    assert health.error_code == "local_inference_unsupported_platform"
    assert health.error_message_redacted == reason
    with pytest.raises(ProviderCapabilityError, match="disabled on Intel macOS"):
        provider.pull_model("BAAI/bge-small-en-v1.5")
    with pytest.raises(ProviderCapabilityError, match="disabled on Intel macOS"):
        provider.embed_many(
            EmbeddingRequest(
                texts=["query"],
                model="BAAI/bge-small-en-v1.5",
                batch_size=1,
                normalize=True,
                dimension=384,
                timeout_seconds=8,
                provider_name="sentence-transformers",
            )
        )
    with pytest.raises(ProviderCapabilityError, match="disabled on Intel macOS"):
        provider.rerank(
            RerankRequest(
                query="query",
                documents=["document"],
                model="BAAI/bge-reranker-v2-m3",
                top_n=1,
                timeout_seconds=8,
                provider_name="sentence-transformers",
            )
        )


def test_local_model_download_and_inference_routes_reject_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.inference.runtime as runtime_module
    import app.local_vector.services as local_vector_services

    reason = "Local Hugging Face inference is disabled on Intel macOS."
    monkeypatch.setattr(
        local_vector_services,
        "local_model_inference_unsupported_reason",
        lambda: reason,
    )
    monkeypatch.setattr(runtime_module, "local_model_inference_unsupported_reason", lambda: reason)

    from app.core.config import get_settings
    from app.core.errors import ApiError
    from app.inference.runtime import LocalInferenceRuntime

    with pytest.raises(ApiError, match="disabled on Intel macOS"):
        local_vector_services.load_local_embedding_model("model", get_settings())
    with pytest.raises(ApiError, match="disabled on Intel macOS"):
        local_vector_services.download_embedding_model("model", get_settings())
    with pytest.raises(ApiError, match="disabled on Intel macOS"):
        local_vector_services.download_reranker_model("model", get_settings())
    with pytest.raises(RuntimeError, match="disabled on Intel macOS"):
        LocalInferenceRuntime(get_settings()).load_embedding_model("model")


def test_local_model_error_codes_are_registered() -> None:
    from app.system.schemas.errors import is_known_error_code

    assert is_known_error_code("MODEL_DOWNLOAD_FAILED")
    assert is_known_error_code("EMBEDDING_MODEL_LOAD_FAILED")
    assert is_known_error_code("RERANKER_MODEL_LOAD_FAILED")
