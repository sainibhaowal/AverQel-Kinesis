"""Turn orchestration package for DeepSpaceChatService."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from app.auth.dependencies import AuthContext
from app.deepspace.services.chat_service.turns.prepare import _TurnPrepareMixin
from app.deepspace.services.chat_service.turns.replay import _TurnReplayMixin
from app.deepspace.services.chat_service.turns.round import _TurnRoundMixin
from app.deepspace.services.chat_service.turns.settle import _TurnSettleMixin
from app.deepspace.services.chat_service.turns.tool_calls import _TurnToolCallsMixin
from app.deepspace.services.chat_service.utils import (
    DeepSpaceEmptyResponseError,
    DeepSpaceToolExecutionError,
    ProviderContinuationPersistenceError,
    _TurnState,
    sse,
)
from app.providers.services.base import ProviderRequestError

logger = logging.getLogger(__name__)


class _TurnsMixin(
    _TurnPrepareMixin,
    _TurnReplayMixin,
    _TurnRoundMixin,
    _TurnToolCallsMixin,
    _TurnSettleMixin,
):
    """Composes all turn lifecycle phases for DeepSpaceChatService."""

    async def _request_disconnected(self, request: Any | None) -> bool:
        if request is None:
            return False
        checker = getattr(request, "is_disconnected", None)
        if not callable(checker):
            return False
        try:
            return bool(await checker())
        except Exception:  # noqa: BLE001
            return False

    async def _replay_existing_request(
        self,
        *,
        conversation_id: uuid.UUID,
        existing_turn: tuple[Any, Any],
    ) -> AsyncIterator[str]:
        _existing_user, existing_assistant = existing_turn
        existing_content = str(
            existing_assistant.active_version.content
            if existing_assistant.active_version is not None
            else existing_assistant.content
        )
        existing_metadata = (
            existing_assistant.metadata_json
            if isinstance(existing_assistant.metadata_json, dict)
            else {}
        )
        existing_status = str(existing_metadata.get("status") or "streaming")
        yield sse(
            "start",
            {
                "conversation_id": str(conversation_id),
                "message_id": str(existing_assistant.id),
                "started_at": existing_assistant.created_at,
                "replayed": True,
            },
        )
        if existing_content.strip() and existing_status not in {"streaming", "error"}:
            yield sse("replace", {"content": existing_content, "replayed": True})
            yield sse(
                "done",
                {
                    "conversation_id": str(conversation_id),
                    "message_id": str(existing_assistant.id),
                    "status": existing_status,
                    "replayed": True,
                },
            )
        elif existing_status == "error":
            yield sse(
                "error",
                {
                    "code": str(existing_metadata.get("error_code") or "LLM_REQUEST_FAILED"),
                    "message": existing_content
                    or "The original DeepSpace request failed safely; please retry.",
                    "replayed": True,
                },
            )
        else:
            yield sse(
                "error",
                {
                    "code": "DUPLICATE_REQUEST_IN_PROGRESS",
                    "message": "This DeepSpace request is already running. Please wait for it to finish.",
                    "replayed": True,
                },
            )

    @staticmethod
    def _workload_for_prompt(
        *, prompt: str, research_requested: bool
    ) -> Literal["balanced", "conversation", "research", "coding"]:
        if research_requested:
            return "research"
        if re.search(
            r"\b(code|coding|debug|stack trace|implement|repository|file)\b", prompt, re.I
        ):
            return "coding"
        return "balanced"

    async def stream_turn(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID | None,
        prompt: str,
        existing_assistant_message_id: uuid.UUID | None = None,
        client_request_id: str | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        request: Any | None = None,
        resume_approval_id: str | None = None,
        resume_user_question_id: str | None = None,
        resume_from_request_id: str | None = None,
        attachment_file_ids: list[str] | None = None,
        preset: str | None = None,
        plan_mode: bool | None = None,
    ) -> AsyncIterator[str]:
        state = _TurnState(
            auth=auth,
            conversation_id=conversation_id,
            prompt=prompt,
            existing_assistant_message_id=existing_assistant_message_id,
            client_request_id=client_request_id,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
            request=request,
            resume_approval_id=resume_approval_id,
            resume_user_question_id=resume_user_question_id,
            resume_from_request_id=resume_from_request_id,
            attachment_file_ids=attachment_file_ids,
            preset=preset,
            plan_mode=plan_mode,
        )
        try:
            async for frame in self._stream_turn(state):
                yield frame
        finally:
            self._close_telemetry_spans(state)

    async def _stream_turn(
        self,
        state: _TurnState,
    ) -> AsyncIterator[str]:
        """Run the prepared turn phases and settlement for one state."""
        async for frame in self._prepare_turn(state):
            yield frame
        if state.aborted:
            return
        try:
            async for frame in self._replay_pending_tool_results(state):
                yield frame
            while True:
                state.control = None
                async for frame in self._stream_model_round(state):
                    yield frame
                if state.control == "break":
                    break
                if state.control == "continue":
                    continue
                state.control = None
                async for frame in self._handle_tool_calls(state):
                    yield frame
                if state.control in {"break", "return"}:
                    break
            async for frame in self._settle_turn(state):
                yield frame
        except ProviderContinuationPersistenceError as exc:
            state.exc = exc
            state.terminal_status = "failed"
            state.message = str(state.exc)
            if state.run_id is not None:
                self.runtime.finish(
                    run_id=state.run_id,
                    status=state.terminal_status,
                    error="provider_continuation_persistence_failed",
                )
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code="DEEPSPACE_CONTINUATION_PERSISTENCE_FAILED",
                message=state.message,
                candidate=state.candidate,
            )
            logger.error("DeepSpace could not securely persist provider continuation data")
            self._record_request_metric(
                auth=state.auth,
                conversation_id=state.conversation_id,
                candidate=state.candidate,
                outcome="failed",
                started_monotonic=state.request_started_monotonic,
                tool_profile=state.tool_profile,
                error_code="DEEPSPACE_CONTINUATION_PERSISTENCE_FAILED",
            )
            yield sse(
                "error",
                {
                    "code": "DEEPSPACE_CONTINUATION_PERSISTENCE_FAILED",
                    "message": state.message,
                    "error_category": "runtime",
                },
            )
            return
        except ProviderRequestError as exc:
            state.exc = exc
            state.terminal_status = "failed"
            if not getattr(
                state.exc, "no_retry", False
            ) and self._provider_error_affects_availability(state.exc):
                self._record_provider_failure(auth=state.auth, candidate=state.candidate)
            state.provider_error_code = self._provider_error_code(state.exc)
            state.safe_provider_message = self._safe_provider_error_message(state.exc)
            if state.run_id is not None:
                self.runtime.finish(
                    run_id=state.run_id,
                    status=state.terminal_status,
                    error=state.safe_provider_message,
                )
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code=state.provider_error_code,
                message=state.safe_provider_message,
                candidate=state.candidate,
            )
            logger.warning("DeepSpace provider request failed", exc_info=True)
            self._record_request_metric(
                auth=state.auth,
                conversation_id=state.conversation_id,
                candidate=state.candidate,
                outcome="failed",
                started_monotonic=state.request_started_monotonic,
                tool_profile=state.tool_profile,
                error_code=state.provider_error_code,
            )
            yield sse(
                "error",
                {
                    "code": state.provider_error_code,
                    "message": state.safe_provider_message,
                    "error_category": self._provider_error_category(state.exc),
                },
            )
            return
        except DeepSpaceEmptyResponseError as exc:
            state.exc = exc
            state.terminal_status = "failed"
            self._record_provider_failure(auth=state.auth, candidate=state.candidate)
            if state.run_id is not None:
                self.runtime.finish(
                    run_id=state.run_id,
                    status=state.terminal_status,
                    error="empty_provider_response",
                )
            state.message = "The selected provider returned no usable response. Please retry or choose another model."
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code="LLM_EMPTY_RESPONSE",
                message=state.message,
                candidate=state.candidate,
            )
            logger.warning("DeepSpace provider returned an empty stream: %s", state.exc)
            self._record_request_metric(
                auth=state.auth,
                conversation_id=state.conversation_id,
                candidate=state.candidate,
                outcome="failed",
                started_monotonic=state.request_started_monotonic,
                tool_profile=state.tool_profile,
                error_code="LLM_EMPTY_RESPONSE",
            )
            yield sse(
                "error",
                {
                    "code": "LLM_EMPTY_RESPONSE",
                    "message": state.message,
                    "error_category": "provider",
                },
            )
            return
        except DeepSpaceToolExecutionError as exc:
            state.exc = exc
            state.terminal_status = "failed"
            state.message = (
                str(state.exc) or "A DeepSpace tool failed. Review the tool error before retrying."
            )
            if state.run_id is not None:
                self.runtime.finish(
                    run_id=state.run_id, status=state.terminal_status, error=state.message
                )
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code="DEEPSPACE_TOOL_FAILED",
                message=state.message,
                candidate=state.candidate,
            )
            self._record_request_metric(
                auth=state.auth,
                conversation_id=state.conversation_id,
                candidate=state.candidate,
                outcome="failed",
                started_monotonic=state.request_started_monotonic,
                tool_profile=state.tool_profile,
                error_code="DEEPSPACE_TOOL_FAILED",
            )
            yield sse(
                "error",
                {
                    "code": "DEEPSPACE_TOOL_FAILED",
                    "message": state.message,
                    "error_category": "tool",
                },
            )
            return
        except TimeoutError:
            # This is an expected provider-side idle-stream failure, not an
            # internal DeepSpace execution fault.  Preserve the durable turn
            # as failed with an actionable category and let the queue advance.
            state.terminal_status = "failed"
            self._record_provider_failure(auth=state.auth, candidate=state.candidate)
            state.message = (
                "The selected model stopped sending response data before the interactive "
                "deadline. DeepSpace ended this turn so the workspace remains responsive. "
                "Please retry or choose another model."
            )
            if state.run_id is not None:
                self.runtime.finish(
                    run_id=state.run_id,
                    status=state.terminal_status,
                    error="provider_stream_timeout",
                )
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code="LLM_REQUEST_TIMEOUT",
                message=state.message,
                candidate=state.candidate,
            )
            logger.warning("DeepSpace provider stream timed out")
            self._record_request_metric(
                auth=state.auth,
                conversation_id=state.conversation_id,
                candidate=state.candidate,
                outcome="failed",
                started_monotonic=state.request_started_monotonic,
                tool_profile=state.tool_profile,
                error_code="LLM_REQUEST_TIMEOUT",
            )
            yield sse(
                "error",
                {
                    "code": "LLM_REQUEST_TIMEOUT",
                    "message": state.message,
                    "error_category": "provider",
                },
            )
            return
        except Exception:
            state.terminal_status = "failed"
            if state.run_id is not None:
                self.runtime.finish(
                    run_id=state.run_id, status=state.terminal_status, error="stream_failed"
                )
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code="DEEPSPACE_STREAM_FAILED",
                message="DeepSpace could not complete this response. Please retry.",
                candidate=state.candidate,
            )
            logger.exception("DeepSpace stream failed")
            self._record_request_metric(
                auth=state.auth,
                conversation_id=state.conversation_id,
                candidate=state.candidate,
                outcome="failed",
                started_monotonic=state.request_started_monotonic,
                tool_profile=state.tool_profile,
                error_code="DEEPSPACE_STREAM_FAILED",
            )
            yield sse(
                "error",
                {
                    "code": "DEEPSPACE_STREAM_FAILED",
                    "message": "DeepSpace could not complete this response.",
                    "error_category": "runtime",
                },
            )
            return
