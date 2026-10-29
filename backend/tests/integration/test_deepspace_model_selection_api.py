"""End-to-end tests for the per-conversation model selection.

The tests run over the real database: the API records a model of a provider
the tenant may use, reads it back, lets DeepSpace choose the model again,
and lists the models the tenant may pick from.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient

from app.auth.dependencies import create_access_token
from app.core.config import get_settings
from app.deepspace.repositories.chat import DeepSpaceChatRepository
from app.deepspace.services.model_selection import ModelSelectionService
from app.platform.database.session import get_session_factory
from app.providers.models.provider_config import ProviderConfig
from app.providers.models.provider_model_cache import ProviderModelCache
from app.providers.services.selection_service import ProviderSelectionService
from tests.conftest import SeededUser


def _conversation(seeded: SeededUser, title: str) -> str:
    """Create a DeepSpace conversation and return its id.

    Args:
        seeded: The conversation's owner.
        title: The conversation's title.

    Returns:
        The conversation id.
    """
    session = get_session_factory()()
    try:
        conversation = DeepSpaceChatRepository(session).create_conversation(
            tenant_id=seeded.tenant_id,
            user_id=seeded.user_id,
            title=title,
            kind="deepspace",
        )
        session.commit()
        return str(conversation.id)
    finally:
        session.rollback()
        session.close()


def _headers(seeded: SeededUser) -> dict[str, str]:
    """Build the request headers for one seeded user.

    Args:
        seeded: The seeded user.

    Returns:
        The headers.
    """
    token = create_access_token(
        user_id=seeded.user_id,
        tenant_id=seeded.tenant_id,
        roles={"admin"},
        settings=get_settings(),
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": str(seeded.tenant_id),
    }


def _provider(
    seeded: SeededUser,
    *,
    display_name: str,
    model_name: str,
    context_window: int = 128_000,
) -> str:
    """Connect a chat provider that published one model.

    Args:
        seeded: The provider's owner.
        display_name: The provider's display name.
        model_name: The model the provider published.
        context_window: The model's context window in tokens.

    Returns:
        The provider config id.
    """
    session = get_session_factory()()
    try:
        provider = ProviderConfig(
            tenant_id=seeded.tenant_id,
            workspace_id=None,
            owner_user_id=seeded.user_id,
            visibility_scope="user",
            provider_type="openai",
            display_name=display_name,
            api_base_url="https://api.openai.com/v1",
            auth_mode="none",
            enabled=True,
            is_local=False,
            supports_chat=True,
            supports_embeddings=False,
            supports_reranking=False,
            supports_model_listing=True,
            supports_model_install=False,
            default_chat_model=model_name,
            default_embedding_model=None,
            default_reranker_model=None,
            timeout_seconds=30,
            priority=10,
            metadata_json={},
        )
        session.add(provider)
        session.flush()
        session.add(
            ProviderModelCache(
                tenant_id=seeded.tenant_id,
                provider_config_id=provider.id,
                model_name=model_name,
                model_kind="chat",
                display_name=model_name,
                context_window=context_window,
                capabilities_json={"supports_image_in": False},
                is_available=True,
            )
        )
        session.commit()
        return str(provider.id)
    finally:
        session.rollback()
        session.close()


def test_a_conversation_records_the_model_it_runs_on(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user("Model Tenant", "model-selection@example.com", "StrongPass!1234", ("admin",))
    headers = _headers(seeded)
    conversation_id = _conversation(seeded, "Model conversation")
    provider_config_id = _provider(
        seeded,
        display_name="OpenAI",
        model_name="gpt-5",
    )

    empty = client.get(f"/api/v1/deepspace/chats/{conversation_id}/model", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["model"] is None
    assert empty.json()["recorded_model"] is None
    assert empty.json()["mode"] == "auto"

    recorded = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/model",
        headers=headers,
        json={"provider_config_id": provider_config_id, "model_name": "gpt-5"},
    )
    assert recorded.status_code == 200
    assert recorded.json()["model"]["model_name"] == "gpt-5"
    assert recorded.json()["mode"] == "manual"
    assert recorded.json()["effective_model"] == "gpt-5"

    read_back = client.get(f"/api/v1/deepspace/chats/{conversation_id}/model", headers=headers)
    assert read_back.status_code == 200
    assert read_back.json()["model"]["provider_config_id"] == provider_config_id
    assert read_back.json()["recorded_provider_config_id"] == provider_config_id
    assert read_back.json()["recorded_model"] == "gpt-5"
    assert read_back.json()["mode"] == "manual"


def test_a_conversation_lets_deepspace_choose_the_model(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user("Auto Tenant", "model-auto@example.com", "StrongPass!1234", ("admin",))
    headers = _headers(seeded)
    conversation_id = _conversation(seeded, "Auto conversation")
    provider_config_id = _provider(
        seeded,
        display_name="OpenAI",
        model_name="gpt-5",
    )

    recorded = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/model",
        headers=headers,
        json={"provider_config_id": provider_config_id, "model_name": "gpt-5"},
    )
    assert recorded.status_code == 200
    assert recorded.json()["mode"] == "manual"

    auto = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/model",
        headers=headers,
        json={"mode": "auto"},
    )
    assert auto.status_code == 200
    assert auto.json()["mode"] == "auto"
    assert auto.json()["recorded_model"] is None

    read_back = client.get(f"/api/v1/deepspace/chats/{conversation_id}/model", headers=headers)
    assert read_back.status_code == 200
    assert read_back.json()["mode"] == "auto"
    assert read_back.json()["recorded_model"] is None


def test_the_tenant_models_are_listed(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user("List Tenant", "model-list@example.com", "StrongPass!1234", ("admin",))
    headers = _headers(seeded)
    provider_config_id = _provider(
        seeded,
        display_name="OpenAI",
        model_name="gpt-5",
        context_window=400_000,
    )

    listed = client.get("/api/v1/deepspace/chats/models", headers=headers)
    assert listed.status_code == 200
    models = listed.json()["models"]
    assert len(models) == 1
    model = models[0]
    assert model["provider_config_id"] == provider_config_id
    assert model["provider_type"] == "openai"
    assert model["provider_name"] == "OpenAI"
    assert model["model_name"] == "gpt-5"
    assert model["context_window"] == 400_000
    assert model["supports_images"] is False
    assert model["supports_tools"] is True


def test_a_model_selection_for_another_tenants_provider_is_refused(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user("First Tenant", "model-first@example.com", "StrongPass!1234", ("admin",))
    other = seed_user("Second Tenant", "model-second@example.com", "StrongPass!1234", ("admin",))
    headers = _headers(seeded)
    conversation_id = _conversation(seeded, "Model conversation")
    other_provider_config_id = _provider(
        other,
        display_name="Other OpenAI",
        model_name="gpt-5",
    )

    refused = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/model",
        headers=headers,
        json={"provider_config_id": other_provider_config_id, "model_name": "gpt-5"},
    )
    assert refused.status_code == 400


def test_a_model_selection_without_a_provider_is_refused(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user("Model Tenant", "model-refused@example.com", "StrongPass!1234", ("admin",))
    headers = _headers(seeded)
    conversation_id = _conversation(seeded, "Model conversation")

    refused = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/model",
        headers=headers,
        json={"provider_config_id": "not-a-uuid", "model_name": "gpt-5"},
    )
    assert refused.status_code == 400


def test_a_model_selection_without_a_model_name_is_refused(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user("Empty Tenant", "model-empty@example.com", "StrongPass!1234", ("admin",))
    headers = _headers(seeded)
    conversation_id = _conversation(seeded, "Model conversation")
    provider_config_id = _provider(
        seeded,
        display_name="OpenAI",
        model_name="gpt-5",
    )

    refused = client.put(
        f"/api/v1/deepspace/chats/{conversation_id}/model",
        headers=headers,
        json={"provider_config_id": provider_config_id, "model_name": "  "},
    )
    assert refused.status_code == 400


def test_another_tenants_models_are_not_listed(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user("Own Tenant", "model-own@example.com", "StrongPass!1234", ("admin",))
    other = seed_user("Hidden Tenant", "model-hidden@example.com", "StrongPass!1234", ("admin",))
    headers = _headers(seeded)
    _provider(other, display_name="Hidden OpenAI", model_name="hidden-model")

    listed = client.get("/api/v1/deepspace/chats/models", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["models"] == []


def test_a_recorded_model_of_another_provider_is_honoured(
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    seeded = seed_user("Cross Tenant", "model-cross@example.com", "StrongPass!1234", ("admin",))
    conversation_id = _conversation(seeded, "Cross provider conversation")
    assigned_provider = _provider(
        seeded,
        display_name="Assigned OpenAI",
        model_name="gpt-5",
    )
    chosen_provider = _provider(
        seeded,
        display_name="Chosen OpenAI",
        model_name="gpt-5-mini",
    )
    session = get_session_factory()()
    try:
        ModelSelectionService(session).set_selection(
            tenant_id=seeded.tenant_id,
            user_id=seeded.user_id,
            conversation_id=uuid.UUID(conversation_id),
            provider_config_id=uuid.UUID(chosen_provider),
            model_name="gpt-5-mini",
        )
        assigned = ProviderSelectionService(session, get_settings()).resolve_chat(
            tenant_id=seeded.tenant_id,
            actor_user_id=seeded.user_id,
            record_audit=False,
        )
        resolution = ModelSelectionService(session).resolve_conversation_candidate(
            tenant_id=seeded.tenant_id,
            user_id=seeded.user_id,
            conversation_id=uuid.UUID(conversation_id),
            settings=get_settings(),
            candidate=assigned.candidates[0] if assigned.candidates else None,
        )
    finally:
        session.rollback()
        session.close()

    assert resolution.mode == "manual"
    assert resolution.candidate is not None
    assert str(resolution.candidate.provider_config_id) == chosen_provider
    assert resolution.candidate.model_name == "gpt-5-mini"
    assert resolution.candidate.base_url == "https://api.openai.com/v1"
    assert str(assigned.candidates[0].provider_config_id) == assigned_provider
