"""SSE event streaming for DeepSpace real-time updates.

This module provides a unified SSE endpoint that streams:
- Subagent events (spawn, start, end, result, descriptor, preset)
- Job progress events (queued, started, completed, failed)
- File watch notifications (create, modify, delete, move)
- Tool call/result events (for live timeline updates)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Query, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.core.config import Settings
from app.deepspace.services.session_events import (
    SUBAGENT_DESCRIPTOR,
    SUBAGENT_END,
    SUBAGENT_PRESET,
    SUBAGENT_RESULT,
    SUBAGENT_SPAWN,
    SUBAGENT_START,
)

logger = logging.getLogger(__name__)

# Event types that can be streamed
SUBAGENT_EVENT_TYPES = frozenset(
    {
        SUBAGENT_SPAWN,
        SUBAGENT_START,
        SUBAGENT_END,
        SUBAGENT_RESULT,
        SUBAGENT_DESCRIPTOR,
        SUBAGENT_PRESET,
    }
)

TOOL_EVENT_TYPES = frozenset(
    {
        "tool_call",
        "tool_result",
    }
)

JOB_EVENT_TYPES = frozenset(
    {
        "job_queued",
        "job_started",
        "job_completed",
        "job_failed",
        "artifact_queued",
        "artifact_completed",
        "artifact_failed",
    }
)

FILE_WATCH_EVENT_TYPES = frozenset(
    {
        "file_created",
        "file_modified",
        "file_deleted",
        "file_moved",
    }
)

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """A single SSE event to be sent to the client."""

    event: str
    data: dict[str, Any]
    sequence: int


class EventStreamer:
    """Streams real-time events for a conversation via SSE."""

    def __init__(
        self,
        *,
        settings: Settings,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        last_sequence: int = 0,
    ) -> None:
        self.settings = settings
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.conversation_id = conversation_id
        self._next_sequence = max(0, last_sequence) + 1
        self._queue: asyncio.Queue[SSEEvent | None] = asyncio.Queue(maxsize=1024)
        self._redis: Redis | None = None
        self._pubsub_task: asyncio.Task[None] | None = None
        self._db_task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Start the event streamer."""
        self._running = True
        await self._start_redis_listener()

    async def stop(self) -> None:
        """Stop the event streamer."""
        self._running = False

    def _format_sse(self, event: SSEEvent) -> str:
        """Format an event as SSE."""
        return f"event: {event.event}\ndata: {json.dumps(event.data, separators=(',', ':'))}\n\n"

    async def _publish_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish an event to the stream."""
        event = SSEEvent(
            event=event_type,
            data=data,
            sequence=self._next_sequence,
        )
        self._next_sequence += 1
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("SSE queue full, dropping event")

    async def _start_redis_listener(self) -> None:
        """Start listening for Redis pub/sub events."""
        redis_client = None
        try:
            redis_client = Redis.from_url(self.settings.redis_url, decode_responses=True)
            pubsub = redis_client.pubsub()

            # Subscribe to relevant channels
            channels = [
                f"deepspace:subagent:{self.tenant_id}:{self.user_id}:{self.conversation_id}",
                f"deepspace:job:{self.tenant_id}:{self.user_id}:{self.conversation_id}",
                f"deepspace:filewatch:{self.tenant_id}:{self.user_id}:{self.conversation_id}",
            ]
            await pubsub.subscribe(*channels)

            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        await self._handle_redis_message(message["channel"], data)
                    except Exception:
                        logger.warning("Failed to handle Redis message", exc_info=True)
        except Exception:
            logger.warning("Redis listener failed", exc_info=True)
        finally:
            if pubsub:
                await pubsub.unsubscribe()
            if redis_client:
                await redis_client.aclose()

    async def _handle_redis_message(self, channel: str, data: dict[str, Any]) -> None:
        """Handle a Redis pub/sub message."""
        # Parse channel to determine event type
        if "subagent" in channel:
            await self._handle_subagent_event(data)
        elif "job" in channel:
            await self._handle_job_event(data)
        elif "filewatch" in channel:
            await self._handle_filewatch_event(data)

    async def _handle_subagent_event(self, data: dict[str, Any]) -> None:
        """Handle a subagent event."""
        event_type = data.get("event_type")
        if event_type in SUBAGENT_EVENT_TYPES:
            await self._publish_event(event_type, data)

    async def _handle_job_event(self, data: dict[str, Any]) -> None:
        """Handle a job event."""
        event_type = data.get("event_type")
        if event_type in JOB_EVENT_TYPES:
            await self._publish_event(event_type, data)

    async def _handle_filewatch_event(self, data: dict[str, Any]) -> None:
        """Handle a file watch event."""
        event_type = data.get("event_type")
        if event_type in FILE_WATCH_EVENT_TYPES:
            await self._publish_event(event_type, data)

    async def _poll_database_events(self) -> None:
        """Poll database for new events not caught by Redis."""
        _ = 0
        while True:
            try:
                await asyncio.sleep(1.0)
                await self._check_new_events()
            except Exception:
                logger.warning("Database polling failed", exc_info=True)

    async def _check_new_events(self) -> None:
        """Check for new events in the database."""
        # This would query the session events table for new events
        # For now, we rely on Redis pub/sub
        pass

    def __aiter__(self) -> AsyncIterator[str]:
        return self._event_generator()

    async def _event_generator(self) -> AsyncIterator[str]:
        """Generate SSE events."""
        # Send initial connection event
        yield f"event: connected\ndata: {json.dumps({'status': 'connected'})}\n\n"

        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=30.0)
                if event is None:
                    break
                yield self._format_sse(event)
            except TimeoutError:
                # Send keepalive
                yield f"event: keepalive\ndata: {json.dumps({'timestamp': datetime.now(UTC).isoformat()})}\n\n"
            except Exception:
                logger.warning("Event generator error", exc_info=True)
                break


async def stream_conversation_events(
    conversation_id: uuid.UUID,
    request: Request,
    auth: AuthContext,
    settings: Settings,
    db: Session,
    last_sequence: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """Stream real-time events for a conversation.

    Events include:
    - Subagent events: spawn, start, end, result, descriptor, preset
    - Job events: queued, started, completed, failed
    - File watch events: created, modified, deleted, moved
    - Tool events: call, result

    The stream uses Server-Sent Events (SSE) with automatic reconnection support.
    """
    streamer = EventStreamer(
        settings=settings,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        last_sequence=last_sequence,
    )

    await streamer.start()

    async def event_generator() -> AsyncIterator[str]:
        try:
            async for event in streamer:
                if await request.is_disconnected():
                    break
                yield event
        finally:
            await streamer.stop()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# Helper functions for publishing events from other parts of the system


async def publish_subagent_event(
    settings: Settings,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Publish a subagent event to Redis for SSE streaming."""
    redis_client = None
    try:
        redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        channel = f"deepspace:subagent:{tenant_id}:{user_id}:{conversation_id}"
        payload = {"event_type": event_type, **data}
        await redis_client.publish(channel, json.dumps(payload))
    except Exception:
        logger.warning("Failed to publish subagent event", exc_info=True)
    finally:
        if redis_client:
            try:
                await redis_client.aclose()
            except Exception:
                pass


async def publish_job_event(
    settings: Settings,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Publish a job event to Redis for SSE streaming."""
    redis_client = None
    try:
        redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        channel = f"deepspace:job:{tenant_id}:{user_id}:{conversation_id}"
        payload = {"event_type": event_type, **data}
        await redis_client.publish(channel, json.dumps(payload))
    except Exception:
        logger.warning("Failed to publish job event", exc_info=True)
    finally:
        if redis_client:
            try:
                await redis_client.aclose()
            except Exception:
                pass


async def publish_filewatch_event(
    settings: Settings,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Publish a file watch event to Redis for SSE streaming."""
    redis_client = None
    try:
        redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        channel = f"deepspace:filewatch:{tenant_id}:{user_id}:{conversation_id}"
        payload = {"event_type": event_type, **data}
        await redis_client.publish(channel, json.dumps(payload))
    except Exception:
        logger.warning("Failed to publish filewatch event", exc_info=True)
    finally:
        if redis_client:
            try:
                await redis_client.aclose()
            except Exception:
                pass
