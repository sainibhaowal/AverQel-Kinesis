"""Unit tests for per-turn workspace file tracking and raw preview serving."""

from __future__ import annotations

import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.deepspace.api import workspace as workspace_api
from app.deepspace.services.workspace_snapshot import (
    changed_file_entries,
    collect_workspace_stat_entries,
    mime_type_for_path,
)

pytestmark = pytest.mark.unit_no_db


def test_changed_file_entries_tracks_binaries_large_files_and_priority(tmp_path: Path) -> None:
    """Stat snapshots catch binary, SVG/HTML, and >512 KB files while skipping build dirs."""
    existing = tmp_path / "notes.md"
    existing.write_text("# Old\n", encoding="utf-8")
    unchanged = tmp_path / "keep.txt"
    unchanged.write_text("same", encoding="utf-8")
    deleted = tmp_path / "gone.txt"
    deleted.write_text("bye", encoding="utf-8")

    before = collect_workspace_stat_entries(tmp_path)

    deleted.unlink()
    existing.write_text("# Updated notes\nmore", encoding="utf-8")
    svg = tmp_path / "ai_news_wallpaper.svg"
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    html = tmp_path / "ai_news_wallpaper.html"
    html.write_text("<!doctype html><h1>News</h1>", encoding="utf-8")
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\x00\x01\x02binary")
    ppm = tmp_path / "wallpaper_ai_news.ppm"
    ppm.write_bytes(b"P6\n10 10\n255\n" + (b"\x00\xff\x10" * 200_000))

    # Build/cache noise must remain excluded
    nm_dir = tmp_path / "node_modules" / "pkg"
    nm_dir.mkdir(parents=True)
    (nm_dir / "index.js").write_text("module.exports = {};", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    after = collect_workspace_stat_entries(tmp_path)
    entries = changed_file_entries(
        before,
        after,
        root=tmp_path,
        presented_paths=["report.pdf"],
        touched_paths=["notes.md"],
    )

    by_path = {item["path"]: item for item in entries}
    assert set(by_path) == {
        "report.pdf",
        "ai_news_wallpaper.svg",
        "ai_news_wallpaper.html",
        "wallpaper_ai_news.ppm",
        "notes.md",
    }
    assert "keep.txt" not in by_path
    assert "gone.txt" not in by_path
    assert "node_modules/pkg/index.js" not in by_path
    assert ".git/HEAD" not in by_path

    # Priority: presented first, then created (sorted), then modified
    assert entries[0]["path"] == "report.pdf"
    assert entries[0]["change_type"] == "presented"
    assert entries[0]["mime_type"] == "application/pdf"
    assert by_path["ai_news_wallpaper.svg"]["mime_type"] == "image/svg+xml"
    assert by_path["ai_news_wallpaper.html"]["mime_type"] == "text/html"
    assert by_path["wallpaper_ai_news.ppm"]["mime_type"] == "image/x-portable-pixmap"
    assert by_path["wallpaper_ai_news.ppm"]["size_bytes"] > 512_000
    assert entries[-1]["path"] == "notes.md"
    assert entries[-1]["change_type"] == "modified"


def test_workspace_file_and_raw_routes_handle_binaries_headers_and_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw workspace route validates paths, enforces size limits, and sets safe headers."""
    monkeypatch.setattr(workspace_api, "_conversation_workspace", lambda *args, **kwargs: tmp_path)

    pdf = tmp_path / "deliverable.pdf"
    pdf.write_bytes(b"%PDF-1.7\x00\xff\xfe")
    svg = tmp_path / "poster.svg"
    svg.write_text("<svg></svg>", encoding="utf-8")

    auth = SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    conv_id = uuid.uuid4()
    settings = SimpleNamespace()
    db = object()

    binary_meta = workspace_api.read_workspace_file(
        conv_id, path="deliverable.pdf", auth=auth, db=db, settings=settings
    )
    assert binary_meta["is_binary"] is True
    assert binary_meta["content"] == ""
    assert binary_meta["mime_type"] == "application/pdf"
    assert binary_meta["size_bytes"] == pdf.stat().st_size

    svg_meta = workspace_api.read_workspace_file(
        conv_id, path="poster.svg", auth=auth, db=db, settings=settings
    )
    assert svg_meta["is_binary"] is False
    assert svg_meta["content"] == "<svg></svg>"
    assert svg_meta["mime_type"] == "image/svg+xml"
    assert svg_meta["language"] == "xml"

    inline_resp = workspace_api.read_workspace_file_raw(
        conv_id, path="deliverable.pdf", download=False, auth=auth, db=db, settings=settings
    )
    assert inline_resp.media_type == "application/pdf"
    assert inline_resp.headers["x-content-type-options"] == "nosniff"
    assert inline_resp.headers["content-security-policy"] == "sandbox"
    assert inline_resp.headers["cache-control"] == "no-store"
    assert "inline;" in inline_resp.headers["content-disposition"]

    download_resp = workspace_api.read_workspace_file_raw(
        conv_id, path="deliverable.pdf", download=True, auth=auth, db=db, settings=settings
    )
    assert "attachment;" in download_resp.headers["content-disposition"]

    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("notes.md", "# Archive note\n")
    archive_resp = workspace_api.read_workspace_archive_entry(
        conv_id,
        path="bundle.zip",
        entry="notes.md",
        auth=auth,
        db=db,
        settings=settings,
    )
    assert archive_resp.media_type == "text/markdown"
    assert archive_resp.body == b"# Archive note\n"

    with pytest.raises(HTTPException) as traversal_exc:
        workspace_api.read_workspace_file_raw(
            conv_id, path="../outside.txt", auth=auth, db=db, settings=settings
        )
    assert traversal_exc.value.status_code == 400

    with pytest.raises(HTTPException) as missing_exc:
        workspace_api.read_workspace_file_raw(
            conv_id, path="missing.pdf", auth=auth, db=db, settings=settings
        )
    assert missing_exc.value.status_code == 404

    monkeypatch.setattr(workspace_api, "MAX_RAW_FILE_BYTES", 4)
    with pytest.raises(HTTPException) as limit_exc:
        workspace_api.read_workspace_file_raw(
            conv_id, path="deliverable.pdf", auth=auth, db=db, settings=settings
        )
    assert limit_exc.value.status_code == 413


def test_mime_type_for_path_covers_workspace_formats() -> None:
    """MIME lookup resolves common code, web, document, and raw image extensions."""
    assert mime_type_for_path(Path("a.svg")) == "image/svg+xml"
    assert mime_type_for_path(Path("a.ppm")) == "image/x-portable-pixmap"
    assert mime_type_for_path(Path("a.ts")) == "text/typescript"
    assert mime_type_for_path(Path("a.unknownext_xyz")) == "application/octet-stream"


def test_collect_turn_workspace_files_captures_all_tool_variants_and_json_outputs(
    tmp_path: Path,
) -> None:
    """Turn collection captures fs_write, create_file, present, and JSON output paths."""
    from app.deepspace.services.chat_service.events import _EventsMixin

    helper = _EventsMixin()
    existing_doc = tmp_path / "notes.docx"
    existing_doc.write_bytes(b"PK\x03\x04docx-before")
    (tmp_path / "ai_news_wallpaper.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "wallpaper_ai_news.ppm").write_bytes(b"P6\n2 2\n255\n123456123456")

    before_stats = collect_workspace_stat_entries(tmp_path)
    # Create an extra nested file via tool metadata while untouched existing_doc is explicitly written
    (tmp_path / "ai_news_wallpaper.html").write_text("<html></html>", encoding="utf-8")

    state = SimpleNamespace(
        workspace_root=tmp_path,
        workspace_stat_before=before_stats,
        durable_steps=[
            {
                "status": "completed",
                "toolName": "present",
                "toolInput": {"paths": ["ai_news_wallpaper.svg", "wallpaper_ai_news.ppm"]},
            },
            {
                "status": "completed",
                "toolName": "fs_write",
                "toolInput": {"path": str(existing_doc)},
            },
            {
                "status": "completed",
                "toolName": "bash",
                "toolInput": {"command": "python make_wallpaper.py"},
                "toolOutput": '{"output_path": "ai_news_wallpaper.html"}',
            },
        ],
    )

    entries = helper._collect_turn_workspace_files(state)
    by_path = {entry["path"]: entry for entry in entries}
    assert {
        "ai_news_wallpaper.svg",
        "wallpaper_ai_news.ppm",
        "ai_news_wallpaper.html",
        "notes.docx",
    }.issubset(by_path.keys())
    assert by_path["ai_news_wallpaper.svg"]["change_type"] == "presented"
    assert by_path["notes.docx"]["change_type"] == "modified"
    assert by_path["ai_news_wallpaper.html"]["mime_type"] == "text/html"
