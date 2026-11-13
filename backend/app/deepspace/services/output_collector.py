"""Bounded output collection with spill recovery for DeepSpace.

Python-native port of the AverQel KineSis OutputCollector. Collects one
stream with a bounded in-memory tail; with spill options, on first overflow
a spill file is created and every chunk is appended there while the full
stream remains within the cap.
"""

from __future__ import annotations

import os
import random
import string
from dataclasses import dataclass
from typing import Any


@dataclass
class CollectedOutput:
    text: str
    truncated: bool
    spill_path: str | None = None


@dataclass
class SpillOptions:
    max_bytes: int
    directory: str
    on_failure: Any = None


@dataclass
class OutputRead:
    text: str
    next_offset: int
    lossy: bool
    spill_path: str | None = None


class OutputCollector:
    """Collects one stream with a bounded in-memory tail and optional spill."""

    def __init__(
        self,
        max_bytes: int,
        label: str,
        spill: SpillOptions | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._label = label
        self._spill = spill
        self._spill_disabled = spill is None
        self._chunks: list[bytes] = []
        self._bytes = 0
        self._dropped = False
        self._spill_fd: int | None = None
        self._spill_file: str | None = None
        self._total = 0
        self._spill_reported = False

    def push(self, chunk: bytes) -> None:
        self._total += len(chunk)
        overflows = self._bytes + len(chunk) > self._max_bytes
        spill = self._spill
        if (
            spill is not None
            and not self._spill_disabled
            and (overflows or self._spill_fd is not None)
        ):
            self._spill_all(spill, chunk)
        self._chunks.append(chunk)
        self._bytes += len(chunk)
        while self._bytes > self._max_bytes:
            head = self._chunks[0]
            excess = self._bytes - self._max_bytes
            if len(head) <= excess:
                self._chunks.pop(0)
                self._bytes -= len(head)
            else:
                self._chunks[0] = head[excess:]
                self._bytes -= excess
            self._dropped = True

    def _spill_all(self, spill: SpillOptions, chunk: bytes) -> None:
        if self._total > spill.max_bytes:
            self._discard_spill()
            return
        try:
            if self._spill_fd is None:
                suffix = "".join(
                    random.choice(string.ascii_lowercase + string.digits) for _ in range(12)
                )
                file = os.path.join(
                    spill.directory,
                    f"dsh-subprocess-{os.getpid()}-{suffix}-{self._label}.log",
                )
                fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self._spill_file = file
                self._spill_fd = fd
                for prior in self._chunks:
                    os.write(fd, prior)
            os.write(self._spill_fd, chunk)
        except OSError as exc:
            self._discard_spill()
            if spill.on_failure is not None and not self._spill_reported:
                self._spill_reported = True
                try:
                    spill.on_failure(exc, self._label)
                except Exception:
                    pass

    def _discard_spill(self) -> None:
        fd = self._spill_fd
        file = self._spill_file
        self._spill_fd = None
        self._spill_file = None
        self._spill_disabled = True
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                self._spill_fd = fd
        if file is not None:
            try:
                os.unlink(file)
            except OSError:
                pass

    def read_from(self, from_byte: int) -> OutputRead:
        window_start = self._total - self._bytes
        buffer = b"".join(self._chunks)
        lossy = from_byte < window_start
        if lossy:
            slice_data = buffer
        else:
            slice_data = buffer[from_byte - window_start :]
        return OutputRead(
            text=slice_data.decode("utf-8", errors="replace"),
            next_offset=self._total,
            lossy=lossy,
            spill_path=self._spill_file,
        )

    def snapshot(self) -> tuple[bytes, int]:
        return b"".join(self._chunks), self._total

    def seal(self) -> None:
        if self._spill_fd is None:
            return
        try:
            os.close(self._spill_fd)
        except OSError:
            self._spill_file = None
        self._spill_fd = None

    def finalize(self) -> CollectedOutput:
        self.seal()
        return CollectedOutput(
            text=b"".join(self._chunks).decode("utf-8", errors="replace"),
            truncated=self._dropped,
            spill_path=self._spill_file,
        )
