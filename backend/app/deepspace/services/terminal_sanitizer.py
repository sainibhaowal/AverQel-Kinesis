"""Streaming terminal-control sanitizer for DeepSpace.

Python-native port of the AverQel KineSis TerminalSanitizer. Removes
CSI/OSC/short escape sequences while preserving split-sequence carry.
"""

from __future__ import annotations

PROMPT_MARKER_PREFIX = "133;D;"
CONTROLLED_PROMPT = "dsh> "


class SanitizedChunk:
    def __init__(self, text: str, prompt: bool, prompt_tail: str | None = None) -> None:
        self.text = text
        self.prompt = prompt
        self.prompt_tail = prompt_tail


class TerminalSanitizer:
    def __init__(self, max_pending_bytes: int) -> None:
        self._max_pending_bytes = max_pending_bytes
        self._pending = ""
        self._discard_mode: str | None = None  # 'osc' | 'csi'
        self._discard_osc_escape = False
        self._trailing_cr = False
        self._tracking_prompt_tail = False

    def push(self, chunk: str) -> SanitizedChunk:
        # discard prefix
        if self._discard_mode is not None:
            chunk = self._discard_prefix(chunk)
        self._pending += chunk
        text = ""
        prompt = False
        include_prompt_tail = self._tracking_prompt_tail
        prompt_tail = ""
        index = 0

        def append_text(value: str) -> None:
            nonlocal text, prompt_tail
            text += value
            if self._tracking_prompt_tail:
                prompt_tail += value

        while index < len(self._pending):
            escape = self._pending.find("\x1b", index)
            if escape < 0:
                append_text(self._pending[index:])
                index = len(self._pending)
                break
            append_text(self._pending[index:escape])
            if escape + 1 >= len(self._pending):
                index = escape
                break
            kind = self._pending[escape + 1]
            if kind == "]":
                bel = self._pending.find("\x07", escape + 2)
                st = self._pending.find("\x1b\\", escape + 2)
                end = -1
                if bel >= 0 and st >= 0:
                    end = min(bel + 1, st + 2)
                elif bel >= 0:
                    end = bel + 1
                elif st >= 0:
                    end = st + 2
                if end < 0:
                    index = escape
                    break
                terminator_bytes = 1 if self._pending[end - 1] == "\x07" else 2
                content = self._pending[escape + 2 : end - terminator_bytes]
                if content.startswith(PROMPT_MARKER_PREFIX):
                    prompt = True
                    self._tracking_prompt_tail = True
                    include_prompt_tail = True
                    prompt_tail = ""
                index = end
                continue
            if kind == "[":
                end = escape + 2
                while end < len(self._pending):
                    code = ord(self._pending[end])
                    if 0x40 <= code <= 0x7E:
                        break
                    end += 1
                if end >= len(self._pending):
                    index = escape
                    break
                index = end + 1
                continue
            index = escape + 2

        self._pending = self._pending[index:]
        self._enforce_pending_bound()
        normalized = self._normalize_text(text)
        result = SanitizedChunk(text=normalized, prompt=prompt)
        if include_prompt_tail:
            result.prompt_tail = prompt_tail
        return result

    def flush(self) -> str:
        text = "" if self._pending.startswith("\x1b") else self._pending
        self._pending = ""
        self._discard_mode = None
        self._discard_osc_escape = False
        self._tracking_prompt_tail = False
        normalized = self._normalize_text(text)
        if self._trailing_cr:
            self._trailing_cr = False
            return normalized + "\n"
        return normalized

    def _normalize_text(self, text: str) -> str:
        complete = ("\r" + text) if self._trailing_cr else text
        self._trailing_cr = False
        if complete.endswith("\r"):
            complete = complete[:-1]
            self._trailing_cr = True
        return normalize_terminal_text(complete)

    def _enforce_pending_bound(self) -> None:
        if len(self._pending.encode("utf-8")) <= self._max_pending_bytes:
            return
        self._discard_mode = "osc" if len(self._pending) > 1 and self._pending[1] == "]" else "csi"
        self._pending = ""

    def _discard_prefix(self, chunk: str) -> str:
        if self._discard_mode == "csi":
            for i in range(len(chunk)):
                code = ord(chunk[i])
                if 0x40 <= code <= 0x7E:
                    self._discard_mode = None
                    return chunk[i + 1 :]
            return ""
        # osc
        index = 0
        if self._discard_osc_escape:
            self._discard_osc_escape = False
            if chunk.startswith("\\"):
                self._discard_mode = None
                return chunk[1:]
        while index < len(chunk):
            if chunk[index] == "\x07":
                self._discard_mode = None
                return chunk[index + 1 :]
            if chunk[index] == "\x1b":
                if index + 1 < len(chunk) and chunk[index + 1] == "\\":
                    self._discard_mode = None
                    return chunk[index + 2 :]
                if index + 1 == len(chunk):
                    self._discard_osc_escape = True
            index += 1
        return ""


def normalize_terminal_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x07", "")
