"""Unit tests for inline XML/Hermes ``<tool_call>`` stream suppression and extraction."""

from __future__ import annotations

import json

import pytest

from app.deepspace.services.xml_tool_calls import (
    XmlToolCallStreamBuffer,
    extract_inline_tool_calls,
)

pytestmark = pytest.mark.unit_no_db


def test_extract_inline_tool_calls_parses_nemotron_write_with_html_content() -> None:
    raw = (
        "Let me create the TypeScript TODO app.\n"
        "<tool_call>\n"
        "<function=write>\n"
        "<parameter=path>\n"
        "/home/AverQel/Kinesis/todo-app.ts\n"
        "</parameter>\n"
        "<parameter=content>\n"
        "import express from 'express'\n"
        "const html = `<html><body><h1>TODO</h1><script>let todos = [];</script></body></html>`\n"
        "console.log(html)\n"
        "</parameter>\n"
        "</function>\n"
        "</tool_call>\n"
    )

    result = extract_inline_tool_calls(raw, start_index=1, call_id_prefix="xml_call_r5")

    assert result.cleaned_text == "Let me create the TypeScript TODO app."
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call["id"] == "xml_call_r5_1"
    assert call["function"]["name"] == "write"
    args = json.loads(call["function"]["arguments"])
    assert args["path"] == "/home/AverQel/Kinesis/todo-app.ts"
    assert (
        "<html><body><h1>TODO</h1><script>let todos = [];</script></body></html>" in args["content"]
    )
    assert args["content"].startswith("import express from 'express'")


def test_extract_inline_tool_calls_coerces_json_and_scalar_non_code_params() -> None:
    raw = (
        "<tool_call>\n"
        "<function=bash>\n"
        "<parameter=command>\nnpm run dev\n</parameter>\n"
        "<parameter=timeout_seconds>\n15\n</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )

    result = extract_inline_tool_calls(raw)
    assert result.cleaned_text == ""
    assert len(result.tool_calls) == 1
    args = json.loads(result.tool_calls[0]["function"]["arguments"])
    assert args == {"command": "npm run dev", "timeout_seconds": 15}


def test_stream_buffer_streams_prose_and_withholds_split_tool_call_xml() -> None:
    buf = XmlToolCallStreamBuffer()
    chunks = [
        "I checked x < ",
        "y first.\n",
        "<too",
        "l_call>\n<function=read>\n<para",
        "meter=path>\npackage.json\n</parameter>\n</function>\n</to",
        "ol_call>",
    ]
    emitted: list[str] = []
    for chunk in chunks:
        out = buf.feed(chunk)
        if out:
            emitted.append(out)

    trailing, extracted = buf.finalize(call_id_prefix="xml_test")
    assert trailing == ""
    # Normal `< ` comparison flushed immediately without waiting for stream end.
    assert "".join(emitted) == "I checked x < y first.\n"
    assert extracted.cleaned_text == "I checked x < y first."
    assert len(extracted.tool_calls) == 1
    assert extracted.tool_calls[0]["function"]["name"] == "read"
    assert json.loads(extracted.tool_calls[0]["function"]["arguments"]) == {"path": "package.json"}


def test_stream_buffer_flushes_trailing_angle_bracket_when_no_tool_call() -> None:
    buf = XmlToolCallStreamBuffer()
    assert buf.feed("compare a ") == "compare a "
    assert buf.feed("<") == ""
    trailing, extracted = buf.finalize()
    assert trailing == "<"
    assert extracted.tool_calls == []


@pytest.mark.asyncio
async def test_chat_service_round_executes_inline_xml_write_tool_call(
    monkeypatch, tmp_path
) -> None:
    """End-to-end check: raw XML <tool_call><function=write> runs the real write tool."""
    from types import SimpleNamespace
    from uuid import uuid4

    import tests.unit.test_deepspace_chat_service as chat_test_mod

    round_num = 0

    class _XmlToolProvider:
        async def stream_generate_events(self, _request):
            nonlocal round_num
            round_num += 1
            if round_num == 1:
                yield {
                    "type": "delta",
                    "text": "Creating file now.\n<tool_call>\n<function=write>\n",
                }
                yield {
                    "type": "delta",
                    "text": f"<parameter=path>\n{tmp_path / 'todo.ts'}\n</parameter>\n",
                }
                yield {
                    "type": "delta",
                    "text": "<parameter=content>\nexport const ok = true;\n</parameter>\n</function>\n</tool_call>",
                }
            else:
                yield {"type": "delta", "text": "Created todo.ts successfully."}

    class _Registry(chat_test_mod._FakeRegistry):
        def get_chat_provider_from_selection(self, _candidate):
            return _XmlToolProvider()

    monkeypatch.setattr(
        chat_test_mod.chat_service_module, "DeepSpaceChatRepository", chat_test_mod._FakeRepository
    )
    monkeypatch.setattr(
        chat_test_mod.chat_service_module,
        "ProviderSelectionService",
        chat_test_mod._FakeSelectionService,
    )
    monkeypatch.setattr(chat_test_mod.chat_service_module, "ProviderRegistry", _Registry)
    monkeypatch.setattr(
        chat_test_mod.chat_service_module, "DeepSpaceTaskLoopStore", chat_test_mod._FakeTaskStore
    )

    service = chat_test_mod.DeepSpaceChatService(
        db=SimpleNamespace(commit=lambda: None, rollback=lambda: None),
        settings=SimpleNamespace(llm_temperature=0.2, llm_max_tokens_per_request=128),
    )
    monkeypatch.setattr(service, "_conversation_workspace_root", lambda *args, **kwargs: tmp_path)
    frames = [
        frame
        async for frame in service.stream_turn(
            auth=SimpleNamespace(tenant_id=uuid4(), user_id=uuid4()),
            conversation_id=None,
            prompt="Write todo.ts",
        )
    ]

    delta_texts = "".join(
        json.loads(frame.split("data: ", 1)[1].strip())["text"]
        for frame in frames
        if frame.startswith("event: delta")
    )
    assert "<tool_call>" not in delta_texts
    assert "<function=" not in delta_texts
    assert (tmp_path / "todo.ts").read_text() == "export const ok = true;"
