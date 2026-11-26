"""Events mixin for DeepSpaceChatService."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.auth.dependencies import AuthContext
from app.deepspace.models.request_metric import DeepSpaceRequestMetric
from app.deepspace.services.chat_service.utils import (
    _pkg_attr,
    _TurnState,
    sse,
)
from app.deepspace.services.deliverables import record_turn_changes
from app.deepspace.services.hook_gate import (
    HookParticipant,
    hook_audit_recorder,
)
from app.deepspace.services.hooks import hook_registry
from app.deepspace.services.runtime_invariants import (
    InvariantContext,
)
from app.deepspace.services.session_events import (
    SessionEventStore,
)
from app.deepspace.services.session_telemetry import SpanHandle
from app.deepspace.services.workspace_snapshot import (
    changed_file_entries,
    collect_snapshot_files,
    collect_workspace_stat_entries,
)
from app.realtime.event_bus import publish_event_sync
from app.system.services.cache_service import get_redis_client

logger = logging.getLogger(__name__)


class _EventsMixin:
    def _turn_source(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        client_request_id: str | None,
    ) -> dict[str, Any]:
        """Return the durable source marker carried by a continued turn."""
        if not client_request_id:
            return {}
        try:
            return self.chat.get_user_source_by_request_id(
                tenant_id=auth.tenant_id,
                conversation_id=conversation_id,
                user_id=auth.user_id,
                request_id=client_request_id,
            )
        except Exception:  # noqa: BLE001 - marker lookup must not fail a turn
            logger.warning("DeepSpace turn source lookup failed", exc_info=True)
            return {}

    def _hook_participant(self) -> HookParticipant:
        """Build the hook participant the tool pipeline asks.

        The participant writes the audit trail, accepts the Claude Code and
        Codex dialect when the deployment allows it, and serves the
        conversation the current turn belongs to.

        Returns:
            The participant.
        """
        settings = getattr(self, "settings", None)
        return HookParticipant(
            audit_factory=lambda: self._hook_audit(),
            claude_dialect=bool(getattr(settings, "deepspace_hook_claude_dialect", True)),
            conversation_id=lambda: str(getattr(self, "_hook_conversation_id", "") or ""),
        )

    def _hook_audit(self) -> Any:
        """Build the audit recorder for the current turn.

        Returns:
            The recorder, or None when the deployment turned the audit off
            or the turn has no conversation.
        """
        settings = getattr(self, "settings", None)
        if settings is None or self.db is None:
            return None
        if not bool(getattr(settings, "deepspace_hook_audit_enabled", True)):
            return None
        conversation_id = str(getattr(self, "_hook_conversation_id", "") or "")
        if not conversation_id:
            return None
        auth = getattr(self, "_hook_auth", None)
        if auth is None:
            return None
        return hook_audit_recorder(
            settings,
            _pkg_attr("SessionEventStore", SessionEventStore)(self.db),
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=uuid.UUID(conversation_id),
        )

    def _load_deployment_hooks(self) -> None:
        """Read the deployment's hook manifest.

        A manifest that cannot be read leaves the hooks empty, so a
        deployment without hooks behaves exactly as it did before.
        """
        manifest = str(getattr(self.settings, "deepspace_hook_manifest", "") or "").strip()
        if not manifest:
            return

        path = Path(manifest).expanduser()
        try:
            if path.is_dir():
                hook_registry().load_directory(path)
            else:
                hook_registry().load_file(path)
        except Exception:  # noqa: BLE001
            logger.warning("The deployment's hook manifest could not be read", exc_info=True)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _record_model_message_step(
        self,
        *,
        run_id: uuid.UUID | None,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        text: str,
        turn_index: int,
    ) -> None:
        """Persist model narration before a pause can rebuild the turn.

        Model messages are user-visible activity, not the final answer. They
        still belong to the durable turn timeline so an ask_user pause,
        resume, reload, or cancellation cannot discard them.
        """

        if run_id is None or not text.strip():
            return
        self.runtime.record_step(
            run_id=run_id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            step_type="model_message",
            status="completed",
            input_json={"turn_index": turn_index},
            result_json={"text": text, "turn_index": turn_index},
        )

    def _record_session_event(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        event_type: str,
        run_id: uuid.UUID | None = None,
        tool_name: str | None = None,
        payload: dict[str, Any] | None = None,
        subagent_id: uuid.UUID | None = None,
        turn: int | None = None,
        step: int | None = None,
    ) -> None:
        """Append one typed event to the conversation's session log.

        The typed log is additive.  A failure to append must never fail the
        turn, so a storage problem is reported and the turn continues.

        Args:
            auth: The authenticated tenant and user.
            conversation_id: The conversation the event belongs to.
            event_type: The event name.
            run_id: The run that produced the event.
            tool_name: The tool the event concerns.
            payload: The event's data.
            subagent_id: The delegated child the event belongs to.
            turn: The turn the event belongs to.
            step: The step the event belongs to.
        """
        store = getattr(self, "session_events", None)
        if store is None:
            return
        database = getattr(self, "db", None)
        try:
            store.append(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                event_type=event_type,
                payload=payload,
                run_id=run_id,
                subagent_id=subagent_id,
                turn=turn,
                step=step,
                tool_name=tool_name,
            )
            if database is not None:
                database.commit()
        except Exception:  # noqa: BLE001
            if database is not None:
                try:
                    database.rollback()
                except Exception:  # noqa: BLE001
                    logger.warning("DeepSpace session event rollback failed", exc_info=True)
            logger.warning(
                "DeepSpace session event append failed; the turn continues",
                exc_info=True,
            )

    def _persist_stream_failure(
        self,
        *,
        assistant_message: Any,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        code: str,
        message: str,
        candidate: Any | None,
        client_request_id: str | None = None,
    ) -> None:
        """Never leave a committed blank assistant message after a failed stream."""
        try:
            self.db.rollback()
            # Rollback expires the in-memory ORM object. Reload the row and
            # retain durable turn metadata so a failed stream cannot erase the
            # request cursor, thinking summary, or already-recorded activity.
            existing = self.chat.get_message_by_conversation(
                tenant_id=auth.tenant_id,
                conversation_id=conversation_id,
                message_id=assistant_message.id,
                user_id=auth.user_id,
            )
            previous_metadata = dict(getattr(existing, "metadata_json", None) or {})
            metadata: dict[str, Any] = {
                **previous_metadata,
                "status": "error",
                "surface": "deepspace",
                "error_code": code,
                "error_message": message,
            }
            if client_request_id:
                metadata["client_request_id"] = client_request_id
            if candidate is not None:
                metadata.update(
                    {
                        "provider_type": str(getattr(candidate, "provider_type", "") or ""),
                        "model_name": str(getattr(candidate, "model_name", "") or ""),
                    }
                )
            self.chat.complete_assistant_message(
                tenant_id=auth.tenant_id,
                conversation_id=conversation_id,
                message_id=assistant_message.id,
                user_id=auth.user_id,
                content=message,
                metadata_json=metadata,
            )
            self.db.commit()
        except Exception:  # noqa: BLE001
            self.db.rollback()
            logger.exception("Failed to persist DeepSpace stream failure")

    def _record_request_metric(
        self,
        *,
        auth: AuthContext,
        conversation_id: uuid.UUID,
        candidate: Any,
        outcome: str,
        started_monotonic: float,
        first_token_monotonic: float | None = None,
        tool_profile: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Persist operational facts only; prompts, answers, tokens, and secrets stay out."""
        try:
            self.request_metrics.record(
                DeepSpaceRequestMetric(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    provider_config_id=getattr(candidate, "provider_config_id", None),
                    provider_type=str(getattr(candidate, "provider_type", "unknown")),
                    model_name=str(getattr(candidate, "model_name", "unknown")),
                    outcome=outcome,
                    total_latency_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
                    time_to_first_token_ms=(
                        max(0, int((first_token_monotonic - started_monotonic) * 1000))
                        if first_token_monotonic is not None
                        else None
                    ),
                    tool_profile=tool_profile,
                    error_code=error_code,
                    metadata_json={"schema_version": 1},
                )
            )
            self.db.commit()
            # Publish only after the metric row is committed. Consumers use
            # this as an invalidation signal and re-read the authorized REST
            # snapshot; no metric values or sensitive request data are emitted.
            for resource in ("deepspace", "metrics"):
                try:
                    publish_event_sync(
                        get_redis_client(),
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        event_type="deepspace.metric.recorded",
                        resource=resource,
                        data={"conversation_id": str(conversation_id), "outcome": outcome},
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("DeepSpace realtime metric notification failed", exc_info=True)
        except Exception:  # noqa: BLE001
            self.db.rollback()
            logger.warning("DeepSpace latency metric persistence failed", exc_info=True)

    def _record_provider_failure(self, *, auth: AuthContext, candidate: Any) -> None:
        self.provider_circuit.record_failure(
            tenant_id=auth.tenant_id,
            provider_config_id=getattr(candidate, "provider_config_id", None),
            model_name=str(getattr(candidate, "model_name", "unknown")),
            threshold=int(getattr(self.settings, "provider_circuit_breaker_threshold", 3)),
            cooldown_seconds=int(
                getattr(self.settings, "provider_circuit_breaker_reset_seconds", 30)
            ),
        )

    def _collect_turn_workspace_files(self, state: _TurnState) -> list[dict[str, Any]]:
        """Return workspace files created, modified, or presented during this turn."""
        root = getattr(state, "workspace_root", None)
        if root is None:
            return []
        before_stats = getattr(state, "workspace_stat_before", None)
        if before_stats is None:
            return []
        presented_paths: list[str] = []
        touched_paths: list[str] = []
        file_tool_names = {
            "write",
            "edit",
            "write_file",
            "edit_file",
            "fs_write",
            "fs_edit",
            "fs_str_replace",
            "create_file",
            "write_document",
            "append_file",
            "save_file",
        }
        for step in getattr(state, "durable_steps", None) or []:
            if not isinstance(step, dict):
                continue
            if step.get("status") == "failed" or step.get("success") is False:
                continue
            tool_name = str(step.get("toolName") or step.get("tool_name") or "").strip().lower()
            tool_input = step.get("toolInput") or step.get("tool_input")
            if isinstance(tool_input, dict):
                if tool_name == "present":
                    raw_paths = tool_input.get("paths")
                    if isinstance(raw_paths, list):
                        presented_paths.extend(str(p) for p in raw_paths if p)
                elif tool_name in file_tool_names:
                    for key in ("path", "file_path", "target_file", "output_path", "destination"):
                        raw_path = tool_input.get(key)
                        if raw_path:
                            touched_paths.append(str(raw_path))
                    for list_key in ("paths", "output_paths", "files"):
                        raw_list = tool_input.get(list_key)
                        if isinstance(raw_list, list):
                            touched_paths.extend(str(item) for item in raw_list if item)

            tool_output = step.get("toolOutput") or step.get("tool_output")
            if isinstance(tool_output, str) and tool_output.lstrip().startswith("{"):
                try:
                    import json

                    parsed_out = json.loads(tool_output)
                except ValueError:
                    parsed_out = None
                if isinstance(parsed_out, dict):
                    for key in ("path", "file_path", "output_path"):
                        val = parsed_out.get(key)
                        if isinstance(val, str) and val.strip():
                            touched_paths.append(val)
                    for list_key in ("paths", "files", "created_files", "workspace_files"):
                        val = parsed_out.get(list_key)
                        if isinstance(val, list):
                            for item in val:
                                if isinstance(item, str) and item.strip():
                                    touched_paths.append(item)
                                elif isinstance(item, dict) and isinstance(item.get("path"), str):
                                    touched_paths.append(item["path"])
        try:
            root_path = Path(root)
            return changed_file_entries(
                before_stats,
                collect_workspace_stat_entries(root_path),
                root=root_path,
                presented_paths=presented_paths,
                touched_paths=touched_paths,
            )
        except Exception:  # noqa: BLE001
            logger.debug("Turn workspace files could not be collected.", exc_info=True)
            return []

    def _record_turn_changes(self, state: _TurnState) -> None:
        """Record the files this turn changed.

        The record is per turn, so the agent can report what it changed in
        this turn rather than what the workspace looks like now.

        Args:
            state: The turn's state.
        """

        before = list(getattr(state, "workspace_files_before", []) or [])
        root = getattr(state, "workspace_root", None)
        if not before or root is None:
            return
        try:
            record_turn_changes(
                conversation_id=state.conversation_id,
                turn=int(getattr(state, "round_index", 0) or 0),
                before=before,
                after=collect_snapshot_files(Path(root)),
                root=Path(root),
            )
        except Exception:  # noqa: BLE001
            logger.debug("The turn's changed files could not be recorded.", exc_info=True)

    async def _maybe_update_session_title(self, state: _TurnState) -> None:
        """Update session title if needed.

        Called after each turn ends to generate/update the conversation title.
        """
        if not getattr(self, "session_title_service", None):
            return
        if not getattr(self.settings, "deepspace_session_title_enabled", True):
            return

        try:
            from sqlalchemy import select

            from app.deepspace.models.message import Message
            from app.deepspace.models.session_title import DeepSpaceSessionTitle

            db = self.db
            if db is None:
                return

            # Get conversation messages for title generation
            messages = (
                db.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == state.conversation_id,
                        Message.role.in_(["user", "assistant"]),
                    )
                    .order_by(Message.created_at.asc())
                )
                .scalars()
                .all()
            )

            message_list = [
                {
                    "role": m.role,
                    "content": m.active_version.content if m.active_version else m.content,
                }
                for m in messages
            ]

            # Get current turn number (approximate from round_index)
            turn_number = int(getattr(state, "round_index", 1) or 1)

            new_title = await self.session_title_service.maybe_update_title(
                conversation_id=state.conversation_id,
                messages=message_list,
                turn_number=turn_number,
            )

            if new_title:
                # Update or create title record
                existing = db.execute(
                    select(DeepSpaceSessionTitle).where(
                        DeepSpaceSessionTitle.conversation_id == state.conversation_id
                    )
                ).scalar_one_or_none()

                provider_name = getattr(
                    self.settings, "deepspace_session_title_provider", "first-prompt"
                )
                model_name = getattr(self.settings, "deepspace_session_title_model", None)

                if existing:
                    existing.title = new_title
                    existing.provider = provider_name
                    existing.model = model_name
                else:
                    new_record = DeepSpaceSessionTitle(
                        conversation_id=state.conversation_id,
                        title=new_title,
                        provider=provider_name,
                        model=model_name,
                    )
                    db.add(new_record)

                from app.deepspace.models.conversation import Conversation

                conversation = db.get(Conversation, state.conversation_id)
                if conversation is not None:
                    conversation.title = new_title

                db.commit()
                logger.info("Updated session title for %s: %s", state.conversation_id, new_title)

        except Exception:  # noqa: BLE001
            logger.debug("Session title update failed", exc_info=True)

    def _open_telemetry_span(self, state: _TurnState, context: Any) -> SpanHandle:
        """Open a span that the turn closes even when a phase returns early.

        Args:
            state: The turn that owns the span.
            context: An unentered telemetry span context manager.

        Returns:
            The opened handle, which the caller may close early.
        """
        handle = SpanHandle(context)
        if state.telemetry_spans is None:
            state.telemetry_spans = []
        state.telemetry_spans.append(handle)
        return handle

    def _close_telemetry_spans(self, state: _TurnState) -> None:
        """Close every span the turn still owns, innermost first.

        Args:
            state: The turn whose spans should close.
        """
        for handle in reversed(state.telemetry_spans or []):
            handle.close()
        state.telemetry_spans = []

    async def _report_invariant_results(
        self,
        state: _TurnState,
    ) -> AsyncIterator[str]:
        """Check runtime invariants against the turn's measured behavior.

        Args:
            state: The settled turn to validate.

        Yields:
            One ``invariant`` frame per violation; passing checks emit nothing.
        """
        if not bool(getattr(self.settings, "deepspace_invariant_enabled", True)):
            return
        context = InvariantContext(
            tenant_id=state.auth.tenant_id,
            user_id=state.auth.user_id,
            conversation_id=state.conversation_id,
            turn=state.round_index or 0,
            extra={
                "tool_call_count": state.tool_call_count or 0,
                "provider_latency_ms": state.request_latency_ms,
                "conversation_turn": state.round_index or 0,
                "terminal_status": state.terminal_status,
            },
        )
        try:
            results = await self.invariant_registry.check_all(context)
        except Exception:  # noqa: BLE001 - a diagnostic must not fail a completed turn
            logger.warning("Runtime invariant evaluation failed", exc_info=True)
            return
        for result in results:
            if result.passed:
                continue
            yield sse(
                "invariant",
                {
                    "name": result.name,
                    "severity": result.severity,
                    "message": result.message,
                    "details": result.details,
                },
            )
