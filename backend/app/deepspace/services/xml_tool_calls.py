"""Extract and strip inline XML/Hermes-style ``<tool_call>`` blocks from model text.

Some models (for example Nemotron, Qwen, or Hermes variants routed through
OpenAI-compatible APIs) occasionally emit tool invocations inside text content
using ``<tool_call><function=name><parameter=k>v</parameter></function></tool_call>``
or ``<tool_call>{"name": ..., "arguments": ...}</tool_call>`` instead of
structured ``delta.tool_calls``.

This helper provides:
- ``XmlToolCallStreamBuffer`` to suppress raw ``<tool_call>...</tool_call>``
  markup from streamed user-facing SSE text while still streaming normal prose
  immediately token-by-token.
- ``extract_inline_tool_calls`` to parse those blocks into standard OpenAI-style
  tool-call dicts and return the clean prose text with tool markup stripped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_OPEN_TAG = "<tool_call>"
_CLOSE_TAG = "</tool_call>"
_OPEN_TAG_RE = re.compile(r"<tool_call(?:\s[^>]*)?>", re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r"</tool_call>", re.IGNORECASE)
_FULL_TOOL_CALL_RE = re.compile(
    r"<tool_call(?:\s[^>]*)?>([\s\S]*?)</tool_call>",
    re.IGNORECASE,
)
_FUNCTION_EQ_RE = re.compile(
    r"<function\s*=\s*([a-zA-Z0-9_.:-]+)[^>]*>([\s\S]*?)</function>",
    re.IGNORECASE,
)
_FUNCTION_NAME_ATTR_RE = re.compile(
    r"""<function\b[^>]*\bname\s*=\s*["']([a-zA-Z0-9_.:-]+)["'][^>]*>([\s\S]*?)</function>""",
    re.IGNORECASE,
)
_PARAM_EQ_RE = re.compile(
    r"<parameter\s*=\s*([a-zA-Z0-9_.-]+)[^>]*>([\s\S]*?)</parameter>",
    re.IGNORECASE,
)
_PARAM_NAME_ATTR_RE = re.compile(
    r"""<parameter\b[^>]*\bname\s*=\s*["']([a-zA-Z0-9_.-]+)["'][^>]*>([\s\S]*?)</parameter>""",
    re.IGNORECASE,
)
_ARG_PAIR_RE = re.compile(
    r"<arg_key>([\s\S]*?)</arg_key>\s*<arg_value>([\s\S]*?)</arg_value>",
    re.IGNORECASE,
)

# Parameters that must stay plain text even if their body looks like JSON or a
# number (for example TS/HTML source files or numeric identifiers as paths).
_STRING_PARAMETERS = frozenset(
    {
        "path",
        "file_path",
        "filepath",
        "new_path",
        "content",
        "text",
        "command",
        "cmd",
        "query",
        "q",
        "pattern",
        "old_string",
        "new_string",
        "url",
        "target",
        "data",
        "task",
        "message",
        "title",
    }
)


def _strip_single_newline_wrapper(value: str) -> str:
    """Remove at most one leading and trailing wrapper newline inside tags."""
    if value.startswith("\r\n"):
        value = value[2:]
    elif value.startswith("\n"):
        value = value[1:]
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    return value


def _coerce_parameter_value(key: str, raw_value: str) -> Any:
    text = _strip_single_newline_wrapper(raw_value)
    lowered_key = key.strip().lower()
    if lowered_key in {"path", "file_path", "filepath", "new_path", "url"}:
        return text.strip()
    if lowered_key in _STRING_PARAMETERS:
        return text

    stripped = text.strip()
    if not stripped:
        return ""
    if stripped in {"true", "false", "null"} or (
        stripped[0] in {"{", "["} and stripped[-1] in {"}", "]"}
    ):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return text
    if re.fullmatch(r"-?\d+", stripped):
        try:
            return int(stripped)
        except ValueError:
            return text
    if re.fullmatch(r"-?\d+\.\d+", stripped):
        try:
            return float(stripped)
        except ValueError:
            return text
    return text


def _parse_single_tool_call_body(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse one ``<tool_call>...</tool_call>`` interior into (name, args) pairs."""
    trimmed = body.strip()
    if not trimmed:
        return []

    parsed: list[tuple[str, dict[str, Any]]] = []

    fn_matches = list(_FUNCTION_EQ_RE.finditer(trimmed)) or list(
        _FUNCTION_NAME_ATTR_RE.finditer(trimmed)
    )
    if fn_matches:
        for fn_match in fn_matches:
            tool_name = fn_match.group(1).strip()
            fn_body = fn_match.group(2)
            args: dict[str, Any] = {}
            param_matches = list(_PARAM_EQ_RE.finditer(fn_body)) or list(
                _PARAM_NAME_ATTR_RE.finditer(fn_body)
            )
            for param_match in param_matches:
                key = param_match.group(1).strip()
                if key:
                    args[key] = _coerce_parameter_value(key, param_match.group(2))
            if not param_matches:
                for arg_match in _ARG_PAIR_RE.finditer(fn_body):
                    key = arg_match.group(1).strip()
                    if key:
                        args[key] = _coerce_parameter_value(key, arg_match.group(2))
            if tool_name:
                parsed.append((tool_name, args))
        return parsed

    # Fallback: GLM/Qwen JSON payload inside <tool_call>...</tool_call>.
    if trimmed.startswith("{"):
        try:
            obj = json.loads(trimmed)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            name = (
                obj.get("name")
                or obj.get("tool")
                or (obj["function"].get("name") if isinstance(obj.get("function"), dict) else None)
            )
            raw_args = (
                obj.get("arguments")
                or obj.get("args")
                or obj.get("parameters")
                or (
                    obj["function"].get("arguments")
                    if isinstance(obj.get("function"), dict)
                    else None
                )
            )
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {}
            if isinstance(name, str) and name.strip():
                args = raw_args if isinstance(raw_args, dict) else {}
                return [(name.strip(), args)]

    return []


@dataclass(slots=True)
class ExtractedToolCallsResult:
    """Result of scanning assistant text for inline XML/JSON tool invocations."""

    cleaned_text: str
    tool_calls: list[dict[str, Any]]


def extract_inline_tool_calls(
    text: str,
    *,
    start_index: int = 0,
    call_id_prefix: str = "xml_call",
) -> ExtractedToolCallsResult:
    """Extract ``<tool_call>...</tool_call>`` calls and strip only matched blocks.

    Non-matching prose is preserved untouched (whitespace collapsed only around
    removed blocks). Unparsed text inside a bogus tag is left in place so no
    legitimate explanation is silently discarded.
    """
    if not text or "<tool_call" not in text.lower():
        return ExtractedToolCallsResult(cleaned_text=text, tool_calls=[])

    tool_calls: list[dict[str, Any]] = []
    out_parts: list[str] = []
    cursor = 0
    next_index = start_index

    for match in _FULL_TOOL_CALL_RE.finditer(text):
        calls = _parse_single_tool_call_body(match.group(1))
        if not calls:
            continue
        out_parts.append(text[cursor : match.start()])
        cursor = match.end()
        for tool_name, args in calls:
            call_id = f"{call_id_prefix}_{next_index}"
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            )
            next_index += 1

    if not tool_calls:
        return ExtractedToolCallsResult(cleaned_text=text, tool_calls=[])

    out_parts.append(text[cursor:])
    cleaned = "".join(out_parts)
    # Collapse excessive blank lines left behind by removed multi-line blocks.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return ExtractedToolCallsResult(cleaned_text=cleaned, tool_calls=tool_calls)


def _trailing_open_prefix_len(text: str) -> int:
    """Return trailing char count matching a non-empty prefix of ``<tool_call``."""
    lower = text.lower()
    needle = "<tool_call"
    max_check = min(len(lower), len(needle))
    for length in range(max_check, 0, -1):
        if lower.endswith(needle[:length]):
            return length
    return 0


class XmlToolCallStreamBuffer:
    """Incremental text filter holding back ``<tool_call>...</tool_call>`` blocks.

    Normal prose is yielded as soon as it is unambiguous (anything before ``<``
    or after a tag prefix fails to match ``<tool_call``). Completed valid
    ``<tool_call>...</tool_call>`` blocks are suppressed from user-visible text
    and accumulated so end-of-round normalization can convert them into tool
    calls cleanly.
    """

    def __init__(self) -> None:
        self._buffer: str = ""
        self._in_tool_call: bool = False
        self._raw_parts: list[str] = []

    @property
    def raw_text(self) -> str:
        return "".join(self._raw_parts)

    def feed(self, chunk: str) -> str:
        """Consume a streamed text delta and return safe user-visible text."""
        if not chunk:
            return ""
        self._raw_parts.append(chunk)
        self._buffer += chunk
        visible_parts: list[str] = []

        while self._buffer:
            if not self._in_tool_call:
                open_match = _OPEN_TAG_RE.search(self._buffer)
                if open_match:
                    if open_match.start() > 0:
                        visible_parts.append(self._buffer[: open_match.start()])
                    self._buffer = self._buffer[open_match.start() :]
                    self._in_tool_call = True
                    continue
                partial_len = _trailing_open_prefix_len(self._buffer)
                safe_len = len(self._buffer) - partial_len
                if safe_len > 0:
                    visible_parts.append(self._buffer[:safe_len])
                    self._buffer = self._buffer[safe_len:]
                break

            close_match = _CLOSE_TAG_RE.search(self._buffer)
            if not close_match:
                break
            candidate_block = self._buffer[: close_match.end()]
            self._buffer = self._buffer[close_match.end() :]
            self._in_tool_call = False
            # Only suppress blocks that actually parse into tool calls.
            inner = candidate_block[candidate_block.find(">") + 1 : -len(_CLOSE_TAG)]
            if not _parse_single_tool_call_body(inner):
                visible_parts.append(candidate_block)

        return "".join(visible_parts)

    def finalize(
        self,
        *,
        start_index: int = 0,
        call_id_prefix: str = "xml_call",
    ) -> tuple[str, ExtractedToolCallsResult]:
        """Flush buffered text on stream completion and extract all tool calls.

        Returns ``(unsent_trailing_text, extraction_result)``. If the stream
        ended without completing any parseable tool call inside a buffered
        prefix, that buffered text is returned as ``unsent_trailing_text`` so
        legitimate content is never swallowed.
        """
        raw = self.raw_text
        extracted = extract_inline_tool_calls(
            raw,
            start_index=start_index,
            call_id_prefix=call_id_prefix,
        )
        trailing = ""
        if self._buffer:
            if self._in_tool_call and extracted.tool_calls:
                # Truncated partial <tool_call> after valid ones: do not leak XML.
                trailing = ""
            elif self._in_tool_call:
                # Check whether there wasn't any valid call; flush literal text.
                trailing = self._buffer
            else:
                trailing = self._buffer
            self._buffer = ""
            self._in_tool_call = False
        return trailing, extracted
