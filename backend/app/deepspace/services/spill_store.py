"""Durable, content-addressed offload for oversized DeepSpace tool output.

A tool result that is too large to carry in the model context is spilled
here instead of being truncated.  The spilled bytes live in the tenant's
object storage under one key per SHA-256 fingerprint, so two spills of the
same bytes share one object, and the model receives a preview plus an opaque
reference it can read back through the ``spill_read`` tool.

The store is deliberately separate from the transient tool-result store.
That store keeps a result in Redis for a quarter of an hour so a resumed
turn can finish reading it; this one keeps a result for as long as the
tenant's retention allows, because an offloaded result is the only remaining
copy of what the tool returned.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.system.services.storage_service import StorageService, StorageServiceError

#: The key segment that separates spilled results from attachment blobs.
SPILL_PREFIX = "spill"

#: The number of characters a spilled result's preview carries.
SPILL_PREVIEW_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class SpillRef:
    """One durable, content-addressed spilled result.

    Attributes:
        sha256: The lowercase hexadecimal SHA-256 fingerprint of the payload.
        size_bytes: The stored byte length.
        bucket: The storage bucket that holds the bytes.
        object_key: The tenant-scoped storage key.
    """

    sha256: str
    size_bytes: int
    bucket: str
    object_key: str

    def as_dict(self) -> dict[str, str | int]:
        """Return the JSON-serializable reference the model receives."""
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "bucket": self.bucket,
            "object_key": self.object_key,
        }


class SpillStoreError(RuntimeError):
    """A spill could not be written or read."""


class SpillStore:
    """Store spilled tool results once per tenant and SHA-256 fingerprint."""

    def __init__(self, settings: Settings) -> None:
        """Create the store for one deployment.

        Args:
            settings: The application settings that choose local or object
                storage.
        """
        self.settings = settings
        self.storage = StorageService(settings)

    @staticmethod
    def fingerprint(payload: bytes) -> str:
        """Return the lowercase SHA-256 fingerprint of one payload.

        Args:
            payload: The spilled bytes.

        Returns:
            The hexadecimal digest.
        """
        return hashlib.sha256(payload).hexdigest()

    def object_key(self, tenant_id: uuid.UUID, sha256: str) -> str:
        """Return the tenant-scoped key for one fingerprint.

        Args:
            tenant_id: The owning tenant.
            sha256: The spilled payload fingerprint.

        Returns:
            The key ``<tenant>/spill/<sha256>``.
        """
        return f"{tenant_id}/{SPILL_PREFIX}/{sha256}"

    def put(
        self,
        *,
        tenant_id: uuid.UUID,
        payload: str,
    ) -> SpillRef:
        """Spill one result, reusing the bytes when the fingerprint exists.

        Args:
            tenant_id: The owning tenant.
            payload: The complete result text.

        Returns:
            The immutable reference for the stored bytes.

        Raises:
            SpillStoreError: The storage backend refused the write.
        """
        raw = payload.encode("utf-8")
        digest = self.fingerprint(raw)
        key = self.object_key(tenant_id, digest)
        try:
            if not self.storage.object_exists(
                tenant_id=tenant_id,
                bucket=self.settings.minio_bucket,
                object_key=key,
            ):
                self.storage.put_object_bytes(
                    tenant_id=tenant_id,
                    object_key=key,
                    content_type="text/plain; charset=utf-8",
                    payload=raw,
                    metadata={"kind": "spill"},
                )
        except StorageServiceError as exc:
            raise SpillStoreError("The spilled result could not be stored.") from exc
        return SpillRef(
            sha256=digest,
            size_bytes=len(raw),
            bucket=self.settings.minio_bucket,
            object_key=key,
        )

    def get(self, *, tenant_id: uuid.UUID, sha256: str) -> str:
        """Read one spilled result back.

        Args:
            tenant_id: The owning tenant.
            sha256: The spilled payload fingerprint.

        Returns:
            The stored text.

        Raises:
            SpillStoreError: The reference is unknown to this tenant or the
                storage backend is unavailable.
        """
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise SpillStoreError("The spilled result reference is invalid.")
        key = self.object_key(tenant_id, sha256)
        try:
            raw = self.storage.get_bytes(
                bucket=self.settings.minio_bucket,
                object_key=key,
            )
        except StorageServiceError as exc:
            raise SpillStoreError("The spilled result is unavailable.") from exc
        return raw.decode("utf-8", errors="replace")


def spill_result(
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    tool_name: str,
    payload: Any,
    threshold_chars: int | None = None,
    max_chars: int | None = None,
) -> tuple[dict[str, Any], SpillRef | None]:
    """Offload one result when it exceeds the model-context ceiling.

    A result at or below the ceiling is returned unchanged.  A larger result
    is written to durable storage, and the model receives a preview plus the
    reference it can read back.

    Args:
        settings: The deployment settings that choose local or object
            storage.
        tenant_id: The owning tenant.
        tool_name: The tool whose result is spilled.
        payload: The tool's result.
        threshold_chars: The character ceiling, taken from the settings when
            omitted.
        max_chars: The largest result that may be stored, taken from the
            settings when omitted.  A tool whose output is legitimately large
            passes its own ceiling.

    Returns:
        The payload the model should receive, and the reference when the
        result was spilled.

    Raises:
        SpillStoreError: When the result is larger than the ceiling the
            store accepts, or when the storage backend refused the write.
            The caller turns the refusal into a result the model can read,
            so one oversized result does not end a turn.
    """
    threshold = int(
        threshold_chars
        if threshold_chars is not None
        else getattr(settings, "deepspace_spill_threshold_chars", 24_000)
    )
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    if len(text) <= threshold:
        return payload, None
    ceiling = int(
        max_chars
        if max_chars is not None
        else getattr(settings, "deepspace_spill_max_chars", 2_000_000)
    )
    if len(text.encode("utf-8")) > ceiling:
        raise SpillStoreError("The spilled result exceeds the deployment's spill ceiling.")
    store = SpillStore(settings)
    reference = store.put(tenant_id=tenant_id, payload=text)
    return (
        {
            "status": "spilled",
            "tool_name": tool_name,
            "spill_ref": reference.sha256,
            "size_bytes": reference.size_bytes,
            "preview": text[:SPILL_PREVIEW_CHARS],
            "preview_truncated": len(text) > SPILL_PREVIEW_CHARS,
            "message": (
                "The complete result is stored. Read it with spill_read using the "
                "spill_ref, or use the preview above when the excerpt is enough."
            ),
        },
        reference,
    )


__all__ = [
    "SPILL_PREFIX",
    "SPILL_PREVIEW_CHARS",
    "SpillRef",
    "SpillStore",
    "SpillStoreError",
    "spill_result",
]
