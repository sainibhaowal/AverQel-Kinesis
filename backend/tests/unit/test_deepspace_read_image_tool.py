"""Tests for the DeepSpace read_image tool."""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.deepspace.services.tools import fs as fs_tools
from app.deepspace.services.tools import image as image_tools
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.tools.image import READ_IMAGE_TOOLS, ReadImageArguments

pytestmark = pytest.mark.unit_no_db

#: The first bytes of every PNG file.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()


def _context() -> ToolContext:
    """Build a tool context for the calling conversation."""
    return ToolContext(
        auth=SimpleNamespace(tenant_id=TENANT_ID, user_id=USER_ID),
        conversation_id=CONVERSATION_ID,
        settings=SimpleNamespace(),
        db=None,
        task_store=SimpleNamespace(),
        tool_result_store=None,
        provider_registry=SimpleNamespace(),
    )


def _workspace(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point the workspace tools at one root."""
    resolved = root.resolve()
    monkeypatch.setattr(
        fs_tools,
        "workspace_root_for",
        lambda settings, tenant_id, user_id: resolved,
    )


def _call(arguments: dict[str, Any]) -> dict[str, Any]:
    """Run the registered read_image handler."""
    return asyncio.run(READ_IMAGE_TOOLS[0].handler(arguments, _context()))


def test_the_read_image_spec_declares_its_name_and_mode() -> None:
    spec = READ_IMAGE_TOOLS[0]

    assert spec.name == "read_image"
    assert spec.mode == "read"
    assert spec.arguments_model is ReadImageArguments
    parameters = spec.definition["function"]["parameters"]
    assert parameters["required"] == ["path"]
    assert set(parameters["properties"]) == {"path", "max_bytes"}


def test_the_read_image_arguments_refuse_values_outside_their_bounds() -> None:
    with pytest.raises(ValidationError):
        ReadImageArguments(path="a.png", unexpected="value")
    with pytest.raises(ValidationError):
        ReadImageArguments(path="a.png", max_bytes=4_194_305)


def test_reading_a_png_returns_its_bytes_as_base64(monkeypatch, tmp_path: Path) -> None:
    _workspace(monkeypatch, tmp_path)
    payload = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR"
    (tmp_path / "pixel.png").write_bytes(payload)

    result = _call({"path": "pixel.png"})

    assert result == {
        "status": "ok",
        "path": "pixel.png",
        "mime_type": "image/png",
        "data": base64.b64encode(payload).decode("ascii"),
        "size_bytes": len(payload),
        "truncated": False,
    }


def test_the_media_type_follows_the_extension(monkeypatch, tmp_path: Path) -> None:
    _workspace(monkeypatch, tmp_path)
    (tmp_path / "photo.JPEG").write_bytes(b"\xff\xd8\xff\xe0rest")

    result = _call({"path": "photo.JPEG"})

    assert result["status"] == "ok"
    assert result["mime_type"] == "image/jpeg"
    assert result["path"] == "photo.JPEG"


def test_a_path_outside_the_workspace_is_refused(monkeypatch, tmp_path: Path) -> None:
    _workspace(monkeypatch, tmp_path)
    (tmp_path / "secret.png").write_bytes(PNG_SIGNATURE)

    result = _call({"path": "../secret.png"})

    assert result["status"] == "error"
    assert "may not contain '..'" in str(result["message"])


def test_a_symlink_out_of_the_workspace_is_refused(monkeypatch, tmp_path: Path) -> None:
    _workspace(monkeypatch, tmp_path)
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(PNG_SIGNATURE)
    (tmp_path / "link.png").symlink_to(outside)

    result = _call({"path": "link.png"})

    assert result["status"] == "error"
    assert "outside the project workspace" in str(result["message"])


def test_a_file_that_is_not_an_image_is_refused(monkeypatch, tmp_path: Path) -> None:
    _workspace(monkeypatch, tmp_path)
    (tmp_path / "notes.png").write_bytes(b"this is a text file")

    result = _call({"path": "notes.png"})

    assert result["status"] == "error"
    assert "does not hold image/png image data" in str(result["message"])


def test_an_unsupported_extension_is_refused(monkeypatch, tmp_path: Path) -> None:
    _workspace(monkeypatch, tmp_path)
    (tmp_path / "diagram.svg").write_bytes(b"<svg/>")

    result = _call({"path": "diagram.svg"})

    assert result["status"] == "error"
    assert "not a supported image format" in str(result["message"])


def test_a_file_over_the_cap_is_truncated(monkeypatch, tmp_path: Path) -> None:
    _workspace(monkeypatch, tmp_path)
    payload = PNG_SIGNATURE + b"x" * 64
    (tmp_path / "large.png").write_bytes(payload)

    result = _call({"path": "large.png", "max_bytes": 16})

    assert result["status"] == "ok"
    assert result["truncated"] is True
    assert result["size_bytes"] == len(payload)
    assert base64.b64decode(str(result["data"])) == payload[:16]


def test_a_missing_file_is_refused(monkeypatch, tmp_path: Path) -> None:
    _workspace(monkeypatch, tmp_path)

    result = _call({"path": "absent.png"})

    assert result["status"] == "error"
    assert "No such file" in str(result["message"])


def test_a_directory_is_refused(monkeypatch, tmp_path: Path) -> None:
    _workspace(monkeypatch, tmp_path)
    (tmp_path / "images").mkdir()

    result = _call({"path": "images"})

    assert result["status"] == "error"
    assert "directory" in str(result["message"])


def test_the_image_module_serves_only_the_four_supported_formats() -> None:
    assert set(image_tools.IMAGE_MEDIA_TYPES) == {".png", ".jpg", ".jpeg", ".webp", ".gif"}
