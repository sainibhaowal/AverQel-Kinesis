"""Tests for the model a DeepSpace conversation runs on.

Two things decide the model: the conversation's recorded choice, and the
rule that lets DeepSpace pick the model that fits the work.  The tests
cover the rule, the recorded choice, and the provider layer's refusal to
run a conversation on a provider of another tenant.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest

from app.core.config import Settings
from app.deepspace.services.model_selection import (
    ConversationModel,
    ModelRoute,
    ModelSelectionService,
    choose_model,
    select_conversation_model,
)
from app.platform.database.session import Session
from app.providers.models.provider_config import ProviderConfig
from app.providers.services.selection_service import ProviderSelectionService

pytestmark = pytest.mark.unit_no_db


def _route(
    model_name: str,
    *,
    provider_config_id: uuid.UUID | None = None,
    context_window: int | None = 128_000,
    supports_images: bool = False,
    supports_tools: bool = True,
    priority: int = 100,
) -> ModelRoute:
    """Build one model a conversation may run on.

    Args:
        model_name: The model's name.
        provider_config_id: The provider the model belongs to.
        context_window: The model's context window in tokens.
        supports_images: Whether the model accepts images.
        supports_tools: Whether the model supports tool calls.
        priority: The provider's priority.

    Returns:
        The route.
    """
    return ModelRoute(
        provider_config_id=provider_config_id or uuid.uuid4(),
        model_name=model_name,
        provider_type="openai",
        provider_name="OpenAI",
        context_window=context_window,
        supports_images=supports_images,
        supports_tools=supports_tools,
        priority=priority,
    )


class _FakeConfigs:
    """The provider configs one test declares."""

    def __init__(self, configs: dict[uuid.UUID, ProviderConfig]) -> None:
        """Create the fake.

        Args:
            configs: The provider configs, keyed by id.
        """
        self._configs = configs

    def get_by_id(
        self, *, tenant_id: uuid.UUID, provider_config_id: uuid.UUID
    ) -> ProviderConfig | None:
        """Return the provider config the tenant owns.

        Args:
            tenant_id: The owning tenant.
            provider_config_id: The provider config to read.

        Returns:
            The provider config, or None.
        """
        config = self._configs.get(provider_config_id)
        if config is None or config.tenant_id != tenant_id:
            return None
        return config

    def get_accessible_by_id(
        self,
        *,
        tenant_id: uuid.UUID,
        provider_config_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> ProviderConfig | None:
        """Return the provider config the user may use.

        Args:
            tenant_id: The owning tenant.
            provider_config_id: The provider config to read.
            owner_user_id: The user the request is made for.

        Returns:
            The provider config, or None.
        """
        config = self.get_by_id(
            tenant_id=tenant_id,
            provider_config_id=provider_config_id,
        )
        if config is None:
            return None
        if config.owner_user_id == owner_user_id or config.visibility_scope == "system":
            return config
        return None

    def list_by_workspace(
        self,
        *,
        tenant_id: uuid.UUID,
        workspace_id: uuid.UUID | None,
        owner_user_id: uuid.UUID | None = None,
    ) -> list[ProviderConfig]:
        """Return the tenant's providers that belong to no workspace.

        Args:
            tenant_id: The owning tenant.
            workspace_id: The workspace to read.
            owner_user_id: The user the request is made for.

        Returns:
            The provider configs.
        """
        return [
            config
            for config in self._configs.values()
            if config.tenant_id == tenant_id and config.workspace_id == workspace_id
        ]


class _FakeAssignments:
    """A deployment with no provider assignment."""

    def get_active_assignment(
        self,
        *,
        tenant_id: uuid.UUID,
        workspace_id: uuid.UUID | None,
        feature_scope: str,
        owner_user_id: uuid.UUID | None,
    ) -> None:
        """Return no assignment.

        Args:
            tenant_id: The owning tenant.
            workspace_id: The workspace to read.
            feature_scope: The feature the assignment serves.
            owner_user_id: The user the request is made for.

        Returns:
            None.
        """
        return None


class _FakeHealthChecks:
    """A provider with no recorded health check."""

    def get_latest_check(self, *, tenant_id: uuid.UUID, provider_config_id: uuid.UUID) -> None:
        """Return no health check.

        Args:
            tenant_id: The owning tenant.
            provider_config_id: The provider config to read.

        Returns:
            None.
        """
        return None


class _FakeModelCache:
    """A provider that published no model list."""

    def get_model(
        self,
        *,
        tenant_id: uuid.UUID,
        provider_config_id: uuid.UUID,
        model_name: str,
        model_kind: str,
    ) -> None:
        """Return no cached model.

        Args:
            tenant_id: The owning tenant.
            provider_config_id: The provider config to read.
            model_name: The model's name.
            model_kind: The kind of model.

        Returns:
            None.
        """
        return None

    def list_models(
        self,
        *,
        tenant_id: uuid.UUID,
        provider_config_id: uuid.UUID,
        model_kind: str | None = None,
    ) -> list[Any]:
        """Return no cached models.

        Args:
            tenant_id: The owning tenant.
            provider_config_id: The provider config to read.
            model_kind: The kind of model.

        Returns:
            An empty list.
        """
        return []


class _FakeSecrets:
    """A provider without a secret."""

    def get_secret_value(
        self, *, tenant_id: uuid.UUID, provider_config_id: uuid.UUID, secret_type: str
    ) -> None:
        """Return no secret.

        Args:
            tenant_id: The owning tenant.
            provider_config_id: The provider config to read.
            secret_type: The kind of secret.

        Returns:
            None.
        """
        return None


class _FakeAudit:
    """An audit log that keeps what it was told."""

    def __init__(self) -> None:
        """Create the fake."""
        self.events: list[dict[str, Any]] = []

    def write_event(self, **fields: Any) -> None:
        """Record one audit event.

        Args:
            fields: The event's fields.
        """
        self.events.append(fields)


def _provider_config(
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID | None = None,
    enabled: bool = True,
    supports_chat: bool = True,
) -> ProviderConfig:
    """Build one provider config for a test.

    Args:
        tenant_id: The owning tenant.
        owner_user_id: The user who owns the provider.
        enabled: Whether the provider is enabled.
        supports_chat: Whether the provider serves chat.

    Returns:
        The provider config.
    """
    return ProviderConfig(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        workspace_id=None,
        owner_user_id=owner_user_id,
        visibility_scope="user" if owner_user_id is not None else "system",
        provider_type="openai",
        display_name="OpenAI",
        api_base_url="https://api.openai.com/v1",
        auth_mode="none" if owner_user_id is None else "api_key",
        enabled=enabled,
        is_local=False,
        supports_chat=supports_chat,
        supports_embeddings=False,
        supports_reranking=False,
        supports_model_listing=True,
        supports_model_install=False,
        default_chat_model="gpt-5",
        default_embedding_model=None,
        default_reranker_model=None,
        timeout_seconds=30,
        priority=100,
        metadata_json={},
    )


def _selection_service(
    settings: Settings,
    *,
    configs: dict[uuid.UUID, ProviderConfig],
) -> ProviderSelectionService:
    """Build a provider selection service backed by fakes.

    Args:
        settings: The deployment settings.
        configs: The provider configs, keyed by id.

    Returns:
        The service.
    """
    service = ProviderSelectionService(
        db=cast(Session, object()),
        settings=settings,
    )
    service.configs = cast(Any, _FakeConfigs(configs))
    service.assignments = cast(Any, _FakeAssignments())
    service.health_checks = cast(Any, _FakeHealthChecks())
    service.model_cache = cast(Any, _FakeModelCache())
    service.secrets = cast(Any, _FakeSecrets())
    service.audit = cast(Any, _FakeAudit())
    return service


def test_a_text_only_turn_runs_on_the_assigned_model() -> None:
    assigned = _route("gpt-5", context_window=400_000)
    other = _route("gpt-4o-mini", context_window=8_000, priority=200)

    choice = choose_model([other, assigned], preferred=assigned)

    assert choice is not None
    assert choice.route.model_name == "gpt-5"
    assert "the assigned model" in choice.reason


def test_a_turn_with_an_image_runs_on_a_model_that_accepts_images() -> None:
    assigned = _route("gpt-5-text", context_window=400_000)
    vision = _route("gpt-5-vision", context_window=400_000, supports_images=True)

    choice = choose_model([assigned, vision], requires_images=True, preferred=assigned)

    assert choice is not None
    assert choice.route.model_name == "gpt-5-vision"
    assert "accepts images" in choice.reason


def test_a_turn_that_calls_tools_runs_on_a_model_that_supports_tools() -> None:
    assigned = _route("gpt-5-plain", context_window=400_000, supports_tools=False)
    with_tools = _route("gpt-5-tools", context_window=400_000)

    choice = choose_model([assigned, with_tools], requires_tools=True, preferred=assigned)

    assert choice is not None
    assert choice.route.model_name == "gpt-5-tools"
    assert "supports tools" in choice.reason


def test_a_conversation_too_large_for_the_small_model_runs_on_the_large_model() -> None:
    small = _route("small-model", context_window=8_000)
    large = _route("large-model", context_window=1_000_000)

    choice = choose_model([small, large], history_tokens=200_000, preferred=small)

    assert choice is not None
    assert choice.route.model_name == "large-model"
    assert "fits 200000 tokens" in choice.reason


def test_a_model_is_chosen_when_no_model_qualifies() -> None:
    only = _route("text-only", context_window=4_000, supports_images=False)

    choice = choose_model([only], requires_images=True, history_tokens=500_000)

    assert choice is not None
    assert choice.route.model_name == "text-only"
    assert "no model accepts images" in choice.reason
    assert "no model fits 500000 tokens" in choice.reason


def test_the_manual_selection_is_honoured() -> None:
    recorded = ConversationModel(
        provider_config_id=uuid.uuid4(),
        model_name="chosen-model",
    )
    assigned = ConversationModel(
        provider_config_id=uuid.uuid4(),
        model_name="assigned-model",
    )

    decision = select_conversation_model(
        mode="manual",
        recorded=recorded,
        assigned=assigned,
        routes=[_route("assigned-model"), _route("chosen-model")],
        requires_tools=True,
    )

    assert decision.model == recorded
    assert decision.source == "manual"


def test_deepspace_chooses_the_model_in_auto_mode() -> None:
    assigned = ConversationModel(
        provider_config_id=uuid.uuid4(),
        model_name="assigned-model",
    )
    routes = [_route("assigned-model"), _route("vision-model", supports_images=True)]

    decision = select_conversation_model(
        mode="auto",
        recorded=None,
        assigned=assigned,
        routes=routes,
        requires_images=True,
    )

    assert decision.model == ConversationModel(
        provider_config_id=routes[1].provider_config_id,
        model_name="vision-model",
    )
    assert decision.source == "auto"


def test_the_deployment_decides_the_default_mode(settings: Settings) -> None:
    auto = settings.model_copy(
        update={
            "deepspace_model_selection_mode": "auto",
            "deepspace_model_auto_choice_enabled": True,
        }
    )
    manual = settings.model_copy(
        update={
            "deepspace_model_selection_mode": "manual",
            "deepspace_model_auto_choice_enabled": True,
        }
    )
    disabled = settings.model_copy(
        update={
            "deepspace_model_selection_mode": "auto",
            "deepspace_model_auto_choice_enabled": False,
        }
    )

    assert ModelSelectionService.deployment_mode(auto) == "auto"
    assert ModelSelectionService.deployment_mode(manual) == "manual"
    assert ModelSelectionService.deployment_mode(disabled) == "manual"


def test_the_provider_override_refuses_a_provider_of_another_tenant(settings: Settings) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    other_tenant = uuid.uuid4()
    other_provider = _provider_config(tenant_id=other_tenant, owner_user_id=uuid.uuid4())
    service = _selection_service(settings, configs={other_provider.id: other_provider})

    result = service.resolve_chat(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        provider_config_id=other_provider.id,
        model_name="gpt-5",
    )

    assert result.candidates == []
    assert any("inaccessible-config" in note for note in result.selection_notes)


def test_the_provider_override_runs_on_the_named_provider(settings: Settings) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    provider = _provider_config(tenant_id=tenant_id, owner_user_id=user_id)
    service = _selection_service(settings, configs={provider.id: provider})

    result = service.resolve_chat(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        provider_config_id=provider.id,
        model_name="gpt-5-mini",
    )

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.provider_config_id == provider.id
    assert candidate.model_name == "gpt-5-mini"
    assert candidate.base_url == "https://api.openai.com/v1"
    assert candidate.auth_mode == "api_key"
    assert candidate.feature_scope == "chat"


def test_the_provider_override_refuses_a_disabled_provider(settings: Settings) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    provider = _provider_config(tenant_id=tenant_id, owner_user_id=user_id, enabled=False)
    service = _selection_service(settings, configs={provider.id: provider})

    result = service.resolve_chat(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        provider_config_id=provider.id,
        model_name="gpt-5",
    )

    assert result.candidates == []
    assert any("disabled-config" in note for note in result.selection_notes)


def test_a_named_model_without_a_provider_runs_on_the_assigned_provider(
    settings: Settings,
) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    provider = _provider_config(tenant_id=tenant_id, owner_user_id=user_id)
    service = _selection_service(settings, configs={provider.id: provider})

    result = service.resolve_chat(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        model_name="gpt-5-mini",
    )

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.provider_config_id == provider.id
    assert candidate.model_name == "gpt-5-mini"
    assert candidate.base_url == "https://api.openai.com/v1"
