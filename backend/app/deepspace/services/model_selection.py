"""The model one DeepSpace conversation runs on.

A conversation can name the model it runs on, so two conversations in the
same workspace can use different models. The choice is recorded in the
typed session log as a ``model/selection`` event, so it survives a restart
by replay and cannot be changed out of band.

A conversation may name a model of any provider the deployment offers the
tenant, not only the model of the provider the workspace is assigned to.
The deployment decides whether DeepSpace may choose the model itself; the
conversation's recorded choice decides the model inside that mode.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services.session_events import MODEL_SELECTION, SessionEventStore
from app.providers.models.provider_config import ProviderConfig
from app.providers.repositories.provider_configs import ProviderConfigsRepository
from app.providers.repositories.provider_model_cache import ProviderModelCacheRepository
from app.providers.services.context_window import resolve_verified_context_window
from app.providers.services.selection_service import ProviderSelectionService
from app.providers.services.types import ProviderSelectionCandidate

#: The two ways a conversation's model is chosen.
ModelSelectionMode = Literal["auto", "manual"]

#: Where a chosen model came from.
ModelChoiceSource = Literal["manual", "auto", "assigned"]


class ModelSelectionError(ValueError):
    """A model selection was rejected."""


@dataclass(frozen=True, slots=True)
class ConversationModel:
    """The model one conversation runs on.

    Attributes:
        provider_config_id: The provider the model belongs to.
        model_name: The model's name.
    """

    provider_config_id: uuid.UUID
    model_name: str

    def to_dict(self) -> dict[str, str]:
        """Return the selection as a JSON-serializable mapping.

        Returns:
            The selection.
        """
        return {
            "provider_config_id": str(self.provider_config_id),
            "model_name": self.model_name,
        }


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """One model a conversation may run on.

    Attributes:
        provider_config_id: The provider the model belongs to.
        model_name: The model's name.
        provider_type: The provider's type, such as "openai".
        provider_name: The provider's display name.
        context_window: The model's context window in tokens, when known.
        supports_images: Whether the model accepts images.
        supports_tools: Whether the model accepts tool calls.
        priority: The provider's priority. A higher priority wins a tie.
    """

    provider_config_id: uuid.UUID
    model_name: str
    provider_type: str = ""
    provider_name: str = ""
    context_window: int | None = None
    supports_images: bool = False
    supports_tools: bool = True
    priority: int = 100

    def to_dict(self) -> dict[str, Any]:
        """Return the route as a JSON-serializable mapping.

        Returns:
            The route.
        """
        return {
            "provider_config_id": str(self.provider_config_id),
            "provider_type": self.provider_type,
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "context_window": self.context_window,
            "supports_images": self.supports_images,
            "supports_tools": self.supports_tools,
        }


@dataclass(frozen=True, slots=True)
class AutoModelChoice:
    """The model DeepSpace picked for a turn, and why.

    Attributes:
        route: The chosen model.
        reason: Why the model was chosen.
    """

    route: ModelRoute
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return the choice as a JSON-serializable mapping.

        Returns:
            The choice.
        """
        return {"model": self.route.to_dict(), "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ModelDecision:
    """The model one conversation runs on, and why.

    Attributes:
        model: The model the conversation runs on.
        source: Where the model came from.
        reason: Why the model was chosen.
    """

    model: ConversationModel | None
    source: ModelChoiceSource
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return the decision as a JSON-serializable mapping.

        Returns:
            The decision.
        """
        return {
            "model": self.model.to_dict() if self.model else None,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ConversationModelResolution:
    """The model one conversation runs on, and the decision behind it.

    Attributes:
        candidate: The candidate the turn runs with.
        decision: The decision behind the candidate.
        mode: The mode the conversation runs in.
    """

    candidate: ProviderSelectionCandidate | None
    decision: ModelDecision
    mode: ModelSelectionMode

    def to_dict(self) -> dict[str, Any]:
        """Return the resolution as a JSON-serializable mapping.

        Returns:
            The resolution.
        """
        return {
            "candidate": self.candidate.model_name if self.candidate else None,
            "decision": self.decision.to_dict(),
            "mode": self.mode,
        }


def _route_order(route: ModelRoute) -> tuple[int, int, str, str]:
    """Order routes so the same input always yields the same choice.

    Args:
        route: The route to order.

    Returns:
        The sort key.
    """
    return (
        -route.priority,
        -(route.context_window or 0),
        str(route.provider_config_id),
        route.model_name,
    )


def _same_route(left: ModelRoute, right: ModelRoute) -> bool:
    """Report whether two routes name the same model.

    Args:
        left: The first route.
        right: The second route.

    Returns:
        True when both routes name the same provider and model.
    """
    return (
        left.provider_config_id == right.provider_config_id and left.model_name == right.model_name
    )


def choose_model(
    routes: Sequence[ModelRoute],
    *,
    history_tokens: int = 0,
    requires_tools: bool = False,
    requires_images: bool = False,
    preferred: ModelRoute | None = None,
) -> AutoModelChoice | None:
    """Choose the model that fits one turn.

    The rule is deterministic and explainable:

    1. A turn with an image needs a model that accepts images.
    2. The conversation must fit in the model's context window.
    3. A turn that calls tools needs a model that supports tools.
    4. The workspace's assigned model wins among the models that qualify.

    A requirement that no model meets never empties the field. The models
    that qualify for the earlier requirements stay, and the reason names the
    requirement that could not be met.

    Args:
        routes: The models the deployment can offer.
        history_tokens: The size of the conversation in tokens.
        requires_tools: Whether the turn will call tools.
        requires_images: Whether the turn carries an image.
        preferred: The workspace's assigned model.

    Returns:
        The chosen model and the reason, or None when there is no model.
    """
    if not routes:
        return None
    eligible = sorted(routes, key=_route_order)
    notes: list[str] = []

    if requires_images:
        image_routes = [route for route in eligible if route.supports_images]
        if image_routes:
            eligible = image_routes
            notes.append("accepts images")
        else:
            notes.append("no model accepts images")

    if history_tokens > 0:
        fitting = [
            route
            for route in eligible
            if route.context_window is None or route.context_window >= history_tokens
        ]
        if fitting:
            eligible = fitting
            notes.append(f"fits {history_tokens} tokens")
        else:
            notes.append(f"no model fits {history_tokens} tokens")

    if requires_tools:
        tool_routes = [route for route in eligible if route.supports_tools]
        if tool_routes:
            eligible = tool_routes
            notes.append("supports tools")
        else:
            notes.append("no model supports tools")

    chosen: ModelRoute | None = None
    if preferred is not None:
        chosen = next((route for route in eligible if _same_route(route, preferred)), None)
        if chosen is not None:
            notes.append("the assigned model")
    if chosen is None:
        chosen = eligible[0]
        notes.append("the highest-priority model")

    return AutoModelChoice(route=chosen, reason="; ".join(notes))


def select_conversation_model(
    *,
    mode: ModelSelectionMode,
    recorded: ConversationModel | None,
    assigned: ConversationModel | None,
    routes: Sequence[ModelRoute] = (),
    history_tokens: int = 0,
    requires_tools: bool = False,
    requires_images: bool = False,
) -> ModelDecision:
    """Decide which model one conversation runs on.

    A recorded manual choice wins. In auto mode DeepSpace picks the model
    that fits the turn, and the deployment's assigned model is the fallback
    when the deployment offers no model to choose from.

    Args:
        mode: The mode the conversation runs in.
        recorded: The conversation's recorded model, when it has one.
        assigned: The model the deployment assigned to the conversation.
        routes: The models the deployment can offer.
        history_tokens: The size of the conversation in tokens.
        requires_tools: Whether the turn will call tools.
        requires_images: Whether the turn carries an image.

    Returns:
        The decision.
    """
    if mode == "manual" and recorded is not None:
        return ModelDecision(
            model=recorded,
            source="manual",
            reason="the conversation names this model",
        )

    preferred: ModelRoute | None = None
    if assigned is not None:
        preferred = next(
            (
                route
                for route in routes
                if route.provider_config_id == assigned.provider_config_id
                and route.model_name == assigned.model_name
            ),
            None,
        )

    if mode == "auto":
        choice = choose_model(
            routes,
            history_tokens=history_tokens,
            requires_tools=requires_tools,
            requires_images=requires_images,
            preferred=preferred,
        )
        if choice is not None:
            return ModelDecision(
                model=ConversationModel(
                    provider_config_id=choice.route.provider_config_id,
                    model_name=choice.route.model_name,
                ),
                source="auto",
                reason=choice.reason,
            )

    return ModelDecision(
        model=assigned,
        source="assigned",
        reason="the deployment's assigned model",
    )


def _model_capabilities(capabilities: dict[str, Any], *, model_kind: str) -> tuple[bool, bool]:
    """Read whether a model accepts images and whether it supports tools.

    Args:
        capabilities: The capabilities the provider advertised.
        model_kind: The kind the provider published the model under.

    Returns:
        Whether the model accepts images, and whether it supports tools.
    """
    supports_images = capabilities.get("supports_image_in") is True
    modalities = capabilities.get("input_modalities")
    if isinstance(modalities, list):
        supports_images = supports_images or any(
            str(entry).strip().lower() in {"image", "images", "vision"} for entry in modalities
        )
    if model_kind == "vision":
        supports_images = True
    supports_tools = True
    for key in ("supports_tools", "supports_tool_use", "tool_use"):
        value = capabilities.get(key)
        if isinstance(value, bool):
            supports_tools = value
            break
    return supports_images, supports_tools


class ModelSelectionService:
    """The model selection of one conversation."""

    def __init__(self, db: Session) -> None:
        """Create the service.

        Args:
            db: The database session.
        """
        self.db = db
        self.events = SessionEventStore(db)
        self.configs = ProviderConfigsRepository(db)
        self.model_cache = ProviderModelCacheRepository(db)

    @staticmethod
    def deployment_mode(settings: Settings) -> ModelSelectionMode:
        """Return the mode the deployment runs conversations in by default.

        Args:
            settings: The deployment settings.

        Returns:
            The deployment's default mode.
        """
        if not settings.deepspace_model_auto_choice_enabled:
            return "manual"
        return "auto" if settings.deepspace_model_selection_mode == "auto" else "manual"

    def recorded_selection(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> ConversationModel | None:
        """Read the conversation's last recorded model selection.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The recorded selection, or None when the conversation never
            recorded one.
        """
        payload = self._last_payload(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if payload is None:
            return None
        provider_config_id = str(payload.get("provider_config_id") or "")
        model_name = str(payload.get("model_name") or "")
        if not provider_config_id or not model_name:
            return None
        try:
            parsed_provider = uuid.UUID(provider_config_id)
        except ValueError:
            return None
        return ConversationModel(
            provider_config_id=parsed_provider,
            model_name=model_name,
        )

    def recorded_mode(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> ModelSelectionMode | None:
        """Read the mode the conversation last recorded.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The recorded mode, or None when the conversation never recorded
            one.
        """
        payload = self._last_payload(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if payload is None:
            return None
        mode = str(payload.get("mode") or "").strip().lower()
        if mode in {"auto", "manual"}:
            return "auto" if mode == "auto" else "manual"
        if payload.get("provider_config_id") and payload.get("model_name"):
            return "manual"
        return None

    def effective_mode(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        settings: Settings,
    ) -> ModelSelectionMode:
        """Return the mode the conversation runs in.

        The conversation's recorded mode wins over the deployment's default,
        except that a deployment which does not allow DeepSpace to choose
        the model always runs conversations in manual mode.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.
            settings: The deployment settings.

        Returns:
            The mode the conversation runs in.
        """
        recorded = self.recorded_mode(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if recorded == "auto" and not settings.deepspace_model_auto_choice_enabled:
            return "manual"
        if recorded is not None:
            return recorded
        return self.deployment_mode(settings)

    def set_selection(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        provider_config_id: uuid.UUID,
        model_name: str,
    ) -> dict[str, Any]:
        """Record the conversation's model selection.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to change.
            provider_config_id: The provider the model belongs to.
            model_name: The model's name.

        Returns:
            The recorded state.
        """
        name = str(model_name or "").strip()
        if not name:
            raise ModelSelectionError("A model name is required.")
        if len(name) > 200:
            raise ModelSelectionError("A model name may be at most 200 characters.")
        selection = ConversationModel(
            provider_config_id=provider_config_id,
            model_name=name,
        )
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=MODEL_SELECTION,
            payload={"mode": "manual", **selection.to_dict()},
        )
        self.db.commit()
        return selection.to_dict()

    def set_auto_mode(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Let DeepSpace choose the conversation's model.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to change.

        Returns:
            The recorded state.
        """
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=MODEL_SELECTION,
            payload={"mode": "auto"},
        )
        self.db.commit()
        return {"mode": "auto"}

    def usable_provider(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        provider_config_id: uuid.UUID,
    ) -> ProviderConfig | None:
        """Return the provider when the caller may run a conversation on it.

        A provider of another tenant, a disabled provider, and a provider
        that does not serve chat are all refused.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            provider_config_id: The provider to check.

        Returns:
            The provider, or None when the caller may not use it.
        """
        config = self.configs.get_accessible_by_id(
            tenant_id=tenant_id,
            provider_config_id=provider_config_id,
            owner_user_id=user_id,
        )
        if config is None or not config.enabled or not config.supports_chat:
            return None
        return config

    def resolve_conversation_candidate(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        settings: Settings,
        candidate: ProviderSelectionCandidate | None,
        history_tokens: int = 0,
        requires_tools: bool = False,
        requires_images: bool = False,
    ) -> ConversationModelResolution:
        """Resolve the candidate one conversation runs on.

        The conversation's recorded model is honoured even when it belongs to
        another provider of the tenant.  In auto mode DeepSpace picks the
        model that fits the turn.  A provider the caller may not use is
        refused, and the deployment's candidate stands.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.
            settings: The deployment settings.
            candidate: The candidate the deployment resolved.
            history_tokens: The size of the conversation in tokens.
            requires_tools: Whether the turn will call tools.
            requires_images: Whether the turn carries an image.

        Returns:
            The candidate the turn runs with and the decision behind it.
        """
        recorded = self.recorded_selection(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        mode = self.effective_mode(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            settings=settings,
        )
        assigned: ConversationModel | None = None
        if candidate is not None and candidate.provider_config_id is not None:
            assigned = ConversationModel(
                provider_config_id=candidate.provider_config_id,
                model_name=candidate.model_name,
            )
        decision = select_conversation_model(
            mode=mode,
            recorded=recorded,
            assigned=assigned,
            routes=self.routes(tenant_id=tenant_id, user_id=user_id),
            history_tokens=history_tokens,
            requires_tools=requires_tools,
            requires_images=requires_images,
        )
        chosen = decision.model
        if chosen is None:
            return ConversationModelResolution(
                candidate=candidate,
                decision=decision,
                mode=mode,
            )
        if (
            candidate is not None
            and candidate.provider_config_id == chosen.provider_config_id
            and candidate.model_name == chosen.model_name
        ):
            return ConversationModelResolution(
                candidate=candidate,
                decision=decision,
                mode=mode,
            )
        named = ProviderSelectionService(self.db, settings).resolve_chat(
            tenant_id=tenant_id,
            workspace_id=None,
            actor_user_id=user_id,
            provider_config_id=chosen.provider_config_id,
            model_name=chosen.model_name,
        )
        if not named.candidates:
            # The provider no longer offers the model the conversation named,
            # so the deployment's choice stands rather than the turn failing
            # on a model that is gone.
            return ConversationModelResolution(
                candidate=candidate,
                decision=decision,
                mode=mode,
            )
        return ConversationModelResolution(
            candidate=named.candidates[0],
            decision=decision,
            mode=mode,
        )

    def selectable_models(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """List every model the caller's account may run a conversation on.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.

        Returns:
            The models, each with the provider it belongs to.
        """
        return [route.to_dict() for route in self.routes(tenant_id=tenant_id, user_id=user_id)]

    def routes(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> list[ModelRoute]:
        """List the models the caller's account may run a conversation on.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.

        Returns:
            The routes, ordered by provider priority and then by model name.
        """
        routes: list[ModelRoute] = []
        for provider in self._chat_providers(tenant_id=tenant_id, user_id=user_id):
            routes.extend(self._provider_routes(tenant_id=tenant_id, provider=provider))
        return sorted(routes, key=_route_order)

    def _chat_providers(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> list[ProviderConfig]:
        """List the providers the caller's account may chat with.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.

        Returns:
            The enabled providers that serve chat.
        """
        providers = self.configs.list_by_workspace(
            tenant_id=tenant_id,
            workspace_id=None,
            owner_user_id=user_id,
        )
        return [provider for provider in providers if provider.enabled and provider.supports_chat]

    def _provider_routes(
        self, *, tenant_id: uuid.UUID, provider: ProviderConfig
    ) -> list[ModelRoute]:
        """List the chat models one provider offers.

        Args:
            tenant_id: The owning tenant.
            provider: The provider to read.

        Returns:
            The provider's chat models.
        """
        cached = self.model_cache.list_models(
            tenant_id=tenant_id,
            provider_config_id=provider.id,
            model_kind="chat",
        )
        facts: dict[str, tuple[int | None, bool, bool]] = {}
        names: list[str] = []
        for row in cached:
            if not row.is_available:
                continue
            supports_images, supports_tools = _model_capabilities(
                dict(row.capabilities_json or {}),
                model_kind=row.model_kind,
            )
            context_window = row.context_window
            if not isinstance(context_window, int) or context_window <= 0:
                verified = resolve_verified_context_window(
                    row.model_name,
                    provider_type=provider.provider_type,
                )
                context_window = verified.context_window
            facts[row.model_name] = (context_window, supports_images, supports_tools)
            names.append(row.model_name)

        default_model = str(provider.default_chat_model or "").strip()
        if default_model and default_model not in facts:
            verified = resolve_verified_context_window(
                default_model,
                provider_type=provider.provider_type,
            )
            facts[default_model] = (verified.context_window, False, True)
            names.append(default_model)

        return [
            ModelRoute(
                provider_config_id=provider.id,
                model_name=model_name,
                provider_type=provider.provider_type,
                provider_name=provider.display_name,
                context_window=facts[model_name][0],
                supports_images=facts[model_name][1],
                supports_tools=facts[model_name][2],
                priority=provider.priority,
            )
            for model_name in names
        ]

    def _last_payload(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """Read the payload of the conversation's last model selection event.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The payload, or None when the conversation never recorded one.
        """
        events = (
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.tenant_id == tenant_id,
                    DeepSpaceSessionEvent.user_id == user_id,
                    DeepSpaceSessionEvent.conversation_id == conversation_id,
                    DeepSpaceSessionEvent.event_type == MODEL_SELECTION,
                )
                .order_by(DeepSpaceSessionEvent.seq.desc())
                .limit(1)
            )
            .scalars()
            .all()
        )
        if not events:
            return None
        payload: object = events[0].payload
        if not isinstance(payload, dict):
            return None
        return payload


__all__ = [
    "AutoModelChoice",
    "ConversationModel",
    "ConversationModelResolution",
    "ModelDecision",
    "ModelRoute",
    "ModelSelectionError",
    "ModelSelectionMode",
    "ModelSelectionService",
    "choose_model",
    "select_conversation_model",
]
