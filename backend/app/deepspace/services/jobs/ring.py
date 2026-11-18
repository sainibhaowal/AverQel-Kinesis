"""Bounded UTF-8 output ring buffer for DeepSpace Jobs."""

from __future__ import annotations

from dataclasses import dataclass

from app.deepspace.services.jobs.brand import JobChannel
from app.deepspace.services.jobs.view import JobChunk


@dataclass(frozen=True, slots=True)
class _RingChunk:
    text: str
    offset: int
    channel: JobChannel | None
    gap_before: bool


class OutputRing:
    """
    A bounded ring buffer for job output.

    - Appends land whole (no partial chunks)
    - Retention is by UTF-8 byte length
    - Model cursor advances only on consuming read
    - Observers read at absolute offsets without moving cursor
    - Settlement trims to settled cap but never below unconsumed bytes
    """

    def __init__(self, retain_bytes: int = 256 * 1024) -> None:
        self._retain_bytes = retain_bytes
        self._chunks: list[_RingChunk] = []
        self._total = 0
        self._earliest = 0
        self._model_cursor = 0

    @property
    def total(self) -> int:
        return self._total

    @property
    def earliest(self) -> int:
        return self._earliest

    @property
    def model_cursor(self) -> int:
        return self._model_cursor

    def append(
        self,
        text: str,
        channel: JobChannel | None = None,
        gap_before: bool = False,
        retain_bytes: int | None = None,
    ) -> bool:
        """Append a chunk. Returns False if chunk was empty."""
        if not text:
            return False

        chunk_bytes = len(text.encode("utf-8"))
        chunk = _RingChunk(
            text=text,
            offset=self._total,
            channel=channel,
            gap_before=gap_before,
        )
        self._chunks.append(chunk)
        self._total += chunk_bytes

        # Trim to retention cap
        self._trim(retain_bytes or self._retain_bytes)
        return True

    def _trim(self, retain_bytes: int) -> None:
        """Drop oldest chunks until retained bytes <= cap."""
        if self._total - self._earliest <= retain_bytes:
            return

        # Find the first chunk whose end offset > total - retain_bytes
        target = self._total - retain_bytes
        while self._chunks:
            first = self._chunks[0]
            chunk_end = first.offset + len(first.text.encode("utf-8"))
            if chunk_end > target:
                break
            # Drop this chunk
            self._earliest = chunk_end
            self._chunks.pop(0)

    def read_from_model_cursor(self) -> tuple[tuple[JobChunk, ...], bool]:
        """Consuming read: returns chunks since model_cursor, advances cursor."""
        return self._read_from(self._model_cursor, advance_cursor=True)

    def read_from(self, offset: int) -> tuple[tuple[JobChunk, ...], bool, int]:
        """Non-consuming read: returns chunks overlapping [offset, total)."""
        chunks, lossy = self._read_from(offset, advance_cursor=False)
        return chunks, lossy, self._total

    def _read_from(self, offset: int, advance_cursor: bool) -> tuple[tuple[JobChunk, ...], bool]:
        if offset < self._earliest:
            lossy = True
            offset = self._earliest
        else:
            lossy = False

        if offset >= self._total:
            return (), lossy

        result: list[JobChunk] = []
        for chunk in self._chunks:
            chunk_end = chunk.offset + len(chunk.text.encode("utf-8"))
            if chunk_end <= offset:
                continue
            if chunk.offset >= self._total:
                break

            # Calculate slice within this chunk
            chunk_bytes = chunk.text.encode("utf-8")
            start = max(0, offset - chunk.offset)
            end = len(chunk_bytes)
            sliced_text = chunk_bytes[start:end].decode("utf-8")

            result.append(
                JobChunk(
                    text=sliced_text,
                    offset=chunk.offset + start,
                    channel=chunk.channel,
                    gap_before=chunk.gap_before if start == 0 else False,
                )
            )

        if advance_cursor:
            self._model_cursor = self._total

        return tuple(result), lossy

    def trim_to_settled(self, settled_retain_bytes: int, model_cursor: int) -> None:
        """Trim ring at settlement: keep settled cap but never below unconsumed bytes."""
        min_retain = self._total - model_cursor
        effective_cap = max(settled_retain_bytes, min_retain)
        self._trim(effective_cap)


__all__ = ["OutputRing"]
