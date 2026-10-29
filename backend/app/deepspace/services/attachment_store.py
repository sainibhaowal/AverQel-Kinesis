"""Content-addressed attachment blobs for DeepSpace.

Every attachment is stored once per tenant and SHA-256 fingerprint.  Two
uploads of the same bytes therefore share one object, and the reference the
session log records is the fingerprint rather than a per-upload path.  The
store uses the configured local storage directory when object storage is not
set up, so a local-first install needs no extra service.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from app.core.config import Settings
from app.system.services.storage_service import StorageService

#: The key segment that separates attachment blobs from document uploads.
ATTACHMENT_PREFIX = "attachments"


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """One immutable content-addressed attachment.

    Attributes:
        sha256: The lowercase hexadecimal SHA-256 fingerprint of the bytes.
        size_bytes: The stored byte length.
        content_type: The media type supplied by the uploader.
        bucket: The storage bucket that holds the bytes.
        object_key: The tenant-scoped storage key.
    """

    sha256: str
    size_bytes: int
    content_type: str
    bucket: str
    object_key: str

    def as_dict(self) -> dict[str, str | int]:
        """Return the JSON-serializable reference the session log stores."""
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "bucket": self.bucket,
            "object_key": self.object_key,
        }


class ContentAddressedAttachmentStore:
    """Store attachment bytes once per tenant and SHA-256 fingerprint."""

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
            payload: The attachment bytes.

        Returns:
            The hexadecimal digest.
        """
        return hashlib.sha256(payload).hexdigest()

    def object_key(self, tenant_id: uuid.UUID, sha256: str) -> str:
        """Return the tenant-scoped key for one fingerprint.

        Args:
            tenant_id: The owning tenant.
            sha256: The attachment fingerprint.

        Returns:
            The key ``<tenant>/attachments/<sha256>``.
        """
        return f"{tenant_id}/{ATTACHMENT_PREFIX}/{sha256}"

    def exists(self, *, tenant_id: uuid.UUID, sha256: str) -> bool:
        """Report whether one fingerprint is already stored.

        Args:
            tenant_id: The owning tenant.
            sha256: The attachment fingerprint.

        Returns:
            True when the bytes are already present.
        """
        return self.storage.object_exists(
            tenant_id=tenant_id,
            bucket=self.settings.minio_bucket,
            object_key=self.object_key(tenant_id, sha256),
        )

    def put(
        self,
        *,
        tenant_id: uuid.UUID,
        payload: bytes,
        content_type: str,
    ) -> AttachmentRef:
        """Store one payload, reusing the bytes when the fingerprint exists.

        Args:
            tenant_id: The owning tenant.
            payload: The attachment bytes.
            content_type: The media type supplied by the uploader.

        Returns:
            The immutable reference for the stored bytes.
        """
        digest = self.fingerprint(payload)
        key = self.object_key(tenant_id, digest)
        if not self.storage.object_exists(
            tenant_id=tenant_id,
            bucket=self.settings.minio_bucket,
            object_key=key,
        ):
            self.storage.put_object_bytes(
                tenant_id=tenant_id,
                object_key=key,
                content_type=content_type,
                payload=payload,
                metadata={"kind": "attachment"},
            )
        return AttachmentRef(
            sha256=digest,
            size_bytes=len(payload),
            content_type=content_type,
            bucket=self.settings.minio_bucket,
            object_key=key,
        )

    def get(self, ref: AttachmentRef) -> bytes:
        """Read one stored attachment back.

        Args:
            ref: The immutable reference returned by :meth:`put`.

        Returns:
            The stored bytes.

        Raises:
            StorageServiceError: The object is missing or the backend is
                unavailable.
        """
        return self.storage.get_bytes(bucket=ref.bucket, object_key=ref.object_key)


__all__ = [
    "ATTACHMENT_PREFIX",
    "AttachmentRef",
    "ContentAddressedAttachmentStore",
]
