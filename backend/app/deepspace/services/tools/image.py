"""Read a local image file for DeepSpace.

``image_read`` fetches an image from a public URL and ``fs_read`` reads text
files.  This tool covers the file on disk: an image inside the project
workspace, returned to a vision-capable model as base64 together with its
media type.

Only the four formats a vision model accepts are served, and the file's first
bytes must carry that format's signature, so a renamed text file never reaches
the model as an image.

The path is resolved with the file tools' own resolver, so an image path is
bounded by the same workspace root and the same symlink check as a text path.
"""

from __future__ import annotations

import base64
from typing import Any

from pydantic import Field

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    string_property,
)
from app.deepspace.services.tools.fs import _resolve

#: The number of bytes one image read returns when the caller names no limit.
MAX_IMAGE_BYTES = 4_194_304

#: The media type of every image format this tool serves.
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ReadImageArguments(ToolArguments):
    """Read one image file from the project workspace."""

    path: str = Field(min_length=1, max_length=1024)
    max_bytes: int | None = Field(default=None, ge=1, le=MAX_IMAGE_BYTES)


def _has_image_signature(data: bytes, media_type: str) -> bool:
    """Report whether the bytes start with the signature of the given format.

    Args:
        data: The first bytes of the file.
        media_type: The media type the file's extension claims.

    Returns:
        True when the bytes carry that format's signature.
    """
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if media_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


async def _read_image(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read one workspace image file."""
    try:
        resolved, refusal = _resolve(context, str(arguments.get("path") or ""))
        if resolved is None:
            message = str((refusal or {}).get("message") or "")
            return {
                "status": "error",
                "message": message or "The path could not be resolved in the project workspace.",
            }
        target = resolved.absolute
        if not target.exists():
            return {
                "status": "error",
                "message": "No such file in the project workspace.",
            }
        if target.is_dir():
            return {
                "status": "error",
                "message": "The path is a directory, not an image file.",
            }
        media_type = IMAGE_MEDIA_TYPES.get(target.suffix.casefold())
        if media_type is None:
            return {
                "status": "error",
                "message": (
                    f"'{target.name}' is not a supported image format. Use .png, .jpg, "
                    ".jpeg, .webp, or .gif."
                ),
            }
        max_bytes = min(MAX_IMAGE_BYTES, max(1, int(arguments.get("max_bytes") or MAX_IMAGE_BYTES)))
        with target.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
        truncated = len(payload) > max_bytes
        if truncated:
            payload = payload[:max_bytes]
        if not _has_image_signature(payload, media_type):
            return {
                "status": "error",
                "message": f"The file does not hold {media_type} image data.",
            }
        return {
            "status": "ok",
            "path": resolved.relative,
            "mime_type": media_type,
            "data": base64.b64encode(payload).decode("ascii"),
            "size_bytes": target.stat().st_size,
            "truncated": truncated,
        }
    except Exception as exc:  # noqa: BLE001 - a tool call must not raise
        return {"status": "error", "message": f"The image could not be read: {exc}"}


READ_IMAGE_DESCRIPTION = (
    "Read an image file from the project workspace and return it to a "
    "vision-capable model. The path is relative to the workspace root and the "
    "file must be a PNG, JPEG, WebP, or GIF image."
)

READ_IMAGE_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="read_image",
        description=READ_IMAGE_DESCRIPTION,
        properties={
            "path": string_property(
                "Workspace-relative path of the image to read.",
                min_length=1,
                max_length=1024,
            ),
            "max_bytes": integer_property(
                "The largest number of bytes to read from the image.",
                minimum=1,
                maximum=MAX_IMAGE_BYTES,
            ),
        },
        required=["path"],
        arguments_model=ReadImageArguments,
        mode="read",
        handler=_read_image,
    ),
]

__all__ = ["READ_IMAGE_TOOLS", "ReadImageArguments"]
