"""Spill tools for DeepSpace.

A tool result that is too large to carry in the model context is offloaded to
durable, content-addressed storage.  The model receives a preview plus a
reference; ``spill_read`` reads the complete result back when the preview is
not enough.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from pydantic import Field
from sqlalchemy import desc, select

from app.deepspace.models.conversation import Conversation
from app.deepspace.models.message import Message
from app.deepspace.services.spill_policy import result_preview
from app.deepspace.services.spill_store import SpillStore, SpillStoreError
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    string_property,
)
from app.providers.services.image_inputs import SUPPORTED_IMAGE_MIME_TYPES

#: The largest number of characters one spill read returns.
MAX_SPILL_READ_CHARS = 200_000

#: The largest number of characters one spill read's offset may skip.
MAX_SPILL_OFFSET = 5_000_000
MAX_SESSION_SPILL_MESSAGES = 500
MAX_SPILL_RECOVERED_IMAGES = 10
MAX_SPILL_RECOVERED_IMAGE_BYTES = 4 * 1024 * 1024
_IMAGE_DATA_URI = re.compile(
    r"^data:(image/[a-z0-9.+-]+);base64,([a-zA-Z0-9+/]*={0,2})$", re.IGNORECASE
)


class SpillReadArguments(ToolArguments):
    """Read a spilled tool result back."""

    spill_ref: str = Field(min_length=64, max_length=64)
    offset: int | None = Field(default=None, ge=0, le=MAX_SPILL_OFFSET)
    max_characters: int | None = Field(default=None, ge=100, le=MAX_SPILL_READ_CHARS)
    include_images: bool = True


class SpillSessionArguments(ToolArguments):
    """Save a readable reference to the current conversation history."""

    label: str = Field(default="conversation", min_length=1, max_length=120)


def _recover_spilled_images(value: Any) -> tuple[Any, list[str], int]:
    """Return a base64-redacted display copy and bounded image model inputs."""
    recovered: list[str] = []
    recovered_bytes = 0
    omitted = 0

    def count_images(value: Any) -> int:
        """Count image nodes without recursing into untrusted nested JSON."""
        count = 0
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                count += current.get("type") == "image"
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)
        return count

    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal recovered_bytes, omitted
        if depth > 32:
            omitted += count_images(item)
            return "[nested spill content omitted for safe image recovery]"
        if isinstance(item, list):
            return [visit(child, depth + 1) for child in item]
        if not isinstance(item, dict):
            return item
        copied = {key: visit(child, depth + 1) for key, child in item.items()}
        if item.get("type") != "image":
            return copied

        candidates: list[tuple[dict[str, Any], str, str]] = []
        source = item.get("source")
        if isinstance(source, dict) and isinstance(source.get("data"), str):
            candidates.append((copied["source"], "data", source["data"]))
        if isinstance(item.get("data"), str):
            candidates.append((copied, "data", item["data"]))
        image_url = item.get("image_url")
        if (
            isinstance(image_url, dict)
            and isinstance(image_url.get("url"), str)
            and image_url["url"].startswith("data:")
        ):
            candidates.append((copied["image_url"], "url", image_url["url"]))

        image_data: str | None = None
        omission_reason = "[invalid image data retained in spill]"
        for owner, field_name, raw in candidates:
            media_type = ""
            encoded = raw
            if raw.startswith("data:"):
                match = _IMAGE_DATA_URI.fullmatch(raw)
                if match is None:
                    owner[field_name] = omission_reason
                    continue
                media_type, encoded = match.groups()
                media_type = media_type.casefold()
                if media_type not in SUPPORTED_IMAGE_MIME_TYPES:
                    owner[field_name] = "[unsupported image format retained in spill]"
                    omission_reason = "[unsupported image format retained in spill]"
                    continue
            elif field_name == "url":
                # Remote URL images are not fetched by spill recovery; only
                # content already stored in the tenant spill can be attached.
                continue
            try:
                image_bytes = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                owner[field_name] = omission_reason
                continue
            if not image_bytes:
                owner[field_name] = omission_reason
                continue
            if (
                len(recovered) >= MAX_SPILL_RECOVERED_IMAGES
                or recovered_bytes + len(image_bytes) > MAX_SPILL_RECOVERED_IMAGE_BYTES
            ):
                owner[field_name] = "[image retained in spill; recovery limit reached]"
                omission_reason = "[image retained in spill; recovery limit reached]"
                continue
            if media_type:
                image_data = f"data:{media_type};base64,{encoded}"
            else:
                source_type = item.get("source")
                declared_type = (
                    source_type.get("media_type") if isinstance(source_type, dict) else None
                )
                if isinstance(declared_type, str):
                    normalized_type = declared_type.casefold()
                    if normalized_type not in SUPPORTED_IMAGE_MIME_TYPES:
                        omission_reason = "[unsupported image format retained in spill]"
                        owner[field_name] = omission_reason
                        continue
                    image_data = f"data:{normalized_type};base64,{encoded}"
                else:
                    image_data = encoded
            recovered_bytes += len(image_bytes)
            break
        if image_data is not None:
            recovered.append(image_data)
            # Some providers serialize the same image in more than one field.
            # Redact every copy, not only the one selected for model input.
            for owner, field_name, _raw in candidates:
                owner[field_name] = "[image reattached to the model input]"
        elif candidates:
            omitted += 1
            for owner, field_name, _raw in candidates:
                if owner.get(field_name) == _raw:
                    owner[field_name] = omission_reason
        return copied

    return visit(value), recovered, omitted


async def _spill_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read one spilled result the model was given a reference for."""
    reference = str(arguments["spill_ref"]).strip().lower()
    store = SpillStore(context.settings)
    try:
        text = store.get(tenant_id=context.auth.tenant_id, sha256=reference)
    except SpillStoreError as exc:
        return {"status": "unavailable", "message": str(exc), "spill_ref": reference}
    offset = int(arguments.get("offset") or 0)
    limit = min(
        MAX_SPILL_READ_CHARS,
        max(100, int(arguments.get("max_characters") or MAX_SPILL_READ_CHARS)),
    )
    include_images = bool(arguments.get("include_images", True))
    recoverable = text
    recovered_images: list[str] = []
    omitted_images = 0
    if include_images:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        except RecursionError:
            parsed = None
            recoverable = (
                "[Spilled JSON is too deeply nested for safe image recovery. "
                "Use include_images=false to inspect the stored text.]"
            )
        if isinstance(parsed, dict | list):
            display, recovered_images, omitted_images = _recover_spilled_images(parsed)
            if recovered_images or omitted_images or display != parsed:
                recoverable = json.dumps(
                    display, ensure_ascii=False, default=str, separators=(",", ":")
                )
    window = recoverable[offset : offset + limit]
    result = {
        "status": "ok",
        "spill_ref": reference,
        "text": window,
        "offset": offset,
        "total_characters": len(recoverable),
        "original_total_characters": len(text),
        "truncated": offset + len(window) < len(recoverable),
        "next_offset": offset + len(window) if offset + len(window) < len(recoverable) else None,
    }
    if include_images:
        result["images_recovered"] = len(recovered_images)
        result["images_omitted"] = omitted_images
        if recovered_images:
            # The chat runtime removes this internal field from the tool JSON
            # and attaches the images to the following multimodal model input.
            result["_images_base64"] = recovered_images
    return result


async def _spill_session(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Persist the current user's conversation as a recoverable spill."""
    if context.db is None:
        return {"status": "unavailable", "message": "Conversation storage is unavailable."}
    conversation = context.db.execute(
        select(Conversation.id).where(
            Conversation.id == context.conversation_id,
            Conversation.tenant_id == context.auth.tenant_id,
            Conversation.user_id == context.auth.user_id,
        )
    ).scalar_one_or_none()
    if conversation is None:
        return {"status": "not_found", "message": "The current conversation is unavailable."}
    rows = context.db.execute(
        select(Message.role, Message.content)
        .where(Message.conversation_id == context.conversation_id)
        .order_by(desc(Message.created_at), desc(Message.id))
        .limit(MAX_SESSION_SPILL_MESSAGES)
    ).all()
    if not rows:
        return {"status": "empty", "message": "The current conversation has no messages."}
    rows = list(reversed(rows))
    transcript = "\n\n".join(f"[{role}]\n{content}" for role, content in rows)
    maximum = int(getattr(context.settings, "deepspace_spill_max_chars", 2_000_000))
    if len(transcript.encode("utf-8", errors="replace")) > maximum:
        return {
            "status": "refused",
            "message": "This conversation is larger than the configured spill limit.",
            "messages_included": len(rows),
        }
    label = str(arguments.get("label") or "conversation").strip()[:120] or "conversation"
    source = {
        "kind": "session-reference",
        "session_id": str(context.conversation_id),
        "label": label,
    }
    try:
        reference = SpillStore(context.settings).put(
            tenant_id=context.auth.tenant_id,
            payload=transcript,
        )
    except SpillStoreError as exc:
        return {"status": "unavailable", "message": str(exc)}
    preview = result_preview(
        {"content": [{"type": "text", "text": transcript}]},
        transcript,
        512,
    )
    return {
        "status": "spilled",
        "spill_ref": reference.sha256,
        "size_bytes": reference.size_bytes,
        "preview": preview.preview,
        "preview_truncated": preview.omitted_bytes > 0,
        "omitted_bytes": preview.omitted_bytes,
        "source": source,
        "messages_included": len(rows),
        "message": "Read this saved conversation with spill_read and the spill_ref.",
    }


SPILL_DESCRIPTION = (
    "Read a spilled tool result. A tool result too large for the context window "
    "is offloaded to durable storage and reported with a spill_ref; use "
    "spill_read with that reference to read the complete result back."
)

SPILL_SESSION_DESCRIPTION = (
    "Save a recoverable text reference to the current conversation history. "
    "Only the authenticated user's current conversation is included. Use "
    "spill_read with the returned spill_ref to retrieve the saved transcript."
)

SPILL_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="spill_read",
        description=SPILL_DESCRIPTION,
        properties={
            "spill_ref": string_property(
                "The reference a spilled tool result reported.",
                min_length=64,
                max_length=64,
            ),
            "offset": integer_property(
                "The character offset to start reading from.", minimum=0, maximum=MAX_SPILL_OFFSET
            ),
            "max_characters": integer_property(
                "The largest number of characters to return.",
                minimum=100,
                maximum=MAX_SPILL_READ_CHARS,
            ),
            "include_images": {"type": "boolean", "default": True},
        },
        required=["spill_ref"],
        arguments_model=SpillReadArguments,
        mode="read",
        handler=_spill_read,
    ),
    define_native_tool(
        name="spill_session",
        description=SPILL_SESSION_DESCRIPTION,
        properties={
            "label": string_property(
                "A short display label for the saved conversation reference.",
                min_length=1,
                max_length=120,
            ),
        },
        required=[],
        arguments_model=SpillSessionArguments,
        mode="read",
        handler=_spill_session,
    ),
]

__all__ = ["SPILL_TOOLS", "SpillReadArguments", "SpillSessionArguments"]
