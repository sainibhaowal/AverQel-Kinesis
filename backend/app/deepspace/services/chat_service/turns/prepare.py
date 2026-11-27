"""Turn prepare phase mixin for DeepSpaceChatService."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

from sqlalchemy import select

from app.deepspace.models.workspace_file import DeepSpaceWorkspaceFile
from app.deepspace.services.chat_service.utils import (
    _PRIVATE_PROVIDER_CONTINUATIONS_KEY,
    CLARIFICATION_TOOL_NAMES,
    DEEPSPACE_AGENT_POLICY,
    DEEPSPACE_COMPACT_POLICY,
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    MAX_RAW_HISTORY_MESSAGES,
    NATIVE_TOOL_REGISTRY,
    _epoch_seconds,
    _pkg_attr,
    _TurnState,
    sse,
)
from app.deepspace.services.compaction_pipeline import compact_request
from app.deepspace.services.deployment_persona import deployment_persona_section
from app.deepspace.services.extensions import (
    ExtensionSet,
    alias_definition,
    extensions_for,
)
from app.deepspace.services.goal_system_prompt import (
    blocked_threshold_from_settings,
    goal_system_prompt,
)
from app.deepspace.services.library_storage import safe_extraction_status
from app.deepspace.services.loop_guard import (
    LoopGuardLimits,
    loop_guard_registry,
)
from app.deepspace.services.mcp_connection_intent import (
    has_matching_connected_mcp,
    is_explicit_mcp_action_request,
    missing_stdio_mcp_options,
)
from app.deepspace.services.plugins import (
    plugin_guidance,
    plugin_registry,
)
from app.deepspace.services.run_events import cancellation_key
from app.deepspace.services.session_events import (
    TURN_START,
)
from app.deepspace.services.time_context import (
    parse_browser_time_zone,
)
from app.deepspace.services.tool_requirements import (
    excludes_mcp_for_prompt,
    requires_tool_execution,
)
from app.deepspace.services.tools import (
    unavailable_tool_names,
)
from app.deepspace.services.workspace_instructions import (
    prepare_instruction_message,
    prepare_skill_catalog_message,
)
from app.system.services.rate_limit_service import RateLimitService

logger = logging.getLogger(__name__)


class _TurnPrepareMixin:
    async def _prepare_turn(
        self,
        state: _TurnState,
    ) -> AsyncIterator[str]:
        """Phase 1 of a turn: identity, history, provider, tools and budget."""
        state.request_started_monotonic = time.monotonic()
        state.resume_approval_id = str(state.resume_approval_id or "").strip() or None
        if bool(getattr(self.settings, "deepspace_loop_guard_enabled", True)):
            # Each turn starts with a fresh budget, so a long conversation is
            # not stopped by the work of an earlier turn. An approval resume
            # is still the same logical turn and restores its checkpoint below.
            guard = loop_guard_registry().get(
                state.conversation_id,
                LoopGuardLimits.from_settings(self.settings),
            )
            if not state.resume_approval_id:
                guard.start_turn()
        if state.conversation_id is not None:
            try:
                prior_msgs = self._messages(auth=state.auth, conversation_id=state.conversation_id)
                state.turn_number = (
                    sum(1 for m in prior_msgs if isinstance(m, dict) and m.get("role") == "user")
                    + 1
                )
            except Exception:
                state.turn_number = 1
        else:
            state.turn_number = 1
        self._open_telemetry_span(
            state,
            self.session_telemetry.session_span(state.conversation_id),
        )
        self._open_telemetry_span(
            state,
            self.session_telemetry.turn_span(state.conversation_id, state.turn_number, None),
        )
        self._record_workspace_baseline(state)
        state.prompt = " ".join(state.prompt.strip().split())
        state.tool_call_required = requires_tool_execution(state.prompt)
        state.tool_requirement_satisfied = False
        state.tool_call_recovery_retries = 0
        state.tool_schema_names = []
        state.round_tool_names = []
        state.tool_choice_history = []
        state.client_request_id = str(state.client_request_id or "").strip() or None
        state.provider_cancel_key = (
            cancellation_key(state.auth.tenant_id, state.auth.user_id, state.client_request_id)
            if state.client_request_id
            else None
        )
        state.resume_user_question_id = str(state.resume_user_question_id or "").strip() or None
        state.resume_from_request_id = str(state.resume_from_request_id or "").strip() or None
        state.attachment_file_ids = list(
            dict.fromkeys(str(item) for item in (state.attachment_file_ids or []))
        )[:10]
        if state.resume_approval_id and state.resume_user_question_id:
            yield sse(
                "error",
                {
                    "code": "INVALID_RESUME_REQUEST",
                    "message": "Only one pending DeepSpace request can be resumed at a time.",
                },
            )
            state.control = "break"
            state.aborted = True
            return
        state.resumed_pending = None
        state.resumed_user_question = None
        state.resumed_failed_run = None
        state.retry_from_saved_prompt = False
        state.resume_denied = False
        state.run_id = None
        state.attachments = []
        state.attachment_rows = []

        if state.attachment_file_ids:
            if state.conversation_id is None:
                yield sse(
                    "error",
                    {
                        "code": "ATTACHMENT_CONVERSATION_REQUIRED",
                        "message": "Choose a DeepSpace chat before attaching files.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            try:
                state.requested_ids = [uuid.UUID(item) for item in state.attachment_file_ids]
            except ValueError:
                yield sse(
                    "error",
                    {
                        "code": "INVALID_ATTACHMENT",
                        "message": "An attachment reference is invalid.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.rows = (
                self.db.execute(
                    select(DeepSpaceWorkspaceFile).where(
                        DeepSpaceWorkspaceFile.id.in_(state.requested_ids),
                        DeepSpaceWorkspaceFile.tenant_id == state.auth.tenant_id,
                        DeepSpaceWorkspaceFile.user_id == state.auth.user_id,
                        DeepSpaceWorkspaceFile.conversation_id == state.conversation_id,
                    )
                )
                .scalars()
                .all()
            )
            state.by_id = {str(row.id): row for row in state.rows}
            if len(state.by_id) != len(state.requested_ids):
                yield sse(
                    "error",
                    {
                        "code": "ATTACHMENT_NOT_FOUND",
                        "message": "One or more selected Library files are unavailable.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.attachment_rows = [state.by_id[str(file_id)] for file_id in state.requested_ids]
            state.attachments = [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "content_type": row.content_type,
                    "size_bytes": row.size_bytes,
                    "source": row.source,
                    **(
                        {
                            "extraction_status": safe_extraction_status(
                                row.metadata_json["extraction_status"]
                            )
                        }
                        if isinstance(row.metadata_json, dict)
                        and isinstance(row.metadata_json.get("extraction_status"), dict)
                        else {}
                    ),
                }
                for row in state.attachment_rows
            ]
            if not state.prompt:
                state.prompt = "Please review the attached Library file(s)."

        if state.resume_approval_id:
            if state.conversation_id is None:
                yield sse(
                    "error",
                    {
                        "code": "APPROVAL_CONVERSATION_REQUIRED",
                        "message": "A conversation is required to resume an approval.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.run = self.runtime.get_run_for_approval(
                tenant_id=state.auth.tenant_id,
                user_id=state.auth.user_id,
                conversation_id=state.conversation_id,
                approval_id=state.resume_approval_id,
            )
            if state.run is None:
                yield sse(
                    "error",
                    {
                        "code": "APPROVAL_NOT_FOUND",
                        "message": "The approval request is no longer available.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.checkpoint = (
                state.run.checkpoint if isinstance(state.run.checkpoint, dict) else {}
            )
            if bool(getattr(self.settings, "deepspace_loop_guard_enabled", True)):
                guard = loop_guard_registry().get(
                    state.conversation_id,
                    LoopGuardLimits.from_settings(self.settings),
                )
                if not guard.restore(state.checkpoint.get("loop_guard")):
                    # Old checkpoints have no loop state. Start a clean bounded
                    # continuation rather than inheriting stale process memory.
                    guard.start_turn()
            state.pending = state.checkpoint.get("pending_approval")
            if not isinstance(state.pending, dict) or str(
                state.pending.get("decision") or ""
            ) not in {
                "approved",
                "denied",
            }:
                yield sse(
                    "error",
                    {
                        "code": "APPROVAL_NOT_RESOLVED",
                        "message": "The approval decision has not been recorded.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.assistant_message_id = state.run.assistant_message_id
            if state.assistant_message_id is None:
                yield sse(
                    "error",
                    {
                        "code": "APPROVAL_RUN_INVALID",
                        "message": "The approval run is missing its assistant message.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.assistant_message = self.chat.get_message_by_conversation(
                tenant_id=state.auth.tenant_id,
                conversation_id=state.conversation_id,
                message_id=state.assistant_message_id,
                user_id=state.auth.user_id,
            )
            if state.assistant_message is None:
                yield sse(
                    "error",
                    {
                        "code": "APPROVAL_MESSAGE_NOT_FOUND",
                        "message": "The approval message no longer exists.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.original_user_message = self._original_user_message_for_assistant(
                auth=state.auth,
                conversation_id=state.conversation_id,
                assistant_message_id=state.assistant_message.id,
            )
            if state.original_user_message is None:
                yield sse(
                    "error",
                    {
                        "code": "RESUME_ORIGINAL_PROMPT_NOT_FOUND",
                        "message": "The original task for this approval could not be found, so DeepSpace did not resume it.",
                        "error_category": "runtime",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.prompt = (
                state.original_user_message.active_version.content
                if state.original_user_message.active_version
                else state.original_user_message.content
            )
            state.previous = self._messages(
                auth=state.auth,
                conversation_id=state.conversation_id,
                exclude_message_id=state.assistant_message.id,
                through_user_message_id=state.original_user_message.id,
            )
            state.run_id = state.run.id
            state.resumed_pending = dict(state.pending)
            state.resume_denied = str(state.pending.get("decision")) == "denied"
            self._restore_subagent_timelines(state)
        elif state.resume_user_question_id:
            if state.conversation_id is None:
                yield sse(
                    "error",
                    {
                        "code": "QUESTION_CONVERSATION_REQUIRED",
                        "message": "A conversation is required to answer this question.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            if not state.prompt:
                yield sse(
                    "error",
                    {"code": "EMPTY_MESSAGE", "message": "An answer is required."},
                )
                state.control = "break"
                state.aborted = True
                return
            # The HTTP endpoint claims and persists a clarification answer
            # before scheduling this worker.  Accept only that claimed run;
            # the legacy lookup remains as a compatibility fallback for
            # in-process callers that do not pass through the endpoint.
            state.get_claimed_question = getattr(self.runtime, "get_claimed_user_question", None)
            state.run = (
                state.get_claimed_question(
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    question_id=state.resume_user_question_id,
                )
                if callable(state.get_claimed_question)
                else None
            )
            state.claimed_by_endpoint = state.run is not None
            if state.run is None:
                state.run = self.runtime.get_run_for_user_question(
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    question_id=state.resume_user_question_id,
                )
            if state.run is None:
                yield sse(
                    "error",
                    {
                        "code": "QUESTION_NOT_FOUND",
                        "message": "This DeepSpace question is no longer awaiting an answer.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.checkpoint = (
                state.run.checkpoint if isinstance(state.run.checkpoint, dict) else {}
            )
            state.pending = state.checkpoint.get("pending_user_question")
            if not isinstance(state.pending, dict):
                yield sse(
                    "error",
                    {
                        "code": "QUESTION_NOT_FOUND",
                        "message": "This DeepSpace question is no longer awaiting an answer.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.assistant_message_id = state.run.assistant_message_id
            if state.assistant_message_id is None:
                yield sse(
                    "error",
                    {
                        "code": "QUESTION_RUN_INVALID",
                        "message": "The question run is missing its assistant message.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.assistant_message = self.chat.get_message_by_conversation(
                tenant_id=state.auth.tenant_id,
                conversation_id=state.conversation_id,
                message_id=state.assistant_message_id,
                user_id=state.auth.user_id,
            )
            if state.assistant_message is None:
                yield sse(
                    "error",
                    {
                        "code": "QUESTION_MESSAGE_NOT_FOUND",
                        "message": "The question message no longer exists.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.original_user_message = self._original_user_message_for_assistant(
                auth=state.auth,
                conversation_id=state.conversation_id,
                assistant_message_id=state.assistant_message.id,
            )
            if state.original_user_message is None:
                yield sse(
                    "error",
                    {
                        "code": "RESUME_ORIGINAL_PROMPT_NOT_FOUND",
                        "message": "The original task for this clarification could not be found, so DeepSpace did not resume it.",
                        "error_category": "runtime",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.previous = self._messages(
                auth=state.auth,
                conversation_id=state.conversation_id,
                exclude_message_id=state.assistant_message.id,
                through_user_message_id=state.original_user_message.id,
            )
            state.run_id = state.run.id
            state.resumed_user_question = dict(state.pending)
            state.resumed_user_question["answer"] = state.prompt
            self._restore_subagent_timelines(state)
            if not state.claimed_by_endpoint:
                self.chat.add_message(
                    tenant_id=state.auth.tenant_id,
                    conversation_id=state.conversation_id,
                    role="user",
                    content=state.prompt,
                    metadata_json={
                        "answer_to_question_id": state.resume_user_question_id,
                        **(
                            {"client_request_id": state.client_request_id}
                            if state.client_request_id
                            else {}
                        ),
                    },
                )
                self.db.commit()
            self.runtime.update_checkpoint(
                run_id=state.run_id,
                status="running",
                checkpoint={
                    **state.checkpoint,
                    "status": "running",
                    "phase": "question_resumed",
                    "pending_user_question": state.resumed_user_question,
                },
            )
        elif state.resume_from_request_id:
            if state.conversation_id is None:
                yield sse(
                    "error",
                    {
                        "code": "RETRY_CONVERSATION_REQUIRED",
                        "message": "A conversation is required to retry a failed queue item.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.run = self.runtime.get_failed_run_for_request_id(
                tenant_id=state.auth.tenant_id,
                user_id=state.auth.user_id,
                conversation_id=state.conversation_id,
                request_id=state.resume_from_request_id,
            )
            state.assistant_message = (
                self.chat.get_message_by_conversation(
                    tenant_id=state.auth.tenant_id,
                    conversation_id=state.conversation_id,
                    message_id=state.run.assistant_message_id,
                    user_id=state.auth.user_id,
                )
                if state.run is not None and state.run.assistant_message_id is not None
                else None
            )
            if state.assistant_message is None:
                # Runtime checkpoints have a shorter retention horizon than
                # chat history. If the original turn still exists, retry the
                # saved prompt in its existing assistant slot rather than
                # leaving a permanently un-retryable queue item.
                state.original_turn = self.chat.find_turn_by_request_id(
                    tenant_id=state.auth.tenant_id,
                    conversation_id=state.conversation_id,
                    user_id=state.auth.user_id,
                    request_id=state.resume_from_request_id,
                )
                if state.original_turn is None:
                    yield sse(
                        "error",
                        {
                            "code": "RETRY_PROMPT_NOT_FOUND",
                            "message": "The original prompt is no longer available to retry.",
                        },
                    )
                    state.control = "break"
                    state.aborted = True
                    return
                state.original_user_message, state.assistant_message = state.original_turn
                state.prompt = (
                    state.original_user_message.active_version.content
                    if state.original_user_message.active_version
                    else state.original_user_message.content
                )
                state.previous = self._messages(
                    auth=state.auth,
                    conversation_id=state.conversation_id,
                    exclude_message_id=state.assistant_message.id,
                    through_user_message_id=state.original_user_message.id,
                )
                state.assistant_message.content = ""
                state.assistant_message.metadata_json = {
                    "status": "streaming",
                    "surface": "deepspace",
                    "client_request_id": state.client_request_id,
                    "retry_of_request_id": state.resume_from_request_id,
                    "retry_mode": "from_start",
                }
                self.db.commit()
                self._invalidate_context_cache(
                    auth=state.auth, conversation_id=state.conversation_id
                )
                state.retry_from_saved_prompt = True
                state.resume_from_request_id = None
            else:
                assert state.run is not None
                state.original_turn = self.chat.find_turn_by_request_id(
                    tenant_id=state.auth.tenant_id,
                    conversation_id=state.conversation_id,
                    user_id=state.auth.user_id,
                    request_id=state.resume_from_request_id,
                )
                if state.original_turn is None:
                    yield sse(
                        "error",
                        {
                            "code": "RETRY_PROMPT_NOT_FOUND",
                            "message": "The original prompt is no longer available to resume.",
                        },
                    )
                    state.control = "break"
                    state.aborted = True
                    return
                state.original_user_message, state.original_assistant_message = state.original_turn
                if state.original_assistant_message.id != state.assistant_message.id:
                    yield sse(
                        "error",
                        {
                            "code": "RETRY_PROMPT_NOT_FOUND",
                            "message": "The saved prompt does not match this failed run.",
                        },
                    )
                    state.control = "break"
                    state.aborted = True
                    return
                state.prompt = (
                    state.original_user_message.active_version.content
                    if state.original_user_message.active_version
                    else state.original_user_message.content
                )
                state.previous = self._messages(
                    auth=state.auth,
                    conversation_id=state.conversation_id,
                    exclude_message_id=state.assistant_message.id,
                    through_user_message_id=state.original_user_message.id,
                )
                state.resumed_failed_run = state.run
                state.run_id = state.run.id
                state.checkpoint = (
                    dict(state.run.checkpoint) if isinstance(state.run.checkpoint, dict) else {}
                )
                state.assistant_message.content = ""
                state.retry_metadata = {
                    "status": "streaming",
                    "surface": "deepspace",
                    "client_request_id": state.client_request_id,
                    "retry_of_request_id": state.resume_from_request_id,
                }
                state.old_continuations = (state.assistant_message.metadata_json or {}).get(
                    _PRIVATE_PROVIDER_CONTINUATIONS_KEY
                )
                if isinstance(state.old_continuations, dict):
                    state.retry_metadata[_PRIVATE_PROVIDER_CONTINUATIONS_KEY] = (
                        state.old_continuations
                    )
                state.assistant_message.metadata_json = state.retry_metadata
                self.db.commit()
                self.runtime.update_checkpoint(
                    run_id=state.run_id,
                    status="running",
                    checkpoint={
                        **state.checkpoint,
                        "status": "running",
                        "phase": "model",
                        "resume_from_request_id": state.resume_from_request_id,
                    },
                    last_error="",
                )
        elif state.existing_assistant_message_id is not None:
            if state.conversation_id is None:
                yield sse(
                    "error",
                    {
                        "code": "REGENERATE_CONVERSATION_REQUIRED",
                        "message": "A conversation is required to regenerate this response.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.assistant_message = self.chat.get_message_by_conversation(
                tenant_id=state.auth.tenant_id,
                conversation_id=state.conversation_id,
                message_id=state.existing_assistant_message_id,
                user_id=state.auth.user_id,
            )
            if state.assistant_message is None or state.assistant_message.role != "assistant":
                yield sse(
                    "error",
                    {
                        "code": "REGENERATE_MESSAGE_NOT_FOUND",
                        "message": "The original DeepSpace response could not be found.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.original_user_message = self._original_user_message_for_assistant(
                auth=state.auth,
                conversation_id=state.conversation_id,
                assistant_message_id=state.assistant_message.id,
            )
            if state.original_user_message is None:
                yield sse(
                    "error",
                    {
                        "code": "REGENERATE_PROMPT_NOT_FOUND",
                        "message": "The saved prompt for this response could not be found.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            # The durable queue passes the prompt snapshot from when the user
            # clicked Regenerate. Fall back to history only for legacy callers.
            if not state.prompt.strip():
                state.prompt = (
                    state.original_user_message.active_version.content
                    if state.original_user_message.active_version
                    else state.original_user_message.content
                )
                state.prompt = " ".join(state.prompt.strip().split())
            # Reuse the existing assistant row, preserve the previous answer
            # as a version, and anchor context at the original prompt. This
            # also prevents later messages from leaking into an older replay.
            state.previous = self._messages(
                auth=state.auth,
                conversation_id=state.conversation_id,
                exclude_message_id=state.assistant_message.id,
                through_user_message_id=state.original_user_message.id,
            )
            if state.previous and state.previous[-1].get("__deepspace_original_task"):
                state.previous[-1]["content"] = state.prompt
            state.regeneration_metadata = {
                "status": "streaming",
                "surface": "deepspace",
                "regenerating": True,
                **(
                    {"client_request_id": state.client_request_id}
                    if state.client_request_id
                    else {}
                ),
            }
            state.versioned_message = self.chat.create_message_version(
                tenant_id=state.auth.tenant_id,
                conversation_id=state.conversation_id,
                message_id=state.assistant_message.id,
                user_id=state.auth.user_id,
                content="",
                source_type="regenerate",
                activate=True,
                metadata_json=state.regeneration_metadata,
            )
            if state.versioned_message is None:
                yield sse(
                    "error",
                    {
                        "code": "REGENERATE_MESSAGE_NOT_FOUND",
                        "message": "The original DeepSpace response could not be found.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return
            state.assistant_message = state.versioned_message
            state.assistant_message.metadata_json = state.regeneration_metadata
            self.db.commit()
        elif not state.prompt:
            yield sse(
                "error",
                {"code": "EMPTY_MESSAGE", "message": "Message cannot be empty."},
            )
            state.control = "break"
            state.aborted = True
            return

        if state.request is not None and not state.resume_approval_id:
            RateLimitService(self.settings).enforce_deepspace_user_limit(
                request=state.request, user_id=str(state.auth.user_id)
            )

        if (
            not state.resume_approval_id
            and not state.resume_user_question_id
            and not state.resume_from_request_id
            and not state.retry_from_saved_prompt
            and state.existing_assistant_message_id is None
        ):
            if state.client_request_id:
                state.lock_request_id = getattr(self.chat, "lock_request_id", None)
                if callable(state.lock_request_id):
                    state.lock_request_id(state.client_request_id)
                state.existing_turn = self.chat.find_turn_by_request_id(
                    tenant_id=state.auth.tenant_id,
                    conversation_id=state.conversation_id,
                    user_id=state.auth.user_id,
                    request_id=state.client_request_id,
                )
                if state.existing_turn is not None:
                    state.conversation_id = state.existing_turn[0].conversation_id
                    async for state.frame in self._replay_existing_request(  # noqa: B020
                        conversation_id=state.conversation_id,
                        existing_turn=state.existing_turn,
                    ):
                        yield state.frame
                    state.control = "break"
                    state.aborted = True
                    return

            if state.conversation_id is None:
                state.conversation = self.chat.create_conversation(
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    title=state.prompt[:80],
                )
                state.conversation_id = state.conversation.id
            elif (
                self.chat.get_conversation(
                    tenant_id=state.auth.tenant_id,
                    conversation_id=state.conversation_id,
                    user_id=state.auth.user_id,
                )
                is None
            ):
                yield sse(
                    "error",
                    {
                        "code": "CONVERSATION_NOT_FOUND",
                        "message": "DeepSpace conversation not found.",
                    },
                )
                state.control = "break"
                state.aborted = True
                return

            # Refresh baseline once conversation_id is created/verified so a newly
            # created chat in a selected workspace snapshots that workspace root.
            resolved_root = self._resolve_workspace_root(state)
            if resolved_root is not None and resolved_root != getattr(
                state, "workspace_root", None
            ):
                self._record_workspace_baseline(state)

            if state.client_request_id:
                state.existing_turn = self.chat.find_turn_by_request_id(
                    tenant_id=state.auth.tenant_id,
                    conversation_id=state.conversation_id,
                    user_id=state.auth.user_id,
                    request_id=state.client_request_id,
                )
                if state.existing_turn is not None:
                    async for state.frame in self._replay_existing_request(  # noqa: B020
                        conversation_id=state.conversation_id,
                        existing_turn=state.existing_turn,
                    ):
                        yield state.frame
                    state.control = "break"
                    state.aborted = True
                    return

            state.previous = self._messages(auth=state.auth, conversation_id=state.conversation_id)
            self.chat.add_message(
                tenant_id=state.auth.tenant_id,
                conversation_id=state.conversation_id,
                role="user",
                content=state.prompt,
                metadata_json={
                    **(
                        {"client_request_id": state.client_request_id}
                        if state.client_request_id
                        else {}
                    ),
                    **({"attachments": state.attachments} if state.attachments else {}),
                }
                or None,
            )
            state.assistant_message = self.chat.add_message(
                tenant_id=state.auth.tenant_id,
                conversation_id=state.conversation_id,
                role="assistant",
                content="",
                metadata_json={
                    "status": "streaming",
                    "surface": "deepspace",
                    **(
                        {"client_request_id": state.client_request_id}
                        if state.client_request_id
                        else {}
                    ),
                },
            )
            if state.assistant_message is None:
                raise RuntimeError("DeepSpace could not create the assistant message.")
            state.assistant_message = cast(Any, state.assistant_message)
            self.db.commit()
            self._invalidate_context_cache(auth=state.auth, conversation_id=state.conversation_id)

        # The branch above either creates or validates the conversation.  Keep
        # the local value narrowed for the rest of this long-lived stream.
        if state.conversation_id is None:
            raise RuntimeError("DeepSpace requires a conversation before streaming.")
        if state.assistant_message is None:
            raise RuntimeError("DeepSpace requires an assistant message before streaming.")

        state.started_at = self._now()
        state.first_token_monotonic = None
        state.first_provider_request_monotonic = None
        state.provider_first_output_monotonic = None
        yield sse(
            "start",
            {
                "conversation_id": str(state.conversation_id),
                "message_id": str(state.assistant_message.id),
                "started_at": state.started_at,
                **(
                    {"regenerating": True}
                    if state.existing_assistant_message_id is not None
                    else {}
                ),
            },
        )
        if state.retry_from_saved_prompt:
            yield sse(
                "lifecycle",
                {
                    "phase": "retry_from_start",
                    "message": (
                        "The saved checkpoint is unavailable. Restarting the saved prompt from "
                        "the beginning; actions completed during the failed attempt may repeat."
                    ),
                },
            )
        yield sse(
            "lifecycle",
            {"phase": "resolving_provider", "message": "Selecting an authorized model."},
        )
        if (
            not state.resume_approval_id
            and not state.resume_user_question_id
            and not state.resume_from_request_id
        ):
            try:
                state.run = self.runtime.create_run(
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    assistant_message_id=state.assistant_message.id,
                    checkpoint={
                        "status": "starting",
                        "started_at": state.started_at,
                        **(
                            {"client_request_id": state.client_request_id}
                            if state.client_request_id
                            else {}
                        ),
                    },
                )
                state.run_id = state.run.id
                self._record_session_event(
                    auth=state.auth,
                    conversation_id=state.conversation_id,
                    run_id=state.run_id,
                    event_type=TURN_START,
                    turn=state.turn_number,
                    payload={"provider_request_started": True, "turn": state.turn_number},
                )
            except AttributeError:
                # Lightweight unit-test repositories can omit the runtime tables;
                # the production database always has them after the migration.
                logger.debug("DeepSpace runtime persistence is unavailable in this test double.")

        try:
            state.selection = self.providers.resolve_chat(
                tenant_id=state.auth.tenant_id,
                workspace_id=None,
                actor_user_id=state.auth.user_id,
                # A context meter must use the selected model's current
                # provider metadata whenever it is available. Resolution
                # still falls back to the cached descriptor and then the
                # verified model registry if discovery is unavailable.
                allow_live_model_discovery=True,
            )
            # Provider resolution can refresh the model metadata cache.  This
            # method then enters a long-lived streaming response, so leave no
            # transaction open while tokens or tool calls are in flight.
            # Otherwise concurrent chats can wait on the cache row lock and
            # eventually starve unrelated DeepSpace requests of DB connections.
            self.db.commit()
        except Exception:  # noqa: BLE001
            self.db.rollback()
            state.message = "DeepSpace could not resolve an enabled chat provider. Check provider configuration and try again."
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code="LLM_PROVIDER_UNAVAILABLE",
                message=state.message,
                candidate=None,
            )
            logger.exception("DeepSpace chat provider resolution failed")
            yield sse("error", {"code": "LLM_PROVIDER_UNAVAILABLE", "message": state.message})
            state.control = "break"
            state.aborted = True
            return
        # The user selected this model through the existing assignment UI.
        # A circuit prevents retry storms, but must never silently switch a
        # user onto another model/provider.
        state.candidate = state.selection.candidates[0] if state.selection.candidates else None
        # The conversation may name the model it runs on, on any provider it
        # may use, or let DeepSpace choose the model that fits the work.
        state.candidate = self._apply_conversation_model(state, state.candidate)
        if state.candidate is None:
            state.message = (
                "No DeepSpace chat model is configured. Select an enabled chat model and try again."
            )
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code="LLM_UNAVAILABLE",
                message=state.message,
                candidate=None,
            )
            yield sse(
                "error",
                {"code": "LLM_UNAVAILABLE", "message": state.message},
            )
            state.control = "break"
            state.aborted = True
            return
        if self.provider_circuit.is_open(
            tenant_id=state.auth.tenant_id,
            provider_config_id=getattr(state.candidate, "provider_config_id", None),
            model_name=state.candidate.model_name,
        ):
            state.message = (
                "Your selected model is temporarily unavailable after repeated failures. "
                "DeepSpace did not switch models. Retry later or choose another model yourself."
            )
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code="LLM_PROVIDER_CIRCUIT_OPEN",
                message=state.message,
                candidate=state.candidate,
            )
            yield sse(
                "lifecycle",
                {
                    "phase": "provider_unavailable",
                    "message": state.message,
                    "modelName": state.candidate.model_name,
                },
            )
            yield sse(
                "error",
                {
                    "code": "LLM_PROVIDER_CIRCUIT_OPEN",
                    "message": state.message,
                    "error_category": "provider",
                },
            )
            state.control = "break"
            state.aborted = True
            return

        yield sse(
            "lifecycle",
            {
                "phase": "provider_ready",
                "message": "Provider selected; preparing the request.",
                "providerType": state.candidate.provider_type,
                "modelName": state.candidate.model_name,
            },
        )

        state.visible_history = list(state.previous)
        # The pipeline runs before the window fit: it removes what the request
        # can still reach later, so the fit keeps as much of the conversation
        # as the model's window allows.
        state.pipeline_outcome = compact_request(
            state.previous,
            keep_recent=int(
                getattr(self.settings, "deepspace_compaction_keep_recent_messages", 24)
            ),
            prune_threshold=int(
                getattr(self.settings, "deepspace_tool_result_prune_threshold", 8_192)
            ),
        )
        if state.pipeline_outcome.changed:
            state.previous = state.pipeline_outcome.messages
        state.previous, state.history_compacted = self._fit_history_to_context(
            state.previous,
            context_window=state.candidate.context_window,
            max_output_tokens=self.settings.llm_max_tokens_per_request,
        )
        state.previous, state.rolling_history_compacted = self._compact_history_for_request(
            state.previous
        )
        if state.rolling_history_compacted:
            try:
                state.durable_summary = self.context_summaries.get(
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                )
            except Exception:  # noqa: BLE001
                state.durable_summary = None
            if state.durable_summary is not None and state.durable_summary.summary_text.strip():
                state.previous = [
                    {
                        "role": "system",
                        "content": state.durable_summary.summary_text,
                    },
                    *state.previous[-MAX_RAW_HISTORY_MESSAGES:],
                ]
        state.history_compacted = state.history_compacted or state.rolling_history_compacted

        state.meta = {
            "conversation_id": str(state.conversation_id),
            "message_id": str(state.assistant_message.id),
            "model_name": state.candidate.model_name,
            "provider_type": state.candidate.provider_type,
        }
        if state.candidate.context_window is not None:
            state.meta["context_window"] = state.candidate.context_window
            state.meta["context_limit"] = state.candidate.context_window
        if state.candidate.context_window_source:
            state.meta["context_limit_source"] = state.candidate.context_window_source
        yield sse("meta", state.meta)
        try:
            state.provider = self.registry.get_chat_provider_from_selection(state.candidate)
        except Exception:  # noqa: BLE001
            state.message = "DeepSpace could not initialize the selected chat provider. Please retry or choose another model."
            self._persist_stream_failure(
                assistant_message=state.assistant_message,
                auth=state.auth,
                conversation_id=state.conversation_id,
                code="LLM_PROVIDER_INIT_FAILED",
                message=state.message,
                candidate=state.candidate,
            )
            logger.exception("DeepSpace chat provider initialization failed")
            yield sse("error", {"code": "LLM_PROVIDER_INIT_FAILED", "message": state.message})
            state.control = "break"
            state.aborted = True
            return
        # Media providers do not share a progress API. Emit only lifecycle
        # states we know to be true; never invent a percentage for a provider
        # that only returns a completed binary result.
        if self._is_native_media_model(state.candidate.model_name):
            yield sse(
                "media_status",
                {"phase": "queued", "message": "Media request accepted."},
            )
            yield sse(
                "media_status",
                {
                    "phase": "generating",
                    "message": "Generating media with the selected model.",
                },
            )
        # Tool access is a connected-account capability, not a provider
        # allowlist or manually entered conversation scope.
        # Every registered chat adapter translates the common tool contract to
        # its native API (or its OpenAI-compatible interface). A future adapter
        # can explicitly opt out with supports_tool_calling = False.
        state.cached_model = None
        state.cached_capabilities = None
        try:
            model_cache = getattr(self.providers, "model_cache", None)
            provider_config_id = getattr(state.candidate, "provider_config_id", None)
            if model_cache is not None and provider_config_id is not None:
                state.cached_model = model_cache.get_model(
                    tenant_id=state.auth.tenant_id,
                    provider_config_id=provider_config_id,
                    model_name=state.candidate.model_name,
                    model_kind="chat",
                )
                state.cached_capabilities = getattr(state.cached_model, "capabilities_json", None)
        except Exception:  # noqa: BLE001 - stale capability metadata must not break chat.
            logger.debug("DeepSpace cached model capabilities could not be read", exc_info=True)
        model_tool_capability = None
        if isinstance(state.cached_capabilities, dict):
            for capability_key in ("supports_tools", "supports_tool_use", "tool_use"):
                capability_value = state.cached_capabilities.get(capability_key)
                if isinstance(capability_value, bool):
                    model_tool_capability = capability_value
                    break
        adapter_supports_tools = bool(getattr(state.provider, "supports_tool_calling", True))
        state.provider_supports_tools = (
            adapter_supports_tools and model_tool_capability is not False
        )
        state.tool_calling_supported = state.provider_supports_tools
        state.native_media_model = self._is_native_media_model(state.candidate.model_name)
        state.attachment_vision_images = []
        state.attachment_vision_statuses = {}
        state.image_attachments = [
            row
            for row in state.attachment_rows
            if str(row.content_type or "").casefold().startswith("image/")
        ]
        if state.image_attachments:
            if self._candidate_supports_image_input(state.candidate, state.auth.tenant_id):
                (
                    state.attachment_vision_images,
                    state.attachment_vision_statuses,
                ) = await self._load_attachment_vision_inputs(
                    state.image_attachments,
                    tenant_id=state.auth.tenant_id,
                )
            else:
                state.attachment_vision_statuses = {
                    str(row.id): "Use extracted text; this model does not support image input."
                    for row in state.image_attachments
                }
        state.web_candidate = None
        state.web_provider = None
        state.web_search_options = {}
        state.resumed_question_context = (
            str(state.resumed_user_question.get("question") or "").strip()
            if state.resumed_user_question
            else ""
        )
        state.resumed_options_context = (
            " ".join(
                str(opt).strip()
                for opt in (state.resumed_user_question.get("options") or [])
                if str(opt).strip()
            )
            if state.resumed_user_question
            else ""
        )
        state.prior_user_message = ""
        if state.resumed_user_question and state.previous:
            for state.msg in reversed(state.previous):  # noqa: B020
                if state.msg.get("role") == "user" and state.msg.get("content"):
                    state.prior_user_message = str(state.msg["content"]).strip()
                    break
        state.effective_routing_prompt = (
            f"{state.prior_user_message} {state.resumed_question_context} {state.resumed_options_context} {state.prompt}".strip()
            if state.resumed_user_question
            else (state.prompt or "")
        )
        state.research_requested = (
            not state.native_media_model
            and bool(getattr(self.settings, "deepspace_research_enabled", True))
            and bool(
                re.search(
                    r"(?:https://\S+|\b(search|look\s*up|find\s+(?:online|on\s+the\s+web|(?:a\s+)?sources?)|latest|current|today|news|verify|fact[ -]?check|research)\b|\b(?:open|read)\s+(?:the\s+)?(?:page|webpage|website|url|link)\b)",
                    state.effective_routing_prompt,
                    re.I,
                )
            )
        )
        state.web_provider, state.web_candidate, state.web_search_options = (
            self.resolve_web_search_state(state.auth)
        )

        # Native image models produce media directly and do not accept function
        # declarations. Other tool-capable models receive the native schemas
        # authorized by the deployment and selected preset.
        state.native_tools_enabled = state.provider_supports_tools and not state.native_media_model
        state.native_tool_names = (
            set(NATIVE_TOOL_REGISTRY.names()) - {"discover_tools"}
            if state.native_tools_enabled
            else set()
        )
        # A tool the deployment switches off stays out of the catalog the
        # model sees, so a disabled capability is never discovered and then
        # refused.
        state.native_tool_names -= _pkg_attr("unavailable_tool_names", unavailable_tool_names)(
            self.settings
        )
        if (
            not bool(getattr(self.settings, "deepspace_research_enabled", True))
            or state.web_candidate is None
            or state.web_provider is None
        ):
            state.native_tool_names.discard("web_search")
        # Plan mode is a per-conversation mode, so it is read here, where
        # the conversation is known, rather than in the per-round tool
        # selection below.
        state.plan_mode_active = False
        state.plan_mode_recorded = None
        state.plan_review_instruction = None
        if (
            state.native_tools_enabled
            and bool(getattr(self.settings, "deepspace_plan_mode_enabled", True))
            and state.conversation_id is not None
        ):
            try:
                if state.plan_mode is not None:
                    self.plan_mode.set_mode(
                        tenant_id=state.auth.tenant_id,
                        user_id=state.auth.user_id,
                        conversation_id=state.conversation_id,
                        active=bool(state.plan_mode),
                    )
                state.plan_mode_recorded = self.plan_mode.recorded_mode(
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                )
                state.plan_mode_active = self.plan_mode.active(
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                )
            except Exception:  # noqa: BLE001
                # A plan-state read must never take down an ordinary turn.
                logger.warning("DeepSpace plan-mode state could not be read", exc_info=True)
                state.plan_mode_active = False
        # Extensions are per-conversation as well, and they carry the
        # guidance, the aliases, and the tool restrictions the turn runs
        # with.
        state.extensions = ExtensionSet()
        if state.native_tools_enabled:
            try:
                state.extensions = extensions_for(
                    self.settings,
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    root=self._resolve_workspace_root(state),
                )
            except Exception:  # noqa: BLE001
                logger.warning("DeepSpace extensions could not be read", exc_info=True)
                state.extensions = ExtensionSet()
        state.native_tool_names -= set(state.extensions.denied_tools)
        state.native_tool_names.update(state.extensions.aliases)
        self.tool_policy.set_extension_rules(
            denied_tools=state.extensions.denied_tools,
            approval_tools=state.extensions.approval_tools,
        )
        # The plugins the conversation runs add their tools, their guidance,
        # and their tool restrictions.  A plugin that is switched off
        # contributes nothing, and the deployment's policy still wins.
        self._hook_conversation_id = str(state.conversation_id or "")
        self._hook_auth = state.auth
        state.plugin_scope = self._plugin_scope(state)
        state.plugins = self._resolve_plugins(state)
        scope = getattr(state, "plugin_scope", None)
        plugin_modes: dict[str, str] = {}
        custom_plugin_modes: dict[str, str] = {}
        plugin_denied: set[str] = set()
        for plugin_id in state.plugins:
            manifest = (
                scope.get(plugin_id) if scope is not None else plugin_registry().get(plugin_id)
            )
            if manifest is None:
                continue
            for tool in manifest.tools:
                plugin_modes[tool.name] = tool.mode
                if manifest.native_tool is None:
                    custom_plugin_modes[tool.name] = tool.mode
            plugin_denied.update(manifest.deny_tools)
        # Every plugin the deployment knows about, so a plugin that is off -
        # for the deployment or for this conversation - takes its tools out of
        # the catalog the model sees.
        known_manifests = (
            list(scope.known()) if scope is not None else list(plugin_registry().known())
        )
        all_plugin_tools = {
            tool.name
            for manifest in known_manifests
            if manifest.native_tool is None
            for tool in manifest.tools
        }
        state.native_tool_names -= all_plugin_tools - set(plugin_modes)
        state.native_tool_names.update(plugin_modes)
        self.tool_policy.set_plugin_rules(
            modes=custom_plugin_modes,  # type: ignore[arg-type]
            denied_tools=plugin_denied,
        )
        # The agent preset says which tools this conversation's agent has,
        # how deep it may delegate, which boundary it runs in, how hard it
        # thinks, and whether it may call the tools from a program.
        if state.preset and self.db is not None and state.conversation_id is not None:
            try:
                from app.deepspace.services.presets import PresetStateService

                PresetStateService(self.db).set_preset(
                    tenant_id=state.auth.tenant_id,
                    user_id=state.auth.user_id,
                    conversation_id=state.conversation_id,
                    preset_id=state.preset,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Unable to apply initial requested preset %r",
                    state.preset,
                    exc_info=True,
                )
        preset_resolution = self._resolve_preset_resolution(state)
        self._apply_agent_mode(state, preset_resolution, plugin_modes)
        try:
            state.mcp_bindings = (
                self.mcp_bridge.tools_for_conversation(
                    auth=state.auth,
                    conversation_id=state.conversation_id,
                )
                if state.provider_supports_tools and not state.native_media_model
                else {}
            )
        except Exception:  # noqa: BLE001
            # MCP discovery must not take down ordinary DeepSpace chat.
            logger.warning(
                "DeepSpace MCP tool discovery failed; continuing without MCP tools",
                exc_info=True,
            )
            state.mcp_bindings = {}
        state.discovered_mcp_bindings = state.mcp_bindings
        self.tool_policy.set_mcp_tool_names(set(state.mcp_bindings))
        if not state.mcp_bindings:
            # The MCP resource tools need a connected server. Without one
            # they would only be refused, so they stay out of the catalog.
            state.native_tool_names -= {
                "list_mcp_resources",
                "list_mcp_resource_templates",
                "read_mcp_resource",
            }
        # The bridge has already scoped bindings to this conversation and user.
        # Keep all authorized candidates in the private broker; do not make
        # connected tools disappear because a prompt regex missed their name.
        state.mcp_bindings = dict(state.discovered_mcp_bindings)
        # A local shell/filesystem request should not be forced through an
        # unrelated connector. Explicit MCP opt-out is also authoritative for
        # this turn; it does not change the saved connection or its policy.
        if excludes_mcp_for_prompt(state.effective_routing_prompt, state.mcp_bindings):
            state.mcp_bindings = {}
            self.tool_policy.set_mcp_tool_names(set())
        # Approval resumes may carry a short UI prompt rather than the
        # original service name. Restore only the exact pending, previously
        # authorized binding from this same tenant/user discovery snapshot.
        if state.resumed_pending is not None:
            state.pending_name = str(state.resumed_pending.get("tool_name") or "")
            state.pending_binding = state.discovered_mcp_bindings.get(state.pending_name)
            if state.pending_binding is not None:
                state.mcp_bindings[state.pending_name] = state.pending_binding
        # The complete connection-scoped MCP catalogue remains server-side.
        # Native schemas are sent directly after the deployment and preset
        # filters have been applied above.
        state.mcp_tools = self.mcp_broker.compact_definitions() if state.mcp_bindings else []
        schema_policy_version = hashlib.sha256(
            json.dumps(
                {
                    "version": "native-schema-policy-v1",
                    "preset": state.preset,
                    "sandbox_mode": state.sandbox_mode,
                    "plan_mode": bool(state.plan_mode_active),
                    "allowed_names": sorted(state.native_tool_names),
                    "plugin_modes": sorted(
                        (str(name), str(mode)) for name, mode in plugin_modes.items()
                    ),
                    "extension_denials": sorted(state.extensions.denied_tools),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        state.available_tools = [
            *(
                self._direct_native_tool_definitions(
                    state.native_tool_names,
                    policy_version=schema_policy_version,
                )
                if state.native_tools_enabled
                else []
            ),
            *state.mcp_tools,
        ]
        available_web_providers = sorted(state.web_search_options)
        if available_web_providers:
            for definition in state.available_tools:
                function = definition.get("function") if isinstance(definition, dict) else None
                parameters = function.get("parameters") if isinstance(function, dict) else None
                properties = parameters.get("properties") if isinstance(parameters, dict) else None
                if (
                    not isinstance(function, dict)
                    or function.get("name") != "web_search"
                    or not isinstance(properties, dict)
                ):
                    continue
                provider_property = properties.get("provider")
                if isinstance(provider_property, dict):
                    provider_property["enum"] = available_web_providers
                    if len(available_web_providers) == 1:
                        provider_property["description"] = (
                            f"The only configured provider for this turn is {available_web_providers[0]}."
                        )
        state.tool_schema_names = sorted(
            {
                str((tool.get("function") or {}).get("name") or "")
                for tool in state.available_tools
                if isinstance(tool, dict)
                and isinstance(tool.get("function"), dict)
                and (tool.get("function") or {}).get("name")
            }
        )
        # Keep compatibility state clear: native schemas are already supplied
        # on this provider round and no discovery hand-off is needed.
        # An extension's alias is published directly, provided the selected
        # preset and deployment still authorize that alias.
        for alias in sorted((state.extensions.aliases or {}).values(), key=lambda item: item.name):
            if alias.name not in state.native_tool_names or not NATIVE_TOOL_REGISTRY.contains(
                alias.target
            ):
                continue
            definition = alias_definition(alias, NATIVE_TOOL_REGISTRY.emit(alias.target))
            if definition:
                state.available_tools.append(definition)
        state.tool_schema_names = sorted(self._tool_names(state.available_tools))
        state.connected_service_tool_required = self._requires_connected_service_tool(
            state.effective_routing_prompt, state.mcp_bindings
        )
        state.initial_task_check = self.task_store.check_tasks(
            tenant_id=state.auth.tenant_id,
            user_id=state.auth.user_id,
            conversation_id=state.conversation_id,
        )
        # A task ledger is entered only when a previous real todo_write exists.
        # The model decides whether a new request merits planning or direct work.
        state.defer_task_for_greeting = (
            self._is_non_work_greeting(state.prompt) and not state.attachments
        )
        state.tool_profile = "dynamic" if state.native_tools_enabled else "direct"
        if state.resumed_user_question is not None:
            # The active ask_user call has a durable answer above.  Do not
            # expose that same pause tool in its immediate continuation: some
            # smaller/local models otherwise repeat the just-resolved question
            # instead of selecting the next real action (for example search).
            state.available_tools = [
                tool
                for tool in state.available_tools
                if str((tool.get("function") or {}).get("name") or "")
                not in CLARIFICATION_TOOL_NAMES
            ]
        state.resume_saved_task = self._should_resume_task_plan(
            state.prompt, state.initial_task_check
        )
        state.allow_existing_task_state = (
            state.resume_saved_task or self._allows_existing_task_state(state.prompt)
        )
        state.managed_task_run = (
            state.provider_supports_tools
            and not state.native_media_model
            and state.resume_saved_task
        )
        state.task_lifecycle_stage, state.active_task_id = self._task_lifecycle_stage(
            state.initial_task_check
        )
        if state.managed_task_run:
            # Resumed work gets fresh state inspection when actionable work remains.
            # If the task ledger is in a terminal or blocked state (no ready actionable
            # task remains), preserve the computed stage so the assistant can directly
            # communicate with the user, report blockers, or provide interactive guidance
            # rather than looping indefinitely in "read_plan".
            if state.task_lifecycle_stage not in {"final", "verify_final"}:
                state.task_lifecycle_stage = "read_plan"
        state.task_has_work_evidence = False
        state.task_lifecycle_prompt_retries = 0
        state.connected_tool_recovery_retries = 0
        state.connected_service_action_completed = False

        state.conversation_messages = [
            {
                "role": "system",
                "content": (
                    DEEPSPACE_COMPACT_POLICY
                    if state.tool_profile == "direct"
                    else DEEPSPACE_AGENT_POLICY
                ),
            },
            *state.previous,
        ]
        # Goal system prompt (injected when goal tools are in scope)
        goal_tool_names = {"goal_read", "goal_set"}
        has_goal_tools = any(
            str((tool.get("function") or {}).get("name") or "") in goal_tool_names
            for tool in state.available_tools
            if isinstance(tool, dict)
        )
        if has_goal_tools:
            threshold = blocked_threshold_from_settings(self.settings)
            goal_policy = goal_system_prompt(threshold)
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="goal_policy",
                content=goal_policy,
            )
        # Workspace instructions (AGENTS.md/CLAUDE.md hierarchy)
        instruction_msg = await prepare_instruction_message(
            self.settings,
            state.auth,
            max_bytes=getattr(self.settings, "deepspace_workspace_instructions_max_bytes", 32_000),
        )
        if instruction_msg is not None:
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="agent_instructions",
                content=instruction_msg["content"],
            )
        # Available skills (rebuilt every turn from the live registry)
        skill_catalog_msg = prepare_skill_catalog_message(self.settings, state.auth)
        if skill_catalog_msg is not None:
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="skill_catalog",
                content=skill_catalog_msg["content"],
            )

        # Per-step time context
        if state.time_context_service is None:
            state.time_context_service = self.time_context_service
        browser_tz_data = getattr(state, "browser_timezone", None)
        browser_context = parse_browser_time_zone(browser_tz_data) if browser_tz_data else None
        previous_event_time = None
        if state.previous:
            for msg in reversed(state.previous):
                if msg.get("role") in ("user", "assistant"):
                    timestamp = _epoch_seconds(msg.get("timestamp"))
                    if timestamp is not None:
                        previous_event_time = timestamp
                        break
        time_injection = state.time_context_service.prepare_injection(
            conversation_id=state.conversation_id,
            turn=state.round_index or 0,
            step=1,
            previous_event_time=previous_event_time,
            settings=self.settings,
            browser_context=browser_context,
        )
        if time_injection is not None:
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="time_context",
                content=time_injection,
            )
        if state.extensions.guidance:
            # An extension's guidance is an additional instruction, never a
            # replacement for the product's own policy.
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="extensions",
                content=(
                    "The following guidance comes from extensions installed for this "
                    "conversation. Follow it unless it conflicts with the "
                    "instructions above.\n\n" + state.extensions.guidance
                ),
            )
        workflow_guidance = self._workflow_guidance(state)
        if workflow_guidance is not None:
            # The workflow tool's own description carries the hook contract.
            # This section says when to reach for it, which the description
            # cannot say, so the model does not guess a script's shape.
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="workflow_guidance",
                content=workflow_guidance,
            )
        if state.sandbox_mode:
            # The model knows the boundary it runs in, so it can plan inside
            # it instead of discovering it by being refused.
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="sandbox_policy",
                content=self._sandbox_policy_instruction(state),
            )
        plugin_instructions = plugin_guidance(state.plugins, scope=scope)
        if plugin_instructions:
            # The plugins the conversation runs add their own instructions.
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="plugins",
                content=plugin_instructions,
            )
        persona_section = deployment_persona_section(self.settings)
        if persona_section is not None:
            # The deployment states who this agent is for. It runs after the
            # product policy and the plugins, and it never outranks them.
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="deployment_persona",
                content=persona_section,
            )
        if state.attachments:
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="current_turn_attachments",
                content=self._attachment_system_instruction(
                    state.attachments,
                    state.attachment_vision_statuses,
                ),
            )
            state.unavailable_ocr_context, state._ = self._attachment_ocr_fallback_instruction(
                state.attachment_rows,
                state.attachment_vision_statuses,
            )
            if state.unavailable_ocr_context:
                self._set_dynamic_system_instruction(
                    state.conversation_messages,
                    key="current_turn_image_ocr",
                    content=state.unavailable_ocr_context,
                )
        if state.history_compacted:
            state.conversation_messages.insert(
                1,
                {
                    "role": "system",
                    "__deepspace_dynamic_key": "history_compaction",
                    "content": (
                        "Older conversation turns were omitted from this provider request to stay within "
                        "the selected model's verified context window. The full transcript remains persisted; "
                        "use the available memory tools when older durable context is needed."
                    ),
                },
            )
        state.conversation_messages.insert(
            1,
            {
                "role": "system",
                "content": self._current_turn_memory_boundary(),
            },
        )
        if state.resumed_failed_run is not None:
            # Rebuild only successful tool results from the durable runtime
            # steps. This lets the provider continue after the last safe
            # checkpoint without repeating already-completed side effects.
            state.replay_groups = {}
            for state.replay_step in self.runtime.replayable_tool_results(  # noqa: B020
                run_id=state.resumed_failed_run.id
            ):
                state.replay_turn = state.replay_step.get("turn_index")
                state.replay_key = (
                    f"turn:{state.replay_turn}"
                    if isinstance(state.replay_turn, int)
                    else f"legacy:{state.replay_step['tool_call_id']}"
                )
                state.group = state.replay_groups.setdefault(
                    state.replay_key,
                    {"turn_index": state.replay_turn, "calls": [], "results": []},
                )
                state.group["calls"].append(
                    {
                        "id": str(state.replay_step["tool_call_id"]),
                        "type": "function",
                        "function": {
                            "name": str(state.replay_step["tool_name"]),
                            "arguments": json.dumps(
                                state.replay_step["arguments"],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                )
                state.group["results"].append(state.replay_step)
            for state.group in state.replay_groups.values():  # noqa: B020
                state.replay_assistant_tool_call = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": state.group["calls"],
                }
                if isinstance(state.group["turn_index"], int):
                    state.replay_assistant_tool_call[
                        "__deepspace_provider_continuation_turn_index"
                    ] = state.group["turn_index"]
                state.conversation_messages.append(state.replay_assistant_tool_call)
                for state.replay_step in state.group["results"]:  # noqa: B020
                    state.conversation_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(state.replay_step["tool_call_id"]),
                            "content": str(state.replay_step["output"]),
                        }
                    )
            state.failed_checkpoint = (
                state.resumed_failed_run.checkpoint
                if isinstance(state.resumed_failed_run.checkpoint, dict)
                else {}
            )
            state.failed_tool_name = str(
                state.failed_checkpoint.get("tool_name") or "the last tool"
            )
            state.validation_message = str(
                state.failed_checkpoint.get("validation_message") or ""
            ).strip()
            state.validation_context = (
                f" The prior attempt failed validation: {state.validation_message[:1_500]}"
                if state.validation_message
                else ""
            )
            state.conversation_messages.append(
                {
                    "role": "system",
                    "__deepspace_dynamic_key": "failed_checkpoint_resume",
                    "content": (
                        "The previous DeepSpace attempt failed after a durable checkpoint. "
                        "Continue the same user task from the saved successful tool results. "
                        f"The last failed operation was {state.failed_tool_name!r}; retry it or choose "
                        "the safest next action as appropriate. Do not repeat successful side effects."
                        f"{state.validation_context} Correct that argument shape before calling the tool again."
                    ),
                }
            )
        if state.mcp_bindings:
            state.attached_services = ", ".join(
                sorted({binding.server.name for binding in state.mcp_bindings.values()})
            )
            # Keep the core policy byte-for-byte stable. Providers that offer
            # prompt-prefix caching can then reuse it; service attachment is
            # dynamic request context and must not invalidate that prefix.
            state.conversation_messages.insert(
                1,
                {
                    "role": "system",
                    "__deepspace_dynamic_key": "mcp_attachment",
                    "content": (
                        f"Attached MCP service connection(s): {state.attached_services}. When the user explicitly "
                        "requests one of these services, call its provided MCP tool; do not claim that the "
                        "connection is unavailable."
                    ),
                },
            )
        if state.defer_task_for_greeting:
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="greeting_guard",
                content=(
                    "The user sent a simple social greeting. Reply naturally without tools. "
                    "Do not resume, complete, alter, or discuss any saved task unless the user asks."
                ),
            )
        if state.resumed_user_question is not None:
            state.pending_call_id = str(state.resumed_user_question.get("call_id") or "")
            state.pending_question = str(
                state.resumed_user_question.get("question")
                or "Please provide the requested information."
            )
            state.pending_options = state.resumed_user_question.get("options")
            state.pending_tool_input = state.resumed_user_question.get("tool_input")
            if not isinstance(state.pending_tool_input, dict):
                state.pending_tool_input = {
                    "question": state.pending_question,
                    "options": (
                        state.pending_options if isinstance(state.pending_options, list) else []
                    ),
                }
            if state.pending_call_id:
                state.pending_assistant_tool_call = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": state.pending_call_id,
                            "type": "function",
                            "function": {
                                "name": (
                                    "ask_user_question"
                                    if "ask_user_question" in (state.native_tool_names or set())
                                    else "ask_user"
                                ),
                                "arguments": json.dumps(
                                    state.pending_tool_input,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            },
                        }
                    ],
                }
                state.question_turn_index = state.resumed_user_question.get("turn_index")
                if isinstance(state.question_turn_index, int):
                    state.pending_assistant_tool_call[
                        "__deepspace_provider_continuation_turn_index"
                    ] = state.question_turn_index
                state.conversation_messages.extend(
                    [
                        state.pending_assistant_tool_call,
                        {
                            "role": "tool",
                            "tool_call_id": state.pending_call_id,
                            "content": json.dumps(
                                {
                                    # This is the completed result of the
                                    # original ask_user call.  Replaying it as
                                    # ``awaiting_user: true`` makes providers
                                    # reasonably ask the same question again.
                                    "awaiting_user": False,
                                    "status": "answered",
                                    "question": state.pending_question,
                                    "answer": state.prompt,
                                    "options": (
                                        state.pending_options
                                        if isinstance(state.pending_options, list)
                                        else []
                                    ),
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                        {"role": "user", "content": state.prompt},
                        {
                            "role": "system",
                            "content": (
                                "The clarification above has been answered by the user. "
                                "Do not ask that same question again. Continue the existing "
                                "task using the answer supplied in the tool result."
                            ),
                        },
                    ]
                )
            else:
                state.conversation_messages.append({"role": "user", "content": state.prompt})
        elif (
            not state.resume_approval_id
            and not state.resume_from_request_id
            and not state.retry_from_saved_prompt
        ):
            state.conversation_messages.append({"role": "user", "content": state.prompt})
        # Record message time for time context
        if hasattr(state, "time_context_service"):
            state.time_context_service.record_message_time(state.conversation_id, time.time())
        state.answer_parts = []
        state.thinking_parts = []
        state.generated_artifacts = []
        state.citations = []
        state.native_research_stats = {
            "searches": 0,
            "results": 0,
            "pages": 0,
            "blocked_pages": 0,
            "images": 0,
        }
        state.used_memories = []
        state.memory_written_this_turn = False
        state.forced_answer = None
        if state.tool_call_required and not state.available_tools:
            state.terminal_status = "blocked"
            if not state.tool_calling_supported:
                state.forced_answer = (
                    "I could not perform that live check because the selected chat model does not "
                    "support structured tool calls. No check was performed. Choose a tool-capable "
                    "chat model and try again."
                )
            else:
                state.forced_answer = (
                    "I could not perform that live check because the selected preset or deployment "
                    "policy supplied no tools for this request. No check was performed. Choose a "
                    "preset that permits the needed tool and try again."
                )
        if state.tool_call_required:
            self._set_dynamic_system_instruction(
                state.conversation_messages,
                key="required_live_tool_call",
                content=(
                    "The user requested an operation that needs current system, workspace, or "
                    "external data. Use a supplied structured tool and base the answer on its "
                    "result. Never say you checked, found, ran, or verified something unless a "
                    "tool result supports it. If no supplied tool can do the task, say that no "
                    "check was performed and explain the limitation."
                ),
            )
        # A freshness request without fetched evidence must not be delegated to
        # the model.  Otherwise a fluent model can stream plausible but stale
        # "latest" news before post-processing can remove its fake citations.
        state.seen_tool_calls = {}
        state.coordination_read_fingerprints = {}
        state.coordination_read_streaks = {}
        state.wait_agent_attempts = 0
        state.wait_agent_started_monotonic = None
        state.wait_agent_limit_notified = False
        # A remote MCP outage can otherwise make a model replay the same call
        # with slightly different arguments forever. Track failures separately
        # from successful call de-duplication so one transient error gets a
        # retry, while a repeated identical outage becomes an actionable stop.
        state.repeated_mcp_failures = {}
        state.mcp_call_count = 0
        state.mcp_discovery_call_count = 0
        state.mcp_result_chars = 0
        state.mcp_budget = {"calls": 0, "discovery": 0, "result_chars": 0}
        state.native_result_chars = 0
        state.pending_images = []
        state.awaiting_user = None
        state.awaiting_approval = None
        state.max_runtime_seconds = max(
            30,
            min(
                int(
                    getattr(
                        self.settings,
                        "deepspace_agent_max_runtime_seconds",
                        DEFAULT_AGENT_TIMEOUT_SECONDS,
                    )
                ),
                24 * 60 * 60,
            ),
        )
        state.loop_deadline = time.monotonic() + state.max_runtime_seconds
        state.round_index = 0
        state.deadline_continuations = 0
        state.empty_provider_retries = 0
        state.clarification_retries = 0
        state.invalid_tool_argument_retries = 0
        state.invalid_tool_error_messages = []
        state.invalid_tool_names = []
        state.terminal_status = "running"
        state.last_context_used_tokens = None
        state.last_context_remaining_tokens = None
        state.last_context_usage = None
        state.last_context_compacted = False
        state.last_reserved_output_tokens = 0
        state.last_prompt_cache_mode = "none"
        state.last_prompt_cache_eligible = False
        state.last_context_epoch = {"epoch": 1, "updated_reason": "initial"}
        state.last_token_categories = {
            "system_context_tokens": 0,
            "tool_schema_tokens": 0,
            "tool_result_tokens": 0,
        }
        state.last_provider_usage = {"usage_source": "unavailable"}
        # Provider usage is reported per model request.  A DeepSpace turn can
        # contain several requests when tools or recovery steps run, so keep
        # the scope explicit instead of presenting the last request as the
        # total turn usage.
        state.provider_usage_rounds_seen = 0
        state.provider_turn_usage_seen = False
        state.provider_turn_usage_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
        }
        state.provider_turn_usage_rounds = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
        }

        def provider_turn_usage_snapshot() -> dict[str, Any]:
            if not state.provider_turn_usage_seen:
                return {"usage_source": "unavailable"}
            snapshot: dict[str, Any] = {
                usage_key: (
                    state.provider_turn_usage_totals[usage_key]
                    if state.provider_turn_usage_rounds[usage_key]
                    == state.provider_usage_rounds_seen
                    else None
                )
                for usage_key in state.provider_turn_usage_totals
            }
            snapshot["usage_source"] = "provider"
            return snapshot

        state.provider_turn_usage_snapshot = provider_turn_usage_snapshot

        state.provider_output_tokens_total = 0
        state.provider_output_usage_rounds = 0
        state.provider_output_usage_complete = True
        state.provider_generation_duration_ms = 0.0
        state.provider_generation_timing_complete = True
        state.provider_generation_buffered = False
        if state.conversation_id is None:
            raise RuntimeError("DeepSpace requires a conversation before building context.")
        state.session_input_tokens, state.session_output_tokens = self._conversation_session_usage(
            auth=state.auth,
            conversation_id=state.conversation_id,
            exclude_message_id=state.assistant_message.id,
        )
        state.request_input_tokens = 0
        state.request_output_tokens = 0
        state.user_visible_input_tokens = self._estimate_visible_tokens(
            [{"role": "user", "content": state.prompt}]
        )
        state.visible_conversation_tokens = self._estimate_visible_tokens(
            [
                message
                for message in state.visible_history
                if message.get("role") in {"user", "assistant"}
            ]
            + [{"role": "user", "content": state.prompt}]
        )
        resumed_mcp_question = (
            isinstance(state.resumed_user_question, dict)
            and state.resumed_user_question.get("kind") == "mcp_connection"
        )
        if (
            not has_matching_connected_mcp(state.prompt, state.mcp_bindings)
            and not resumed_mcp_question
        ):
            options = missing_stdio_mcp_options(
                self.db,
                auth=state.auth,
                prompt=state.prompt,
                connected=False,
            )
            if options:
                question = (
                    "This task needs a local MCP connection. Choose a server to connect, "
                    "or cancel and continue without MCP. Connecting starts the command shown "
                    "below; it does not approve any later MCP action."
                )
                question_id = str(uuid.uuid4())
                call_id = str(uuid.uuid4())
                step_id = str(uuid.uuid4())
                state.awaiting_user = {
                    "kind": "mcp_connection",
                    "question": question,
                    "options": [],
                    "question_id": question_id,
                    "call_id": call_id,
                    "step_id": step_id,
                    "tool_name": "ask_user",
                    "tool_input": {"question": question, "options": []},
                    "turn_index": state.turn_number,
                    "mcp_connection_options": options,
                }
                yield sse(
                    "ask_user_question",
                    {
                        "kind": "mcp_connection",
                        "tool_name": "ask_user",
                        "tool_id": call_id,
                        "step_id": step_id,
                        "message": question,
                        "options": [],
                        "question_id": question_id,
                        "turn_index": state.turn_number,
                        "mcp_connection_options": options,
                    },
                )
            elif is_explicit_mcp_action_request(state.prompt):
                state.forced_answer = (
                    "I could not use MCP for this task because no matching, ready MCP connection "
                    "is available. No MCP action was performed. Connect or enable the requested "
                    "provider in MCP settings, then ask again."
                )
                state.terminal_status = "blocked"
