"""Productivity tool dispatch mixin for DeepSpaceChatService."""

from __future__ import annotations

import base64
import difflib
import itertools
import logging
import uuid
from typing import Any

from app.auth.dependencies import AuthContext
from app.deepspace.memory.memory_service import MemoryService
from app.deepspace.services.chat_service.utils import (
    NATIVE_TOOL_REGISTRY,
    _pkg_attr,
    _relabel_citations,
)
from app.deepspace.services.conversation_retrieval import ConversationRetrievalService
from app.deepspace.services.mcp_bridge import DeepSpaceMCPTool
from app.deepspace.services.mcp_tool_broker import (
    MCP_GET_RESULT,
    MCP_GET_TOOL_SCHEMA,
    MCP_SEARCH_TOOLS,
)
from app.deepspace.services.sandbox_executor import SandboxExecutorError, execute_sandbox
from app.deepspace.services.session_events import (
    TODO_WRITE,
)
from app.deepspace.services.task_loop import (
    normalize_harness_todos,
    summarize_tasks,
    todo_counts,
)
from app.deepspace.services.todo_localization import (
    request_locale,
    todo_summary,
)
from app.documents.repositories.chunks import RetrievedChunkRow
from app.ingestion.services.embedding_service import EmbeddingService
from app.query.services.reranker_service import RerankerService

logger = logging.getLogger(__name__)


class _ProductivityToolsMixin:
    async def _execute_productivity_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        auth: AuthContext,
        conversation_id: uuid.UUID,
        web_provider: Any | None,
        web_candidate: Any | None,
        request: Any | None,
        mcp_binding: DeepSpaceMCPTool | None = None,
        mcp_bindings: dict[str, DeepSpaceMCPTool] | None = None,
        mcp_approval_granted: bool = False,
        assistant_message_id: uuid.UUID | None = None,
        ignore_existing_tasks: bool = False,
        user_prompt: str | None = None,
        native_tool_names: set[str] | None = None,
        web_search_options: dict[str, tuple[Any, Any]] | None = None,
        mcp_budget: dict[str, int] | None = None,
        run_id: uuid.UUID | None = None,
        turn: int | None = None,
        step: int | None = None,
        skill_scope: Any = None,
        progress_callback: Any = None,
        activity_callback: Any = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        source_message_id: str | None = None
        if tool_name == "write" and str(arguments.get("target") or "").strip().lower() == "library":
            source = str(arguments.get("source") or "").strip().lower()
            if source:
                if source == "previous_assistant":
                    source_messages = self.chat.get_messages(
                        tenant_id=auth.tenant_id,
                        conversation_id=conversation_id,
                        user_id=auth.user_id,
                    )
                    source_message = next(
                        (
                            item
                            for item in reversed(source_messages)
                            if item.role == "assistant"
                            and (assistant_message_id is None or item.id != assistant_message_id)
                        ),
                        None,
                    )
                elif source == "message":
                    raw_id = str(arguments.get("source_message_id") or "").strip()
                    try:
                        source_id = uuid.UUID(raw_id)
                    except ValueError as exc:
                        raise ValueError(
                            "write message source requires a valid source_message_id."
                        ) from exc
                    source_message = self.chat.get_message_by_conversation(
                        tenant_id=auth.tenant_id,
                        conversation_id=conversation_id,
                        message_id=source_id,
                        user_id=auth.user_id,
                    )
                else:
                    raise ValueError("write source must be 'previous_assistant' or 'message'.")
                if source_message is None or getattr(source_message, "role", None) != "assistant":
                    raise ValueError("The requested assistant response could not be found.")
                source_message_obj = source_message
                content = str(
                    source_message_obj.active_version.content
                    if getattr(source_message_obj, "active_version", None) is not None
                    else source_message_obj.content
                )
                if not content.strip():
                    raise ValueError("The requested assistant response has no saved content yet.")
                source_message_id = str(source_message_obj.id)
                arguments = dict(arguments)
                arguments["content"] = content
                arguments["operation"] = "create"
                arguments.pop("mode", None)
                arguments.pop("source", None)
                arguments.pop("source_message_id", None)
        if (
            tool_name == "read"
            and str(arguments.get("target") or "").strip().lower() == "library"
            and str(arguments.get("operation") or "read").strip().lower() == "query"
        ):
            return await self._execute_productivity_tool(
                tool_name="document_query",
                arguments={
                    key: arguments[key] for key in ("query", "file_id", "limit") if key in arguments
                },
                auth=auth,
                conversation_id=conversation_id,
                web_provider=web_provider,
                web_candidate=web_candidate,
                request=request,
                assistant_message_id=assistant_message_id,
                user_prompt=user_prompt,
            )
        guard_refusal = self._loop_guard_refusal(
            tool_name=tool_name,
            arguments=arguments,
            conversation_id=conversation_id,
        )
        if guard_refusal is not None:
            return guard_refusal
        native_result = await self._dispatch_package_tool(
            tool_name=tool_name,
            arguments=arguments,
            auth=auth,
            conversation_id=conversation_id,
            web_provider=web_provider,
            web_candidate=web_candidate,
            web_search_options=web_search_options,
            mcp_bindings=mcp_bindings,
            mcp_budget=mcp_budget,
            request=request,
            run_id=run_id,
            skill_scope=skill_scope,
            progress_callback=progress_callback,
            activity_callback=activity_callback,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
        )
        if native_result is not None:
            if source_message_id is not None and isinstance(native_result, dict):
                native_result = {**native_result, "source_message_id": source_message_id}
            return native_result
        if tool_name == "discover_tools":
            return self.native_tool_broker.discover(
                query=str(arguments.get("query") or ""),
                registry=NATIVE_TOOL_REGISTRY,
                allowed_names=native_tool_names or set(),
                tool_names=(
                    [str(name) for name in arguments.get("tool_names", [])]
                    if isinstance(arguments.get("tool_names"), list)
                    else None
                ),
                offset=int(arguments.get("offset") or 0),
            )
        if tool_name == "read" and str(arguments.get("target") or "") == "library":
            operation = str(arguments.get("operation") or "read")
            if operation == "query":
                return await self._execute_productivity_tool(
                    tool_name="document_query",
                    arguments={
                        key: arguments[key]
                        for key in ("query", "file_id", "limit")
                        if key in arguments
                    },
                    auth=auth,
                    conversation_id=conversation_id,
                    web_provider=web_provider,
                    web_candidate=web_candidate,
                    request=request,
                    assistant_message_id=assistant_message_id,
                    user_prompt=user_prompt,
                )
            if arguments.get("file_id") or arguments.get("filename"):
                return await self._execute_productivity_tool(
                    tool_name="document_read",
                    arguments={
                        key: arguments[key]
                        for key in ("file_id", "filename", "max_characters")
                        if key in arguments
                    },
                    auth=auth,
                    conversation_id=conversation_id,
                    web_provider=web_provider,
                    web_candidate=web_candidate,
                    request=request,
                    assistant_message_id=assistant_message_id,
                    user_prompt=user_prompt,
                )
        if tool_name == "analyze" and arguments.get("operation") == "compare_documents":
            return await self._execute_productivity_tool(
                tool_name="document_compare",
                arguments={
                    key: arguments[key]
                    for key in (
                        "file_ids",
                        "left_file_id",
                        "right_file_id",
                        "max_characters",
                    )
                    if key in arguments
                },
                auth=auth,
                conversation_id=conversation_id,
                web_provider=web_provider,
                web_candidate=web_candidate,
                web_search_options=web_search_options,
                mcp_budget=mcp_budget,
                request=request,
                assistant_message_id=assistant_message_id,
                user_prompt=user_prompt,
            )
        if tool_name == "get_tool_result":
            value = self.tool_result_store.get(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                ref=str(arguments.get("result_ref") or ""),
            )
            return (
                value
                if value is not None
                else {
                    "status": "error",
                    "message": "The native tool result reference is missing or expired.",
                }
            )
        if tool_name == MCP_SEARCH_TOOLS:
            return self.mcp_broker.search(
                mcp_bindings or {},
                str(arguments.get("query") or ""),
                cursor=str(arguments.get("cursor") or "") or None,
            )
        if tool_name == MCP_GET_TOOL_SCHEMA:
            return self.mcp_broker.get_schema(
                mcp_bindings or {}, str(arguments.get("tool_ref") or "")
            )
        if tool_name == MCP_GET_RESULT:
            value = self.mcp_result_store.get(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                ref=str(arguments.get("result_ref") or ""),
            )
            return (
                value
                if value is not None
                else {
                    "status": "error",
                    "message": "The MCP result reference is missing or expired.",
                }
            )
        if mcp_binding is not None:
            result = await self.mcp_bridge.execute(
                auth=auth,
                conversation_id=conversation_id,
                binding=mcp_binding,
                arguments=arguments,
                approval_granted=mcp_approval_granted,
            )
            if result.get("is_error") or result.get("status") == "error":
                raise ValueError(str(result.get("message") or "MCP tool execution failed."))
            bounded_result = self.mcp_broker.bound_result(
                {
                    # Use the discovery snapshot; the MCP runtime commits audit
                    # data and may expire the ORM server instance.
                    "mcp_server": mcp_binding.server_name,
                    "mcp_tool": mcp_binding.raw_name,
                    **result,
                }
            )
            return (
                bounded_result if isinstance(bounded_result, dict) else {"result": bounded_result}
            )
        if tool_name == "sandbox_execute":
            try:
                sandbox_files: list[dict[str, Any]] = []
                requested_file_ids = arguments.get("file_ids")
                if requested_file_ids is not None and not isinstance(requested_file_ids, list):
                    raise SandboxExecutorError("file_ids must be an array.")
                for raw_file_id in (requested_file_ids or [])[:5]:
                    file = self.task_store.read_workspace_file_for_sandbox(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        file_id=str(raw_file_id),
                        settings=self.settings,
                    )
                    sandbox_files.append(
                        {
                            "name": file["name"],
                            "content_type": file["content_type"],
                            "data_base64": base64.b64encode(file["payload"]).decode("ascii"),
                            # Existing ingestion/OCR is the authoritative first
                            # pass; Python receives it without re-running OCR.
                            "extracted_text": file["extracted_text"],
                        }
                    )
                return await execute_sandbox(
                    code=str(arguments.get("code") or ""),
                    language=str(arguments.get("language") or ""),
                    settings=self.settings,
                    input_data=(
                        arguments.get("input") if isinstance(arguments.get("input"), dict) else None
                    ),
                    timeout_seconds=int(
                        arguments.get("timeout_seconds")
                        or self.settings.deepspace_sandbox_timeout_seconds
                    ),
                    files=sandbox_files,
                )
            except SandboxExecutorError as exc:
                return {
                    "status": "unavailable",
                    "execution": "isolated_sandbox",
                    "message": str(exc),
                }
        if tool_name == "document_read":
            file_id = str(arguments.get("file_id") or "").strip() or None
            filename = str(arguments.get("filename") or "").strip() or None
            # Models occasionally place an exact filename in ``file_id``
            # after a Library inventory call.  Treat a non-UUID value as the
            # filename lookup it clearly represents; authorization still
            # happens inside read_workspace_file against this conversation.
            if file_id and not filename:
                try:
                    uuid.UUID(file_id)
                except ValueError:
                    filename, file_id = file_id, None
            if not file_id and not filename:
                raise ValueError("document_read requires file_id or filename.")
            result = self.task_store.read_workspace_file(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
                file_id=file_id,
                filename=filename,
            )
            # Keep a direct read bounded enough for the smallest production
            # context windows. Larger documents should use document_query or
            # sandbox file access; returning 200k characters here can make the
            # next provider round fail before it can answer.
            limit = min(32_000, max(100, int(arguments.get("max_characters") or 16_000)))
            text = str(result.get("extracted_text") or result.get("content") or "")
            return {
                "file": {
                    key: result.get(key)
                    for key in ("id", "name", "content_type", "size_bytes", "version")
                },
                "text": text[:limit],
                "truncated": len(text) > limit,
                "extraction_status": result.get("extraction_status"),
                "citation": {"file_id": result.get("id"), "filename": result.get("name")},
            }
        if tool_name == "document_compare":
            raw_file_ids = arguments.get("file_ids")
            if isinstance(raw_file_ids, list):
                file_ids = [str(item or "").strip() for item in raw_file_ids]
            else:
                file_ids = [
                    str(arguments.get("left_file_id") or "").strip(),
                    str(arguments.get("right_file_id") or "").strip(),
                ]
            # Keep caller order while refusing ambiguous duplicate comparisons.
            file_ids = list(dict.fromkeys(file_id for file_id in file_ids if file_id))
            if len(file_ids) < 2 or len(file_ids) > 5:
                raise ValueError("document_compare requires 2 to 5 different file ids.")
            documents = [
                self.task_store.read_workspace_file(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    file_id=file_id,
                )
                for file_id in file_ids
            ]
            limit = min(100_000, max(100, int(arguments.get("max_characters") or 30_000)))
            pairs = list(itertools.combinations(documents, 2))
            comparison_limit = max(100, limit // len(pairs))

            def compare_pair(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
                raw_left = str(left.get("extracted_text") or left.get("content") or "")
                raw_right = str(right.get("extracted_text") or right.get("content") or "")
                left_text = raw_left[:limit]
                right_text = raw_right[:limit]
                left_lines = left_text.splitlines()
                right_lines = right_text.splitlines()
                rendered = "\n".join(difflib.unified_diff(left_lines, right_lines, lineterm=""))
                matcher = difflib.SequenceMatcher(a=left_lines, b=right_lines, autojunk=False)
                equal_blocks = [opcode for opcode in matcher.get_opcodes() if opcode[0] == "equal"]
                unchanged_lines = sum(
                    end_left - start_left for _, start_left, end_left, _, _ in equal_blocks
                )
                samples = []
                for _, start_left, end_left, start_right, _end_right in equal_blocks[:3]:
                    samples.append(
                        {
                            "left_line_start": start_left + 1,
                            "right_line_start": start_right + 1,
                            "line_count": end_left - start_left,
                            "text": "\n".join(
                                left_lines[start_left : min(end_left, start_left + 3)]
                            ),
                        }
                    )
                return {
                    "left": {"id": left.get("id"), "name": left.get("name")},
                    "right": {"id": right.get("id"), "name": right.get("name")},
                    "comparison_method": "exact_line_diff",
                    "source_truncated": len(raw_left) > limit or len(raw_right) > limit,
                    "diff": rendered[:comparison_limit],
                    "truncated": len(rendered) > comparison_limit,
                    "unchanged": {
                        "exact_line_count": unchanged_lines,
                        "matching_blocks": len(equal_blocks),
                        "samples": samples,
                    },
                    "citation": {
                        "left_file_id": left.get("id"),
                        "right_file_id": right.get("id"),
                    },
                }

            comparisons = [compare_pair(left, right) for left, right in pairs]
            if len(comparisons) == 1:
                # Retain the established two-file response shape for existing
                # clients and model prompts, while adding accurate method and
                # unchanged-line metadata.
                return comparisons[0]
            return {
                "comparison_method": "exact_line_diff_pairwise",
                "file_count": len(documents),
                "comparisons": comparisons,
                "citation": {"file_ids": [document.get("id") for document in documents]},
            }
        if tool_name == "document_query":
            query = str(arguments.get("query") or "").strip()
            if not query:
                raise ValueError("document_query requires query.")
            limit = min(20, max(1, int(arguments.get("limit") or 8)))
            file_id = str(arguments.get("file_id") or "").strip() or None
            if file_id:
                candidates = [
                    self.task_store.read_workspace_file(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        file_id=file_id,
                    )
                ]
            else:
                entries = self.task_store.list_workspace_entries(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                )
                candidates = [
                    self.task_store.read_workspace_file(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        file_id=str(item["id"]),
                    )
                    for item in entries.get("files", [])[:50]
                ]
            needle = query.casefold()
            passages: list[dict[str, Any]] = []
            ranked_candidates: list[RetrievedChunkRow] = []
            source_file_ids: dict[uuid.UUID, str] = {}
            # Workspace uploads are intentionally separate from canonical
            # indexed documents (they are conversation-scoped).  Build a
            # bounded hybrid index over their extracted/OCR text for this
            # query, then use the same embedding and reranker services as the
            # canonical RAG path.  This keeps Library authorization unchanged
            # while avoiding a second durable vector store for ephemeral files.
            workspace_chunks: list[tuple[RetrievedChunkRow, str, bool]] = []
            max_chunks = 240
            max_file_chars = 200_000
            for candidate in candidates:
                content = str(candidate.get("content") or candidate.get("extracted_text") or "")
                content = content[:max_file_chars]
                lines = content.splitlines()
                for start in range(0, len(lines), 8):
                    if len(workspace_chunks) >= max_chunks:
                        break
                    passage_text = "\n".join(lines[start : start + 8])[:4000].strip()
                    if not passage_text:
                        continue
                    chunk_id = uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"workspace:{candidate.get('id')}:{start}",
                    )
                    row = RetrievedChunkRow(
                        document_id=uuid.uuid5(
                            uuid.NAMESPACE_URL, f"workspace:{candidate.get('id')}"
                        ),
                        chunk_id=chunk_id,
                        filename=str(candidate.get("name") or "Library file"),
                        content=passage_text,
                        similarity_score=0.0,
                        source_type="workspace",
                        chunk_index=start,
                        page_number=None,
                    )
                    source_file_ids[chunk_id] = str(candidate.get("id") or "")
                    workspace_chunks.append((row, passage_text, needle in passage_text.casefold()))

            embedding_metadata: dict[str, Any] = {"applied": False, "reason": "no_chunks"}
            if workspace_chunks:
                try:
                    embedding_service = _pkg_attr("EmbeddingService", EmbeddingService)(
                        self.settings, self.db
                    )
                    embedding_result = embedding_service.embed_many_with_metadata(
                        [query, *(item[1] for item in workspace_chunks)],
                        tenant_id=auth.tenant_id,
                        actor_user_id=auth.user_id,
                    )
                    query_vector = embedding_result.vectors[0]
                    if embedding_result.metadata.provider == "nvidia-nim":
                        # NIM embedding models that require input_type need a
                        # query vector distinct from the passage vectors.
                        query_vector = embedding_service.embed_many_with_metadata(
                            [query],
                            tenant_id=auth.tenant_id,
                            actor_user_id=auth.user_id,
                            input_type="query",
                        ).vectors[0]
                    query_norm = sum(value * value for value in query_vector) ** 0.5
                    for index, (row, _text, lexical_match) in enumerate(workspace_chunks, start=1):
                        vector = embedding_result.vectors[index]
                        vector_norm = sum(value * value for value in vector) ** 0.5
                        cosine = (
                            sum(
                                left * right
                                for left, right in zip(query_vector, vector, strict=False)
                            )
                            / (query_norm * vector_norm)
                            if query_norm and vector_norm
                            else 0.0
                        )
                        # Exact lexical hits remain a useful precision boost,
                        # while semantic similarity finds paraphrases and OCR
                        # variants that do not contain the query verbatim.
                        row.similarity_score = max(0.0, cosine) + (0.25 if lexical_match else 0.0)
                    embedding_metadata = {
                        "applied": True,
                        "provider": embedding_result.metadata.provider,
                        "model": embedding_result.metadata.model,
                        "fallback_used": embedding_result.metadata.fallback_used,
                        "chunks_considered": len(workspace_chunks),
                    }
                except Exception:  # noqa: BLE001
                    # Provider outages must not turn an authorized Library
                    # read into an error.  Preserve deterministic lexical
                    # retrieval as the documented fallback.
                    for row, _text, lexical_match in workspace_chunks:
                        row.similarity_score = 1.0 if lexical_match else 0.0
                    embedding_metadata = {
                        "applied": False,
                        "reason": "embedding_unavailable",
                        "chunks_considered": len(workspace_chunks),
                    }
            ranked_candidates = [
                row
                for row, _text, _lexical in sorted(
                    workspace_chunks,
                    key=lambda item: item[0].similarity_score,
                    reverse=True,
                )
                if row.similarity_score > 0
            ]
            rerank_metadata: dict[str, Any] = {"applied": False, "reason": "no_matches"}
            if ranked_candidates:
                try:
                    reranked = RerankerService(self.db, self.settings).rerank_chunks(
                        tenant_id=auth.tenant_id,
                        workspace_id=None,
                        actor_user_id=auth.user_id,
                        query=query,
                        chunks=ranked_candidates[: min(20, len(ranked_candidates))],
                        top_n=limit,
                    )
                    ranked_candidates = reranked.chunks
                    rerank_metadata = {
                        "applied": reranked.metadata.applied,
                        "provider": reranked.metadata.provider,
                        "model": reranked.metadata.model,
                        "failure_reason": reranked.metadata.failure_reason,
                    }
                except Exception:  # noqa: BLE001
                    # Workspace files may not have provider configuration; the
                    # deterministic lexical order remains a safe fallback.
                    rerank_metadata = {"applied": False, "reason": "reranker_unavailable"}
            for row in ranked_candidates[:limit]:
                line_number = row.chunk_index + 1
                passages.append(
                    {
                        "file_id": source_file_ids.get(row.chunk_id),
                        "filename": row.filename,
                        "line_start": line_number,
                        "line_end": row.chunk_index + 8,
                        "text": row.content,
                        "citation": f"file:{source_file_ids.get(row.chunk_id)}#L{line_number}",
                    }
                )
            return {
                "query": query,
                "passages": passages,
                "total_matches": len(ranked_candidates),
                "retrieval": {
                    "strategy": "workspace_hybrid",
                    "reranker": rerank_metadata,
                    "embeddings": embedding_metadata,
                },
            }
        if tool_name == "todo_write":
            raw_todos = arguments.get("todos")
            if raw_todos is not None:
                allow_parallel = bool(
                    getattr(
                        self.settings,
                        "deepspace_todo_allow_parallel_in_progress",
                        True,
                    )
                )
                normalized_todos = normalize_harness_todos(
                    raw_todos,
                    allow_parallel_in_progress=allow_parallel,
                )
                counts = todo_counts(normalized_todos)
                self._record_session_event(
                    auth=auth,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    turn=turn,
                    step=step,
                    event_type=TODO_WRITE,
                    tool_name="todo_write",
                    payload={"todos": normalized_todos},
                )
                return {
                    "todos": normalized_todos,
                    "counts": counts,
                    "text": todo_summary(counts, locale=request_locale(request)),
                }
            allow_parallel = bool(
                getattr(
                    self.settings,
                    "deepspace_todo_allow_parallel_in_progress",
                    True,
                )
            )
            operation = str(arguments.get("operation") or "").strip().lower()
            if operation == "replace":
                tasks = arguments.get("tasks")
                if not isinstance(tasks, list):
                    raise ValueError("todo_write(replace) requires a tasks array.")
                task_result = self.task_store.replace_tasks(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    tasks=tasks,
                    allow_parallel_in_progress=allow_parallel,
                )
                return {"tasks": task_result, "summary": summarize_tasks(task_result)}
            if operation == "read":
                tasks = self.task_store.read_tasks(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                )
                return {
                    "tasks": tasks,
                    "summary": summarize_tasks(tasks),
                    "task_check": self.task_store.check_tasks(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                    ),
                }
            if operation == "check":
                return self.task_store.check_tasks(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                )
            if operation == "update":
                task_id = str(arguments.get("task_id") or "").strip()
                status = str(arguments.get("status") or "").strip()
                if not task_id or not status:
                    raise ValueError("todo_write(update) requires task_id and status.")
                task = self.task_store.mark_task(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    status=status,
                    evidence=str(arguments.get("evidence") or "")[:1000],
                    allow_parallel_in_progress=allow_parallel,
                )
                return {
                    "task": task,
                    "summary": "Task status updated.",
                    "task_check": self.task_store.check_tasks(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                    ),
                }
            raise ValueError("todo_write requires operation replace, read, update, or check.")
        if tool_name == "read" and arguments.get("target"):
            target = str(arguments.get("target") or "").strip().lower()
            if target == "workspace":
                workspace_path = arguments.get("path")
                if not isinstance(workspace_path, str) or not workspace_path.strip():
                    return {
                        "status": "invalid_arguments",
                        "error_code": "WORKSPACE_PATH_REQUIRED",
                        "message": (
                            "read(target='workspace') needs a file path. Use fs_glob to find "
                            "the file, then retry read with its workspace-relative path."
                        ),
                    }
                workspace_arguments = {
                    key: arguments[key] for key in ("path", "offset") if key in arguments
                }
                if arguments.get("line_limit") is not None:
                    workspace_arguments["limit"] = arguments["line_limit"]
                return await self._execute_package_tool_call(
                    tool_name="read",
                    arguments=workspace_arguments,
                    auth=auth,
                    conversation_id=conversation_id,
                    web_provider=web_provider,
                    web_candidate=web_candidate,
                    request=request,
                )
            if target == "note":
                return self.task_store.read_note(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                )
            if target == "library":
                if not arguments.get("file_id") and not arguments.get("filename"):
                    return self.task_store.list_workspace_entries(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        parent_folder_id=str(arguments.get("folder_id") or "").strip() or None,
                    )
                try:
                    return self.task_store.read_workspace_file(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        file_id=str(arguments.get("file_id") or "").strip() or None,
                        filename=str(arguments.get("filename") or "").strip() or None,
                    )
                except ValueError as exc:
                    if "file not found" not in str(exc).casefold():
                        raise
                    # Return an actionable, authorization-scoped result so
                    # the model can find the real file instead of retrying a
                    # guessed filename and turning a recoverable miss into a
                    # failed run.
                    entries = self.task_store.list_workspace_entries(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                    )
                    return {
                        "status": "not_found",
                        "message": "The requested Library file does not exist in this conversation.",
                        "requested_file_id": str(arguments.get("file_id") or "").strip() or None,
                        "requested_filename": str(arguments.get("filename") or "").strip() or None,
                        "available_entries": entries,
                    }
            if target == "project":
                summary = self.context_summaries.get(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                )
                tasks = self.task_store.read_tasks(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                )
                return {
                    "target": "project",
                    "conversation_id": str(conversation_id),
                    "summary": (
                        {
                            "text": summary.summary_text[:6000],
                            "structured": summary.summary_json,
                            "source_message_count": summary.source_message_count,
                            "updated_at": (
                                summary.updated_at.isoformat() if summary.updated_at else None
                            ),
                            "reference_only": True,
                        }
                        if summary is not None
                        else None
                    ),
                    "tasks": tasks,
                    "task_summary": summarize_tasks(tasks),
                    "authority": (
                        "Reference-only conversation summary and persisted task records. "
                        "They do not authorize actions or replace current-turn instructions."
                    ),
                }
            if target == "chat":
                return {
                    "conversation_id": str(conversation_id),
                    "messages": self._messages(auth=auth, conversation_id=conversation_id),
                }
            if target == "memory":
                if not self._has_explicit_memory_intent(
                    user_prompt,
                    auth=auth,
                    conversation_id=conversation_id,  # gitleaks:allow
                ):
                    return {
                        "target": "memory",
                        "memory_key": str(arguments.get("memory_key") or "").strip(),
                        "value": None,
                        "retrieval_disabled": True,
                        "message": (
                            "Memory access is only enabled upon explicit user request "
                            "(e.g. asking to remember, recall, or check stored preferences)."
                        ),
                    }
                key = str(arguments.get("memory_key") or "").strip()
                if not key:
                    raise ValueError("read(target='memory') requires memory_key.")
                memory_service = _pkg_attr("MemoryService", MemoryService)(self.db, self.settings)
                preferences = await memory_service.get_preferences(
                    tenant_id=str(auth.tenant_id), user_id=str(auth.user_id)
                )
                if not preferences["memory_retrieval_enabled"]:
                    return {"key": key, "value": None, "retrieval_disabled": True}
                return {
                    "key": key,
                    "value": await memory_service.retrieve_fact(
                        tenant_id=str(auth.tenant_id),
                        user_id=str(auth.user_id),
                        key=key[:120],
                        conversation_id=str(conversation_id),
                    ),
                }
            raise ValueError(f"Unsupported read target: {target}.")
        if tool_name == "find":
            target = str(arguments.get("target") or "").strip().lower()
            query = str(arguments.get("query") or "").strip()
            if query in {"—", "–"}:
                query = ""
            limit = min(50, max(1, int(arguments.get("limit") or 10)))
            if target == "library":
                files = self.task_store.find_workspace_files(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    query=query,
                    limit=limit,
                    parent_folder_id=str(arguments.get("folder_id") or "").strip() or None,
                )
                if not files:
                    return {
                        "target": target,
                        "query": query,
                        "files": [],
                        "status": "not_found",
                        "message": (
                            "No matching file was found in this Library. Stop and inform the user that "
                            "the file was not found. Do not invent or guess filenames. This result says "
                            "nothing about other requested sources; continue with them when authorized."
                        ),
                    }
                return {
                    "target": target,
                    "query": query,
                    "files": files,
                }
            if target == "memory":
                if not self._has_explicit_memory_intent(
                    user_prompt,
                    auth=auth,
                    conversation_id=conversation_id,  # gitleaks:allow
                ):
                    return {
                        "target": target,
                        "query": query,
                        "memories": [],
                        "retrieval_disabled": True,
                        "message": (
                            "Memory access is only enabled upon explicit user request "
                            "(e.g. asking to remember, recall, or check stored preferences)."
                        ),
                    }
                memory_service = _pkg_attr("MemoryService", MemoryService)(self.db, self.settings)
                preferences = await memory_service.get_preferences(
                    tenant_id=str(auth.tenant_id), user_id=str(auth.user_id)
                )
                if not preferences["memory_retrieval_enabled"]:
                    return {
                        "target": target,
                        "query": query,
                        "memories": [],
                        "retrieval_disabled": True,
                    }
                return {
                    "target": target,
                    "query": query,
                    "memories": await memory_service.search_memories(
                        tenant_id=str(auth.tenant_id),
                        user_id=str(auth.user_id),
                        query=query[:1000],
                        limit=min(10, limit),
                        conversation_id=str(conversation_id),
                    ),
                }
            if target == "chat":
                return {
                    "target": target,
                    "query": query,
                    "messages": ConversationRetrievalService(self.db, self.settings).search_chat(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        query=query,
                        limit=limit,
                    ),
                    "retrieval": {
                        "strategy": "bounded_hybrid_chat_index",
                        "reference_only": True,
                    },
                }
            raise ValueError(f"Unsupported find target: {target}.")
        if tool_name == "write" and arguments.get("target"):
            target = str(arguments.get("target") or "").strip().lower()
            content = str(arguments.get("content") or "")
            mode = str(arguments.get("mode") or "replace")
            if target == "note":
                return self.task_store.write_note(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    markdown=content,
                    mode=mode,
                )
            if target == "library":
                source = str(arguments.get("source") or "").strip().lower()
                if source:
                    if source == "previous_assistant":
                        source_messages = self.chat.get_messages(
                            tenant_id=auth.tenant_id,
                            conversation_id=conversation_id,
                            user_id=auth.user_id,
                        )
                        source_message = next(
                            (
                                item
                                for item in reversed(source_messages)
                                if item.role == "assistant"
                                and (
                                    assistant_message_id is None or item.id != assistant_message_id
                                )
                            ),
                            None,
                        )
                    elif source == "message":
                        raw_id = str(arguments.get("source_message_id") or "").strip()
                        try:
                            source_id = uuid.UUID(raw_id)
                        except ValueError as exc:
                            raise ValueError(
                                "write message source requires a valid source_message_id."
                            ) from exc
                        source_message = self.chat.get_message_by_conversation(
                            tenant_id=auth.tenant_id,
                            conversation_id=conversation_id,
                            message_id=source_id,
                            user_id=auth.user_id,
                        )
                    else:
                        raise ValueError("write source must be 'previous_assistant' or 'message'.")
                    if source_message is None or source_message.role != "assistant":
                        raise ValueError("The requested assistant response could not be found.")
                    source_message_obj: Any = source_message
                    content = str(
                        source_message_obj.active_version.content
                        if source_message_obj.active_version is not None
                        else source_message_obj.content
                    )
                    if not content.strip():
                        raise ValueError(
                            "The requested assistant response has no saved content yet."
                        )
                    mode = "replace"
                if arguments.get("folder_name"):
                    return self.task_store.create_workspace_folder(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        name=str(arguments.get("folder_name") or ""),
                        parent_folder_id=str(arguments.get("folder_id") or "").strip() or None,
                    )
                filename = str(arguments.get("filename") or "").strip()
                if not filename:
                    raise ValueError("write(target='library') requires filename.")
                file_result = self.task_store.write_workspace_file(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    filename=filename,
                    content=content,
                    mode=mode,
                    parent_folder_id=str(arguments.get("folder_id") or "").strip() or None,
                )
                if source:
                    return {
                        "operation": "reference_copy",
                        "source": "assistant_message",
                        "source_message_id": str(source_message_obj.id),
                        "destination": "library",
                        "file": file_result,
                    }
                return file_result
            if target == "memory":
                key = str(arguments.get("memory_key") or "").strip()
                if not key:
                    raise ValueError("write(target='memory') requires memory_key.")
                memory_id = await _pkg_attr("MemoryService", MemoryService)(
                    self.db, self.settings
                ).store_fact(
                    tenant_id=str(auth.tenant_id),
                    user_id=str(auth.user_id),
                    key=key[:120],
                    value=content[:10000],
                    scope=str(arguments.get("memory_scope") or "user"),
                    tags=[],
                    importance_score=None,
                    confidence_score=1.0,
                    source="deepspace_universal_write",
                    conversation_id=str(conversation_id),
                    metadata_json={"source": "deepspace_universal_write"},
                )
                return {"memory_id": memory_id, "status": "saved", "key": key}
            raise ValueError(f"Unsupported write target: {target}.")
        if tool_name == "edit":
            target = str(arguments.get("target") or "").strip().lower()
            operation = str(arguments.get("operation") or "").strip().lower()
            if target == "workspace":
                return await self._execute_package_tool_call(
                    tool_name="write",
                    arguments={
                        key: arguments[key]
                        for key in ("path", "operation", "content", "new_path")
                        if key in arguments
                    },
                    auth=auth,
                    conversation_id=conversation_id,
                    web_provider=web_provider,
                    web_candidate=web_candidate,
                    request=request,
                )
            if target == "note":
                if operation not in {"replace", "append"}:
                    raise ValueError("Note edit supports replace or append.")
                return self.task_store.write_note(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    markdown=str(arguments.get("content") or ""),
                    mode=operation,
                )
            if target == "library":
                if not str(arguments.get("file_id") or "").strip():
                    raise ValueError("Library edit requires file_id.")
                if operation == "move":
                    move_folder_id = (
                        str(arguments.get("folder_id")).strip()
                        if "folder_id" in arguments
                        else None
                    )
                    return self.task_store.edit_workspace_file(
                        tenant_id=auth.tenant_id,
                        user_id=auth.user_id,
                        conversation_id=conversation_id,
                        file_id=str(arguments.get("file_id")),
                        parent_folder_id=move_folder_id,
                        mode="move",
                    )
                return self.task_store.edit_workspace_file(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    file_id=str(arguments.get("file_id")),
                    name=(
                        str(arguments.get("name")) if arguments.get("name") is not None else None
                    ),
                    content=(
                        str(arguments.get("content"))
                        if arguments.get("content") is not None
                        else None
                    ),
                    mode="append" if operation == "append" else "replace",
                )
            raise ValueError(f"Unsupported edit target: {target}.")
        if tool_name == "delete":
            target = str(arguments.get("target") or "").strip().lower()
            if target == "workspace":
                return await self._execute_package_tool_call(
                    tool_name="delete",
                    arguments={
                        key: arguments[key] for key in ("path", "recursive") if key in arguments
                    },
                    auth=auth,
                    conversation_id=conversation_id,
                    web_provider=web_provider,
                    web_candidate=web_candidate,
                    request=request,
                )
            if target == "library":
                file_id = str(arguments.get("file_id") or "").strip()
                if not file_id:
                    raise ValueError("delete(target='library') requires file_id.")
                return self.task_store.delete_workspace_file(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    conversation_id=conversation_id,
                    file_id=file_id,
                )
            if target == "memory":
                key = str(arguments.get("memory_key") or "").strip()
                if not key:
                    raise ValueError("delete(target='memory') requires memory_key.")
                deleted = await _pkg_attr("MemoryService", MemoryService)(
                    self.db, self.settings
                ).forget_memory(
                    tenant_id=str(auth.tenant_id),
                    user_id=str(auth.user_id),
                    key=key[:120],
                )
                return {"key": key, "deleted": deleted}
            raise ValueError(f"Unsupported delete target: {target}.")
        if tool_name == "web_search":
            selected_web_provider = web_provider
            selected_web_candidate = web_candidate
            provider_name = str(arguments.get("provider") or "").strip().casefold()
            if web_search_options:
                if provider_name:
                    selected = web_search_options.get(provider_name)
                    if selected is None:
                        available = ", ".join(sorted(web_search_options))
                        raise ValueError(
                            f"The requested web search provider is not available. Choose one of: {available}."
                        )
                else:
                    selected = next(iter(web_search_options.values()))
                selected_web_provider, selected_web_candidate = selected
            if selected_web_provider is None or selected_web_candidate is None:
                raise ValueError("No web search provider is configured.")
            _response, payload = await self._run_web_search(
                provider=selected_web_provider,
                candidate=selected_web_candidate,
                arguments=arguments,
                auth=auth,
                request=request,
            )
            return payload
        if tool_name == "url_read":
            payload = await self._execute_package_tool_call(
                tool_name="web_fetch",
                arguments={
                    "url": str(arguments.get("url") or "").strip(),
                    "mode": "text",
                    **(
                        {"allowed_domains": arguments["allowed_domains"]}
                        if isinstance(arguments.get("allowed_domains"), list)
                        else {}
                    ),
                },
                auth=auth,
                conversation_id=conversation_id,
                web_provider=web_provider,
                web_candidate=web_candidate,
                request=request,
            )
            _relabel_citations(payload, "web_fetch")
            return payload
        if tool_name == "image_read":
            payload = await self._execute_package_tool_call(
                tool_name="web_fetch",
                arguments={
                    "url": str(arguments.get("url") or "").strip(),
                    "mode": "image",
                    **(
                        {"allowed_domains": arguments["allowed_domains"]}
                        if isinstance(arguments.get("allowed_domains"), list)
                        else {}
                    ),
                },
                auth=auth,
                conversation_id=conversation_id,
                web_provider=web_provider,
                web_candidate=web_candidate,
                request=request,
            )
            _relabel_citations(payload, "web_fetch")
            return payload
        if tool_name == "ask_user":
            question = str(arguments.get("question") or "").strip()[:2000]
            if not question:
                raise ValueError("ask_user requires a question.")
            options = arguments.get("options")
            return {
                "awaiting_user": True,
                "question": question,
                "options": (
                    [str(item).strip()[:200] for item in options[:8] if str(item).strip()]
                    if isinstance(options, list)
                    else []
                ),
            }
        raise ValueError(f"Tool '{tool_name}' is not available in DeepSpace.")
