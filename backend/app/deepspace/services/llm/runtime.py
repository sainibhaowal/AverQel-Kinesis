"""LLM Runtime — adapter registry with waterfall-interceptable streaming."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from typing import Any, cast

from app.deepspace.services.llm.adapter import LlmAdapter
from app.deepspace.services.llm.call_config import call_config_equals
from app.deepspace.services.llm.error import (
    DUPLICATE_ADAPTER_CODE,
    DUPLICATE_DIRECTORY_CODE,
    INVALID_ADAPTER_CODE,
    INVALID_CATALOG_CODE,
    INVALID_DIRECTORY_CODE,
    INVALID_DISCOVERY_CODE,
    INVALID_MODEL_CONTEXT_CODE,
    INVALID_MODEL_INFO_CODE,
    INVALID_MODEL_MAX_TOKENS_CODE,
    INVALID_MODEL_REASONING_CODE,
    INVALID_PREPARED_CALL_CODE,
    NO_ADAPTER_CODE,
    NO_DISCOVERY_CODE,
    UNSUPPORTED_REASONING_EFFORT_CODE,
    LlmError,
)
from app.deepspace.services.llm.types import (
    GenerateOptions,
    LlmCallConfig,
    LlmCallConfigAdapterDefaults,
    LlmConfigurableProvider,
    LlmDiscoveredModel,
    LlmModelContext,
    LlmModelDiscoveryRequest,
    LlmProviderInfo,
    LlmResolvedModelInfo,
    PreparedLlmCall,
    ProviderRoute,
    RequestMessage,
    ResolvedRetryPolicy,
    StreamChunk,
    content_has_file,
    content_has_image,
    deep_freeze,
    project_files_to_text,
    project_images_for_text_model,
    project_tool_updates,
    resolve_retry_policy,
    structured_clone,
)


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    adapter: LlmAdapter
    provider: LlmProviderInfo
    retry_policy: ResolvedRetryPolicy


class LlmRuntime:
    """
    The LLM service: an adapter registry plus a streaming model-call API,
    interceptable via the `llm/stream` waterfall.
    """

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._adapters: dict[ProviderRoute, AdapterRegistration] = {}
        self._directory: dict[ProviderRoute, LlmConfigurableProvider] = {}
        self._discoveries: dict[str, Callable[[LlmModelDiscoveryRequest, Any | None], Any]] = {}

    # --- Adapter Registration ---

    def register_adapter(
        self,
        providers: list[ProviderRoute],
        adapter: LlmAdapter,
    ) -> Callable[[], None]:
        """Register an adapter for the given provider routes. Returns disposer."""
        if not providers:
            raise LlmError("an adapter must register at least one provider", INVALID_ADAPTER_CODE)

        owned: set[ProviderRoute] = set()
        released = False

        def dispose() -> None:
            nonlocal released
            if released:
                return
            released = True
            for p in owned:
                self._adapters.pop(p, None)
            owned.clear()
            self._emit_adapters_updated()

        # Validate all routes first
        for provider in providers:
            if provider in self._adapters and provider not in owned:
                raise LlmError(
                    f'an adapter for provider "{provider}" is already registered',
                    DUPLICATE_ADAPTER_CODE,
                )
            info = adapter.provider_info(provider)
            if info.id != provider or not info.name:
                raise LlmError(
                    f'adapter metadata for provider "{provider}" must preserve its id and have a non-empty name',
                    INVALID_ADAPTER_CODE,
                )

        # Commit all
        for provider in providers:
            retry_policy = adapter.provider_retry_policy(provider) or resolve_retry_policy(
                None, f'llm: provider "{provider}" retryPolicy'
            )
            self._adapters[provider] = AdapterRegistration(
                adapter=adapter,
                provider=info,
                retry_policy=retry_policy,
            )
            owned.add(provider)

        self._emit_adapters_updated()
        return dispose

    def _emit_adapters_updated(self) -> None:
        for listener in self.ctx.events.dispatch("emit", ["llm/adapters_updated"]) or []:
            try:
                listener()
            except Exception:
                pass

    @property
    def adapters(self) -> dict[ProviderRoute, AdapterRegistration]:
        return self._adapters

    # --- Directory (Configurable Providers) ---

    def register_configurable_providers(
        self,
        entries: list[LlmConfigurableProvider],
    ) -> Callable[[], None]:
        if not entries:
            raise LlmError(
                "a configurable-provider registration must declare at least one provider",
                INVALID_DIRECTORY_CODE,
            )

        held: list[LlmConfigurableProvider] = []
        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            for entry in held:
                self._directory.pop(entry.provider, None)
            held.clear()
            self._emit_adapters_updated()

        def commit(candidates: list[LlmConfigurableProvider]) -> None:
            for entry in candidates:
                if not entry.provider or not entry.display_name or not entry.settings_ns:
                    raise LlmError(
                        "configurable providers need non-empty provider, displayName, and settingsNs",
                        INVALID_DIRECTORY_CODE,
                    )
                if entry.provider in self._directory and entry.provider not in {
                    e.provider for e in held
                }:
                    raise LlmError(
                        f'configurable provider "{entry.provider}" is already declared',
                        DUPLICATE_DIRECTORY_CODE,
                    )

            for entry in held:
                self._directory.pop(entry.provider, None)
            for entry in candidates:
                self._directory[entry.provider] = entry
            held[:] = candidates
            self._emit_adapters_updated()

        commit(entries)
        return dispose

    def list_configurable_providers(self) -> list[LlmConfigurableProvider]:
        return list(self._directory.values())

    # --- Model Discovery ---

    def register_model_discovery(
        self,
        settings_ns: str,
        discover: Callable[[LlmModelDiscoveryRequest, Any | None], Any],
    ) -> Callable[[], None]:
        if not settings_ns:
            raise LlmError(
                "model discovery needs a non-empty settings namespace", INVALID_DISCOVERY_CODE
            )
        if settings_ns in self._discoveries:
            raise LlmError(
                f'model discovery for "{settings_ns}" is already registered',
                DUPLICATE_DIRECTORY_CODE,
            )

        self._discoveries[settings_ns] = discover

        def dispose() -> None:
            self._discoveries.pop(settings_ns, None)

        return dispose

    async def discover_models(
        self,
        settings_ns: str,
        request: LlmModelDiscoveryRequest,
        signal: Any | None = None,
    ) -> list[LlmDiscoveredModel]:
        discover = self._discoveries.get(settings_ns)
        if discover is None:
            raise LlmError(
                f'no model discovery is registered for "{settings_ns}"', NO_DISCOVERY_CODE
            )
        if not request.provider and not request.base_url:
            raise LlmError(
                "model discovery needs a provider route or a baseURL", INVALID_DISCOVERY_CODE
            )

        discovered = (
            await discover(request, signal)
            if inspect.iscoroutinefunction(discover)
            else discover(request, signal)
        )

        seen: set[str] = set()
        models: list[LlmDiscoveredModel] = []
        for model in discovered:
            if not model.id or model.id in seen:
                continue
            seen.add(model.id)
            models.append(
                LlmDiscoveredModel(
                    id=model.id,
                    name=model.name,
                    context_window=model.context_window,
                    max_tokens=model.max_tokens,
                    input_modalities=model.input_modalities or ("text",),
                )
            )
        return models

    # --- Provider Queries ---

    def list_providers(self) -> list[LlmProviderInfo]:
        return [reg.provider for reg in self._adapters.values()]

    def provider_retry_policy(self, provider: ProviderRoute) -> ResolvedRetryPolicy:
        return self._adapters[provider].retry_policy

    def image_request_pricing(self, provider: ProviderRoute, model: str) -> Any | None:
        reg = self._adapters.get(provider)
        return reg.adapter.image_request_pricing(provider, model) if reg else None

    def image_request_target(
        self, provider: ProviderRoute, model: str, image: dict[str, Any]
    ) -> Any:
        reg = self._adapters[provider]
        return reg.adapter.image_request_target(provider, model, image)

    async def list_models(self, provider: ProviderRoute) -> list[Any]:
        reg = self._adapters[provider]
        models = await reg.adapter.list_models(provider)
        seen: set[str] = set()
        result = []
        for model in models:
            if not model.provider or model.provider != provider or not model.id or model.id in seen:
                raise LlmError(
                    f'adapter returned invalid or duplicate model metadata for provider "{provider}"',
                    INVALID_CATALOG_CODE,
                )
            seen.add(model.id)
            result.append(model)
        return result

    async def resolve_model_info(
        self,
        provider: ProviderRoute,
        model: str,
        signal: Any | None = None,
    ) -> LlmResolvedModelInfo:
        return await self._resolve_model_info_for(self._adapters[provider], model, signal)

    async def _resolve_model_info_for(
        self,
        registration: AdapterRegistration,
        model: str,
        signal: Any | None,
    ) -> LlmResolvedModelInfo:
        resolved = await registration.adapter.resolve_model(registration.provider.id, model, signal)
        return self._normalize_model_info(registration, model, resolved)

    def _normalize_model_info(
        self,
        registration: AdapterRegistration,
        model: str,
        resolved: LlmResolvedModelInfo,
    ) -> LlmResolvedModelInfo:
        provider = registration.provider.id
        if resolved.provider != provider or resolved.id != model or not resolved.name:
            raise LlmError(
                f'adapter returned invalid exact model metadata for provider "{provider}" model "{model}"',
                INVALID_MODEL_INFO_CODE,
            )

        if resolved.context is not None:
            if (
                not isinstance(resolved.context.context_window, int)
                or resolved.context.context_window <= 0
            ):
                raise LlmError(
                    f'adapter returned invalid context metadata for provider "{provider}" model "{model}"',
                    INVALID_MODEL_CONTEXT_CODE,
                )

        input_modalities = tuple(resolved.input_modalities) or ("text",)
        system_prompt_update = resolved.system_prompt_update
        if system_prompt_update and system_prompt_update != "in_history":
            raise LlmError(
                f'adapter returned invalid system prompt update mode for provider "{provider}" model "{model}"',
                INVALID_MODEL_INFO_CODE,
            )
        tool_update = resolved.tool_update
        if tool_update and tool_update not in ("in_history", "addition_only"):
            raise LlmError(
                f'adapter returned invalid tool update mode for provider "{provider}" model "{model}"',
                INVALID_MODEL_INFO_CODE,
            )

        if resolved.default_max_tokens is not None and (
            not isinstance(resolved.default_max_tokens, int) or resolved.default_max_tokens <= 0
        ):
            raise LlmError(
                f'adapter returned invalid default maxTokens for provider "{provider}" model "{model}"',
                INVALID_MODEL_MAX_TOKENS_CODE,
            )

        info = LlmResolvedModelInfo(
            provider=provider,
            id=model,
            name=resolved.name,
            description=resolved.description,
            input_modalities=input_modalities,
            context=resolved.context,
            default_max_tokens=resolved.default_max_tokens,
            system_prompt_update=system_prompt_update,
            tool_update=tool_update,
            reasoning=resolved.reasoning,
        )

        if resolved.reasoning:
            if not resolved.reasoning.efforts:
                raise LlmError(
                    f'adapter returned invalid reasoning metadata for provider "{provider}" model "{model}"',
                    INVALID_MODEL_REASONING_CODE,
                )
            seen_efforts: set[str] = set()
            for effort in resolved.reasoning.efforts:
                if not effort.id or not effort.name or effort.id in seen_efforts:
                    raise LlmError(
                        f'adapter returned invalid or duplicate reasoning effort metadata for provider "{provider}" model "{model}"',
                        INVALID_MODEL_REASONING_CODE,
                    )
                seen_efforts.add(effort.id)
            if (
                resolved.reasoning.default_effort
                and resolved.reasoning.default_effort not in seen_efforts
            ):
                raise LlmError(
                    f'adapter returned an unknown default reasoning effort for provider "{provider}" model "{model}"',
                    INVALID_MODEL_REASONING_CODE,
                )
        return info

    # --- Call Config Resolution ---

    async def resolve_call_config(
        self,
        config: LlmCallConfig,
        signal: Any | None = None,
    ) -> LlmCallConfig:
        _ = self._adapters[config.provider]
        info = await self.resolve_model_info(config.provider, config.model, signal)
        return self._resolve_call_with_info(config, info)[0]

    def _resolve_call_with_info(
        self,
        config: LlmCallConfig,
        info: LlmResolvedModelInfo,
    ) -> tuple[LlmCallConfig, LlmModelContext | None, LlmResolvedModelInfo]:
        defaulted = config
        if config.max_tokens is None and info.default_max_tokens is not None:
            defaulted = LlmCallConfig(
                provider=config.provider,
                model=config.model,
                temperature=config.temperature,
                top_p=config.top_p,
                max_tokens=info.default_max_tokens,
                reasoning_effort=config.reasoning_effort,
                stop=config.stop,
                seed=config.seed,
                tool_history=config.tool_history,
            )

        if info.reasoning:
            requested = defaulted.reasoning_effort
            effective = requested or info.reasoning.default_effort
            if effective:
                if not any(e.id == effective for e in info.reasoning.efforts):
                    raise LlmError(
                        f'provider "{config.provider}" model "{config.model}" does not support reasoning effort "{effective}"',
                        UNSUPPORTED_REASONING_EFFORT_CODE,
                    )
                if requested != effective:
                    defaulted = LlmCallConfig(
                        provider=defaulted.provider,
                        model=defaulted.model,
                        temperature=defaulted.temperature,
                        top_p=defaulted.top_p,
                        max_tokens=defaulted.max_tokens,
                        reasoning_effort=effective,
                        stop=defaulted.stop,
                        seed=defaulted.seed,
                        tool_history=defaulted.tool_history,
                    )
        elif defaulted.reasoning_effort:
            raise LlmError(
                f'provider "{config.provider}" model "{config.model}" does not support reasoning effort "{defaulted.reasoning_effort}"',
                UNSUPPORTED_REASONING_EFFORT_CODE,
            )

        return defaulted, info.context, info

    # --- Prepared Calls ---

    async def prepare_call(
        self,
        config: LlmCallConfig,
        signal: Any | None = None,
        configure: Callable[[dict[str, Any], LlmResolvedModelInfo], dict[str, Any]] | None = None,
    ) -> PreparedLlmCall:
        if signal and getattr(signal, "aborted", False):
            raise asyncio.CancelledError()

        registration = self._adapters[config.provider]
        adapter_call = await registration.adapter.prepare_call(
            config.provider, config.model, signal
        )

        if signal and getattr(signal, "aborted", False):
            raise asyncio.CancelledError()

        model_info = self._normalize_model_info(registration, config.model, adapter_call.model)

        if signal and getattr(signal, "aborted", False):
            raise asyncio.CancelledError()

        proposed = config
        if configure:
            controls = {
                "temperature": config.temperature,
                "top_p": config.top_p,
                "max_tokens": config.max_tokens,
                "reasoning_effort": config.reasoning_effort,
                "stop": config.stop,
                "seed": config.seed,
                "tool_history": config.tool_history,
            }
            proposed_controls = configure(
                deep_freeze(structured_clone(controls)), deep_freeze(model_info)
            )
            proposed = LlmCallConfig(
                provider=config.provider,
                model=config.model,
                **proposed_controls,
            )

        if signal and getattr(signal, "aborted", False):
            raise asyncio.CancelledError()

        resolved_config, context, model_info = self._resolve_call_with_info(proposed, model_info)

        adapter_defaults = LlmCallConfigAdapterDefaults(
            reasoning_effort=config.reasoning_effort is None
            and resolved_config.reasoning_effort is not None,
            max_tokens=config.max_tokens is None and resolved_config.max_tokens is not None,
        )

        dispatched = False

        async def stream(options: GenerateOptions) -> AsyncIterable[StreamChunk]:
            nonlocal dispatched
            if dispatched:
                raise LlmError(
                    "a prepared LLM call can only be dispatched once", INVALID_PREPARED_CALL_CODE
                )
            # Compare only the LlmCallConfig fields
            config_fields = {
                f: options.get(f) for f in resolved_config.__dataclass_fields__ if f in options
            }
            if not call_config_equals(config_fields, resolved_config):
                raise LlmError(
                    "prepared LLM call config changed before adapter dispatch",
                    INVALID_PREPARED_CALL_CODE,
                )
            dispatched = True

            # Project messages for this adapter
            projected_options = await self._project_for_adapter(
                options, resolved_config, model_info, registration.adapter
            )

            async for chunk in adapter_call.stream(projected_options):
                yield chunk

        return PreparedLlmCall(
            config=resolved_config,
            retry_policy=registration.retry_policy,
            adapter_defaults=adapter_defaults,
            context=context,
            input_modalities=model_info.input_modalities,
            system_prompt_update=model_info.system_prompt_update,
            tool_update=model_info.tool_update,
            stream=stream,
        )

    async def _project_for_adapter(
        self,
        options: GenerateOptions,
        config: LlmCallConfig,
        model_info: LlmResolvedModelInfo,
        adapter: LlmAdapter,
    ) -> GenerateOptions:
        messages = options.get("messages", [])

        # Remove replay state from other adapters
        new_messages: list[RequestMessage] = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("source", {}).get("replayState"):
                source = msg["source"]
                provider = source.get("provider")
                reg = self._adapters.get(provider) if provider else None
                if reg is not None and reg.adapter != adapter:
                    new_messages.append(
                        {
                            **msg,
                            "source": {
                                "kind": "model",
                                "provider": source["provider"],
                                "model": source["model"],
                            },
                        }
                    )
                else:
                    new_messages.append(msg)
            else:
                new_messages.append(msg)

        # Project files to text
        if any(
            content is not None and content_has_file(content)
            for m in new_messages
            if (content := m.get("content")) is not None
        ):
            new_messages = project_files_to_text(
                new_messages, lambda ref: self._file_read_path(ref)
            )

        # Project images for text-only models
        if model_info.input_modalities and "image" not in model_info.input_modalities:
            if any(
                content is not None and content_has_image(content)
                for m in new_messages
                if (content := m.get("content")) is not None
            ):
                new_messages = project_images_for_text_model(new_messages)

        # Project tool updates
        projected_tools = project_tool_updates(
            new_messages, options.get("tools"), model_info.tool_update, config.tool_history
        )

        result = cast(GenerateOptions, dict(options))
        if new_messages != messages or projected_tools[1] != options.get("tools"):
            result = cast(
                GenerateOptions,
                {
                    **result,
                    "messages": new_messages,
                    **({"tools": projected_tools[1]} if projected_tools[1] is not None else {}),
                },
            )

        return result

    def _file_read_path(self, ref: dict[str, Any]) -> str | None:
        try:
            attachments = self.ctx.get("attachments")
            host_path = attachments.file_host_path(ref) if attachments else None
        except Exception:
            return None
        if not host_path:
            return None
        fs = self.ctx.get("fs")
        return fs.process_path_from_host_path(host_path) if fs else None

    # --- Stream with Waterfall ---

    async def stream(
        self,
        options: GenerateOptions,
    ) -> AsyncIterable[StreamChunk]:
        """Main entry point: runs the waterfall then the adapter stream."""

        # Run waterfall
        async def run_waterfall(opts: GenerateOptions) -> AsyncIterable[StreamChunk]:
            for listener in self.ctx.events.dispatch("waterfall", ["llm/stream"], [opts]) or []:
                try:
                    result = listener(opts)
                    if inspect.isasyncgen(result):
                        async for chunk in result:
                            yield chunk
                        return
                except Exception:
                    pass
            # No waterfall listener handled it - run adapter directly
            async for chunk in self._adapter_stream(opts):
                yield chunk

        async for chunk in run_waterfall(options):
            yield chunk

    async def _adapter_stream(self, options: GenerateOptions) -> AsyncIterable[StreamChunk]:
        """Direct adapter dispatch with error handling."""
        provider = options.get("provider")
        if not provider:
            yield self._adapter_failure_chunk(
                LlmError("provider is required", INVALID_ADAPTER_CODE)
            )
            return

        registration = self._adapters.get(provider)
        if not registration:
            yield self._adapter_failure_chunk(
                LlmError(f'no adapter registered for provider "{provider}"', NO_ADAPTER_CODE)
            )
            return

        adapter = registration.adapter

        try:
            async for chunk in adapter.stream(options):
                yield chunk
        except Exception as e:
            yield self._adapter_failure_chunk(e)

    def _adapter_failure_chunk(self, error: Exception) -> StreamChunk:
        return {
            "type": "error",
            "error": {
                "message": str(error),
                "code": getattr(error, "code", "ADAPTER_ERROR"),
                "status": getattr(error, "details", {}).get("status"),
            },
        }


__all__ = ["LlmRuntime"]
