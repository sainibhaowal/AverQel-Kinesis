"""Tests for the shell tool's output handling.

A command that prints more than the model context holds must not lose the
rest. The output is spilled to durable storage, and the model reads it back
in pages.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.deepspace.services.tools.shell import _spill_large_output

pytestmark = pytest.mark.unit_no_db


class _Auth:
    """The caller the tool context carries."""

    tenant_id = "tenant"
    user_id = "user"


class _Context:
    """A tool context with only what the helper reads."""

    def __init__(self, threshold: int = 24_000) -> None:
        self.auth = _Auth()
        self.settings = _Settings(threshold)


class _Settings:
    """The settings the helper reads."""

    def __init__(self, threshold: int) -> None:
        self.deepspace_spill_threshold_chars = threshold
        self.deepspace_spill_capture_bytes = 8_000_000


def test_a_small_output_is_returned_unchanged() -> None:
    result = {"status": "ok", "output": "hello", "exit_code": 0}

    returned = _spill_large_output(result, context=_Context(), tool_name="bash")  # type: ignore[arg-type]

    assert returned is result
    assert returned["output"] == "hello"


def test_a_large_output_is_spilled_and_the_whole_output_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import spill_store

    kept: dict[str, Any] = {}

    def _fake_put(self, *, tenant_id, payload):  # type: ignore[no-untyped-def]
        kept["payload"] = payload
        return spill_store.SpillRef(
            sha256="a" * 64,
            size_bytes=len(payload.encode("utf-8")),
            bucket="bucket",
            object_key="tenant/spill/" + "a" * 64,
        )

    monkeypatch.setattr(spill_store.SpillStore, "put", _fake_put)
    output = "x" * 60_000
    result = {"status": "ok", "output": output, "exit_code": 0}

    returned = _spill_large_output(result, context=_Context(), tool_name="bash")  # type: ignore[arg-type]

    assert returned["status"] == "spilled"
    assert returned["spill_ref"] == "a" * 64
    assert returned["preview"]
    assert returned["preview_truncated"] is True
    # The whole output was written, so the model can read all of it back.
    assert len(kept["payload"]) >= len(output)
    assert "spill_read" in returned["message"]


def test_an_output_larger_than_the_storage_ceiling_keeps_its_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import spill_store

    def _raise(self, *, tenant_id, payload):  # type: ignore[no-untyped-def]
        raise spill_store.SpillStoreError("too large")

    monkeypatch.setattr(spill_store.SpillStore, "put", _raise)
    result = {"status": "ok", "output": "x" * 60_000, "exit_code": 0}

    returned = _spill_large_output(result, context=_Context(), tool_name="bash")  # type: ignore[arg-type]

    assert returned["status"] == "ok"
    assert returned["truncated"] is True


def test_a_broken_spill_leaves_the_result_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import spill_store

    def _raise(self, *, tenant_id, payload):  # type: ignore[no-untyped-def]
        raise RuntimeError("storage is down")

    monkeypatch.setattr(spill_store.SpillStore, "put", _raise)
    result = {"status": "ok", "output": "x" * 60_000, "exit_code": 0}

    returned = _spill_large_output(result, context=_Context(), tool_name="bash")  # type: ignore[arg-type]

    assert returned["status"] == "ok"
    assert returned["output"] == "x" * 60_000
