"""Session title generation for DeepSpace.

Ported from NeoSIS @averqel/neosis-session-title, @averqel/neosis-session-title-llm, etc.
Provides LLM-generated, auto-updating session titles.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.deepspace.services.provider_circuit import DeepSpaceProviderCircuit
from app.providers.services.registry import ProviderRegistry
from app.providers.services.types import ChatGenerateRequest

logger = logging.getLogger(__name__)


async def _generate_title_with_llm(
    prompt: str,
    *,
    fallback: str | None,
    max_tokens: int,
    settings: Settings,
    provider_registry: ProviderRegistry,
    circuit: DeepSpaceProviderCircuit,
) -> str | None:
    """Generate a short title with the configured chat provider."""
    model_name = settings.deepspace_session_title_model or settings.llm_model
    try:
        if circuit.is_open(
            tenant_id=UUID("00000000-0000-0000-0000-000000000000"),
            provider_config_id=None,
            model_name=model_name,
        ):
            return fallback

        provider = provider_registry.get_chat_provider()
        result = await asyncio.to_thread(
            provider.generate,
            ChatGenerateRequest(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=max_tokens,
                base_url=settings.llm_api_base_url or "",
                api_key=settings.llm_api_key,
                stream=False,
            ),
        )
        title = result.content.strip()
        return title[:80] if title else fallback
    except Exception as exc:
        logger.warning("LLM title generation failed: %s", exc)
        return fallback


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

        return await _generate_title_with_llm(
            prompt,
            fallback=None,
            max_tokens=50,
            settings=settings,
            provider_registry=provider_registry,
            circuit=circuit,
        )


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

        return await _generate_title_with_llm(
            prompt,
            fallback=first_prompt[:80],
            max_tokens=30,
            settings=settings,
            provider_registry=provider_registry,
            circuit=circuit,
        )


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
