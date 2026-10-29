"""Session title generation for DeepSpace.

Ported from NeoSIS @averqel/neosis-session-title, @averqel/neosis-session-title-llm, etc.
Provides LLM-generated, auto-updating session titles.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.deepspace.services.provider_circuit import DeepSpaceProviderCircuit
from app.providers.services.registry import ProviderRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionTitleProvider(ABC):
    """Abstract base class for session title providers."""

    name: str = ""

    @abstractmethod
    async def generate(
        self,
        conversation_id: UUID,
        messages: list[dict[str, Any]],
        settings: Settings,
        provider_registry: ProviderRegistry,
        circuit: DeepSpaceProviderCircuit,
    ) -> str | None:
        """Generate a title for the conversation.

        Args:
            conversation_id: The conversation ID.
            messages: The conversation messages (user/assistant).
            settings: Deployment settings.
            provider_registry: Provider registry for LLM calls.
            circuit: Provider circuit breaker.

        Returns:
            Generated title, or None if generation failed.
        """
        ...


class FirstPromptTitleProvider(SessionTitleProvider):
    """Title provider that uses the first user prompt as the title."""

    name = "first-prompt"

    async def generate(
        self,
        conversation_id: UUID,
        messages: list[dict[str, Any]],
        settings: Settings,
        provider_registry: ProviderRegistry,
        circuit: DeepSpaceProviderCircuit,
    ) -> str | None:
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    return content.strip()[:80]
        return None


class AllPromptsLLMTitleProvider(SessionTitleProvider):
    """Title provider that uses an LLM to summarize all user prompts."""

    name = "all-prompts-llm"

    async def generate(
        self,
        conversation_id: UUID,
        messages: list[dict[str, Any]],
        settings: Settings,
        provider_registry: ProviderRegistry,
        circuit: DeepSpaceProviderCircuit,
    ) -> str | None:
        # Collect all user messages
        user_messages = [
            msg.get("content", "")
            for msg in messages
            if msg.get("role") == "user" and isinstance(msg.get("content"), str)
        ]

        if not user_messages:
            return None

        # Build summarization prompt
        prompt = (
            "Summarize this conversation in 8 words or fewer. "
            "Return only the title, no quotes or punctuation:\n\n"
            + "\n".join(f"- {m[:200]}" for m in user_messages[:10])
        )

        try:
            provider_name = getattr(settings, "deepspace_session_title_model", None) or "default"
            provider = provider_registry.get_provider(provider_name)
            if not provider:
                logger.warning("No provider configured for session title generation")
                return None

            # Check circuit
            if circuit.is_open(
                tenant_id=UUID("00000000-0000-0000-0000-000000000000"),
                provider_config_id=None,
                model_name=provider_name,
            ):
                logger.warning("Provider circuit open for session title generation")
                return None

            # Generate title
            result = await provider.generate(
                prompt=prompt,
                max_tokens=50,
                temperature=0.3,
            )

            title = result.get("text", "").strip()
            if title:
                return title[:80]

        except Exception as e:
            logger.warning("LLM title generation failed: %s", e)

        return None


class FirstPromptLLMTitleProvider(SessionTitleProvider):
    """Title provider that uses an LLM to refine the first prompt into a title."""

    name = "first-prompt-llm"

    async def generate(
        self,
        conversation_id: UUID,
        messages: list[dict[str, Any]],
        settings: Settings,
        provider_registry: ProviderRegistry,
        circuit: DeepSpaceProviderCircuit,
    ) -> str | None:
        first_prompt = None
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    first_prompt = content.strip()
                    break

        if not first_prompt:
            return None

        # If first prompt is already short, use it
        if len(first_prompt) <= 80:
            return first_prompt[:80]

        # Otherwise, ask LLM to summarize
        prompt = (
            "Create a concise title (max 8 words) for a conversation that starts with this prompt:\n\n"
            f"{first_prompt[:500]}"
        )

        try:
            provider_name = getattr(settings, "deepspace_session_title_model", None) or "default"
            provider = provider_registry.get_provider(provider_name)
            if not provider:
                return first_prompt[:80]

            if circuit.is_open(
                tenant_id=UUID("00000000-0000-0000-0000-000000000000"),
                provider_config_id=None,
                model_name=provider_name,
            ):
                return first_prompt[:80]

            result = await provider.generate(
                prompt=prompt,
                max_tokens=30,
                temperature=0.3,
            )

            title = result.get("text", "").strip()
            if title:
                return title[:80]

        except Exception as e:
            logger.warning("LLM title generation failed: %s", e)

        return first_prompt[:80]


class SessionTitleService:
    """Service for managing session titles."""

    def __init__(
        self,
        settings: Settings,
        provider_registry: ProviderRegistry,
        circuit: DeepSpaceProviderCircuit,
    ):
        self.settings = settings
        self.provider_registry = provider_registry
        self.circuit = circuit
        self._providers: dict[str, SessionTitleProvider] = {
            "first-prompt": FirstPromptTitleProvider(),
            "all-prompts-llm": AllPromptsLLMTitleProvider(),
            "first-prompt-llm": FirstPromptLLMTitleProvider(),
        }
        self._active_provider = self._get_active_provider()

    def _get_active_provider(self) -> SessionTitleProvider:
        provider_name = getattr(self.settings, "deepspace_session_title_provider", "first-prompt")
        return self._providers.get(provider_name, self._providers["first-prompt"])

    async def maybe_update_title(
        self,
        conversation_id: UUID,
        messages: list[dict[str, Any]],
        turn_number: int,
    ) -> str | None:
        """Maybe update the conversation title.

        Called after each turn. Generates a new title if:
        - Title generation is enabled
        - The turn number is a multiple of the refresh interval
        - There's no title yet or it's a refresh turn
        """
        if not getattr(self.settings, "deepspace_session_title_enabled", True):
            return None

        refresh_turns = getattr(self.settings, "deepspace_session_title_refresh_turns", 3)
        if turn_number % refresh_turns != 0 and turn_number != 1:
            return None

        provider = self._active_provider
        title = await provider.generate(
            conversation_id,
            messages,
            self.settings,
            self.provider_registry,
            self.circuit,
        )

        if title:
            max_chars = getattr(self.settings, "deepspace_session_title_max_chars", 80)
            return title[:max_chars]

        return None

    def get_provider(self, name: str) -> SessionTitleProvider | None:
        return self._providers.get(name)

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())
