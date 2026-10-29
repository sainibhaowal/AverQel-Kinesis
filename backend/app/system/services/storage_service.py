from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import boto3  # type: ignore[import-untyped]
from botocore.client import BaseClient  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from app.core.config import Settings

logger = logging.getLogger(__name__)

_FILENAME_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_FALLBACK_FILENAME = "file"


class StorageServiceError(Exception):
    def __init__(self, *, code: str, message: str, retryable: bool) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


@dataclass(slots=True)
class StoredObject:
    bucket: str
    object_key: str
    etag: str
    size_bytes: int
    content_type: str


def _safe_filename(filename: str) -> str:
    normalized = filename.strip().replace("\\", "/").split("/")[-1]
    normalized = _FALLBACK_FILENAME if not normalized else normalized
    normalized = _FILENAME_SAFE_PATTERN.sub("_", normalized)
    return normalized or _FALLBACK_FILENAME


class StorageService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: BaseClient | None = None
        self._bucket_verified = False

    @property
    def is_local(self) -> bool:
        backend = getattr(self.settings, "storage_backend", "local")
        endpoint = (self.settings.minio_endpoint or "").strip().lower()
        return backend == "local" or endpoint in {"local", "", "none", "filesystem"}

    @property
    def local_root(self) -> Path:
        raw_path = getattr(self.settings, "local_storage_path", None)
        if not raw_path or raw_path == "./data/storage":
            from app.core.averqel_home import averqel_storage_path

            path = averqel_storage_path()
        else:
            path = Path(raw_path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def is_tenant_object_key(*, tenant_id: uuid.UUID, object_key: str) -> bool:
        """Verify the canonical tenant prefix without accepting path traversal."""
        key = str(object_key).replace("\\", "/")
        prefix = f"{tenant_id}/"
        return key.startswith(prefix) and ".." not in key.split("/")

    def get_tenant_bytes(self, *, tenant_id: uuid.UUID, bucket: str, object_key: str) -> bytes:
        """Tenant-guarded read for new lifecycle/archive integrations.

        Existing legacy reads remain unchanged until their stored keys are
        individually verified; callers using this boundary cannot cross a
        tenant prefix.
        """
        if not self.is_tenant_object_key(tenant_id=tenant_id, object_key=object_key):
            raise StorageServiceError(
                code="STORAGE_TENANT_PREFIX_MISMATCH",
                message="Object storage key is not owned by this tenant.",
                retryable=False,
            )
        return self.get_bytes(bucket=bucket, object_key=object_key)

    def delete_tenant_object(
        self, *, tenant_id: uuid.UUID, bucket: str, object_key: str, raise_on_error: bool = False
    ) -> None:
        if not self.is_tenant_object_key(tenant_id=tenant_id, object_key=object_key):
            raise StorageServiceError(
                code="STORAGE_TENANT_PREFIX_MISMATCH",
                message="Object storage key is not owned by this tenant.",
                retryable=False,
            )
        self.delete_object(bucket=bucket, object_key=object_key, raise_on_error=raise_on_error)

    @staticmethod
    def _write_atomic(target: Path, payload: bytes) -> None:
        """Write bytes through a temporary sibling and rename them into place.

        Args:
            target: The final path.
            payload: The bytes to write.
        """
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, target)

    def put_bytes(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        filename: str,
        content_type: str,
        payload: bytes,
    ) -> StoredObject:
        """Store one uploaded document under its tenant and file id."""
        safe_filename = _safe_filename(filename)
        object_key = f"{tenant_id}/{document_id}/{safe_filename}"
        return self.put_object_bytes(
            tenant_id=tenant_id,
            object_key=object_key,
            content_type=content_type,
            payload=payload,
            metadata={"document_id": str(document_id)},
        )

    def put_object_bytes(
        self,
        *,
        tenant_id: uuid.UUID,
        object_key: str,
        content_type: str,
        payload: bytes,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        """Store bytes at an explicit tenant-scoped key.

        Content-addressed callers use this to keep one object per SHA-256
        fingerprint instead of one object per document.  A local write is
        atomic: the bytes land in a temporary sibling first, then replace the
        target, so a crashed write never leaves a partial object.

        Args:
            tenant_id: The owning tenant; the key must carry its prefix.
            object_key: The tenant-scoped object key.
            content_type: The stored content type.
            payload: The bytes to store.
            metadata: Optional extra object metadata.

        Returns:
            The stored object.

        Raises:
            StorageServiceError: The key escapes the tenant prefix or the
                storage backend is unavailable.
        """
        if not self.is_tenant_object_key(tenant_id=tenant_id, object_key=object_key):
            raise StorageServiceError(
                code="STORAGE_KEY_OUTSIDE_TENANT",
                message="Object key must stay inside the tenant prefix.",
                retryable=False,
            )

        digest = hashlib.sha256(payload).hexdigest()
        if self.is_local:
            bucket_root = self.local_root / self.settings.minio_bucket
            target_path = bucket_root / object_key
            target_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path = bucket_root / f"{tenant_id}/attachments/{digest}"
            if target_path == blob_path:
                if not target_path.is_file():
                    self._write_atomic(target_path, payload)
            else:
                blob_path.parent.mkdir(parents=True, exist_ok=True)
                if not blob_path.is_file():
                    self._write_atomic(blob_path, payload)
                if not target_path.is_file():
                    try:
                        os.link(blob_path, target_path)
                    except OSError:
                        # Filesystems without hard links keep a private copy.
                        self._write_atomic(target_path, payload)
            return StoredObject(
                bucket=self.settings.minio_bucket,
                object_key=object_key,
                etag=digest,
                size_bytes=len(payload),
                content_type=content_type,
            )

        client = self._get_client()
        self._ensure_bucket(client)
        object_metadata = {"tenant_id": str(tenant_id), "sha256": digest}
        if metadata:
            object_metadata.update(metadata)

        try:
            response = client.put_object(
                Bucket=self.settings.minio_bucket,
                Key=object_key,
                Body=payload,
                ContentType=content_type,
                Metadata=object_metadata,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.warning(
                "Object storage put failed.",
                extra={"error_type": type(exc).__name__},
            )
            raise StorageServiceError(
                code="STORAGE_UNAVAILABLE",
                message="Unable to store document in object storage.",
                retryable=True,
            ) from exc

        etag = str(response.get("ETag", "")).strip('"')
        return StoredObject(
            bucket=self.settings.minio_bucket,
            object_key=object_key,
            etag=etag,
            size_bytes=len(payload),
            content_type=content_type,
        )

    def object_exists(
        self,
        *,
        tenant_id: uuid.UUID,
        bucket: str,
        object_key: str,
    ) -> bool:
        """Report whether one tenant-scoped object already exists.

        Args:
            tenant_id: The owning tenant.
            bucket: The bucket that holds the object.
            object_key: The tenant-scoped object key.

        Returns:
            True when the object exists.
        """
        if not self.is_tenant_object_key(tenant_id=tenant_id, object_key=object_key):
            return False
        if self.is_local:
            return (self.local_root / bucket / object_key).is_file()
        client = self._get_client()
        try:
            client.head_object(Bucket=bucket, Key=object_key)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in {"NoSuchKey", "NoSuchBucket", "NotFound", "404"}:
                return False
            raise StorageServiceError(
                code="STORAGE_UNAVAILABLE",
                message="Unable to inspect an object in object storage.",
                retryable=True,
            ) from exc
        except BotoCoreError as exc:
            raise StorageServiceError(
                code="STORAGE_UNAVAILABLE",
                message="Unable to inspect an object in object storage.",
                retryable=True,
            ) from exc
        return True

    def prune_unreferenced_blob(self, *, tenant_id: uuid.UUID, sha256: str) -> bool:
        """Delete one blob when its last document link is gone.

        Args:
            tenant_id: The owning tenant.
            sha256: The blob fingerprint.

        Returns:
            True when the blob was removed.
        """
        if not self.is_local:
            return False
        try:
            blob = (
                self.local_root
                / self.settings.minio_bucket
                / str(tenant_id)
                / "attachments"
                / sha256
            )
            if not blob.is_file():
                return False
            if blob.stat().st_nlink > 1:
                return False
            blob.unlink()
        except OSError:
            # The document row is already gone; cleanup must not fail the request.
            return False
        return True

    def prune_unreferenced_blobs(self, *, tenant_id: uuid.UUID) -> int:
        """Delete deduplication blobs that no per-file link references.

        Local uploads keep one content-addressed blob under ``attachments/`` and a
        hard link at each document key.  Removing a document drops its link;
        the blob stays only while another link points at it.  This method
        removes the blobs whose last document link is gone.

        Args:
            tenant_id: The owning tenant.

        Returns:
            The number of blobs removed.
        """
        if not self.is_local:
            return 0
        removed = 0
        try:
            directory = (
                self.local_root / self.settings.minio_bucket / str(tenant_id) / "attachments"
            )
            if not directory.is_dir():
                return 0
            for blob in directory.iterdir():
                if not blob.is_file():
                    continue
                try:
                    if blob.stat().st_nlink <= 1:
                        blob.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            # The rows are already gone; cleanup must not fail the request.
            return removed
        return removed

    def get_bytes(self, *, bucket: str, object_key: str) -> bytes:
        if self.is_local:
            target_path = self.local_root / bucket / object_key
            if not target_path.is_file():
                raise StorageServiceError(
                    code="STORAGE_OBJECT_NOT_FOUND",
                    message="Object storage payload is missing.",
                    retryable=False,
                )
            return target_path.read_bytes()

        client = self._get_client()
        body = None
        try:
            response = client.get_object(Bucket=bucket, Key=object_key)
            body = response["Body"]
            data = body.read()
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in {"NoSuchKey", "NoSuchBucket", "NotFound"}:
                raise StorageServiceError(
                    code="STORAGE_OBJECT_NOT_FOUND",
                    message="Object storage payload is missing.",
                    retryable=False,
                ) from exc
            raise StorageServiceError(
                code="STORAGE_UNAVAILABLE",
                message="Unable to read document from object storage.",
                retryable=True,
            ) from exc
        except BotoCoreError as exc:
            raise StorageServiceError(
                code="STORAGE_UNAVAILABLE",
                message="Unable to read document from object storage.",
                retryable=True,
            ) from exc
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:  # noqa: BLE001
                    logger.debug("Failed to close object storage response body.", exc_info=True)

        return cast(bytes, data)

    def put_upload_chunk(
        self,
        *,
        tenant_id: uuid.UUID,
        upload_id: uuid.UUID,
        chunk_index: int,
        payload: bytes,
    ) -> StoredObject:
        """Store a private, temporary upload chunk under a tenant-scoped key."""
        object_key = f"{tenant_id}/library-uploads/{upload_id}/chunk-{chunk_index:08d}"

        if self.is_local:
            target_path = self.local_root / self.settings.minio_bucket / object_key
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(payload)
            etag = hashlib.sha256(payload).hexdigest()
            return StoredObject(
                bucket=self.settings.minio_bucket,
                object_key=object_key,
                etag=etag,
                size_bytes=len(payload),
                content_type="application/octet-stream",
            )

        client = self._get_client()
        self._ensure_bucket(client)
        try:
            response = client.put_object(
                Bucket=self.settings.minio_bucket,
                Key=object_key,
                Body=payload,
                ContentType="application/octet-stream",
                Metadata={
                    "tenant_id": str(tenant_id),
                    "upload_id": str(upload_id),
                    "chunk_index": str(chunk_index),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageServiceError(
                code="STORAGE_UNAVAILABLE",
                message="Unable to store the upload chunk.",
                retryable=True,
            ) from exc
        return StoredObject(
            bucket=self.settings.minio_bucket,
            object_key=object_key,
            etag=str(response.get("ETag", "")).strip('"'),
            size_bytes=len(payload),
            content_type="application/octet-stream",
        )

    def get_upload_chunk(
        self, *, tenant_id: uuid.UUID, upload_id: uuid.UUID, chunk_index: int
    ) -> bytes:
        return self.get_bytes(
            bucket=self.settings.minio_bucket,
            object_key=f"{tenant_id}/library-uploads/{upload_id}/chunk-{chunk_index:08d}",
        )

    def delete_upload_chunks(
        self, *, tenant_id: uuid.UUID, upload_id: uuid.UUID, total_chunks: int
    ) -> None:
        for index in range(max(0, total_chunks)):
            self.delete_object(
                bucket=self.settings.minio_bucket,
                object_key=f"{tenant_id}/library-uploads/{upload_id}/chunk-{index:08d}",
            )

    def get_stream(self, *, bucket: str, object_key: str):
        """Return a stream for the object."""
        if self.is_local:
            target_path = self.local_root / bucket / object_key
            if not target_path.is_file():
                raise StorageServiceError(
                    code="STORAGE_OBJECT_NOT_FOUND",
                    message="Object storage payload is missing.",
                    retryable=False,
                )
            return open(target_path, "rb")

        client = self._get_client()
        try:
            response = client.get_object(Bucket=bucket, Key=object_key)
            return response["Body"]
        except (BotoCoreError, ClientError) as exc:
            logger.warning(
                "Object storage stream failed.",
                extra={"error_type": type(exc).__name__},
            )
            raise StorageServiceError(
                code="STORAGE_UNAVAILABLE",
                message="Unable to stream document from object storage.",
                retryable=True,
            ) from exc

    def delete_object(
        self,
        *,
        bucket: str,
        object_key: str,
        raise_on_error: bool = False,
    ) -> None:
        if self.is_local:
            target_path = self.local_root / bucket / object_key
            if target_path.is_file():
                try:
                    target_path.unlink()
                except Exception as exc:
                    if raise_on_error:
                        raise StorageServiceError(
                            code="STORAGE_UNAVAILABLE",
                            message="Unable to delete document from object storage.",
                            retryable=True,
                        ) from exc
            return

        client = self._get_client()
        try:
            client.delete_object(Bucket=bucket, Key=object_key)
        except (BotoCoreError, ClientError) as exc:
            logger.warning(
                "Object storage delete failed.",
                extra={"error_type": type(exc).__name__},
            )
            if raise_on_error:
                raise StorageServiceError(
                    code="STORAGE_UNAVAILABLE",
                    message="Unable to delete document from object storage.",
                    retryable=True,
                ) from exc

    def copy_object(
        self,
        *,
        bucket: str,
        source_key: str,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        filename: str,
        content_type: str,
    ) -> StoredObject:
        """Copy an object inside the private bucket without exposing it publicly."""
        safe_filename = _safe_filename(filename)
        object_key = f"{tenant_id}/{document_id}/{safe_filename}"

        if self.is_local:
            src_path = self.local_root / bucket / source_key
            if not src_path.is_file():
                raise StorageServiceError(
                    code="STORAGE_UNAVAILABLE",
                    message="Unable to copy the Library object.",
                    retryable=True,
                )
            data = src_path.read_bytes()
            return self.put_object_bytes(
                tenant_id=tenant_id,
                object_key=object_key,
                content_type=content_type,
                payload=data,
                metadata={"document_id": str(document_id)},
            )

        client = self._get_client()
        self._ensure_bucket(client)
        try:
            client.copy_object(
                Bucket=bucket,
                Key=object_key,
                CopySource={"Bucket": bucket, "Key": source_key},
                ContentType=content_type,
                MetadataDirective="REPLACE",
                Metadata={"tenant_id": str(tenant_id), "document_id": str(document_id)},
            )
            head = client.head_object(Bucket=bucket, Key=object_key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageServiceError(
                code="STORAGE_UNAVAILABLE",
                message="Unable to copy the Library object.",
                retryable=True,
            ) from exc
        return StoredObject(
            bucket=bucket,
            object_key=object_key,
            etag=str(head.get("ETag", "")).strip('"'),
            size_bytes=int(head.get("ContentLength", 0)),
            content_type=content_type,
        )

    def _ensure_bucket(self, client: BaseClient | None = None) -> None:
        if self._bucket_verified:
            return

        if self.is_local:
            (self.local_root / self.settings.minio_bucket).mkdir(parents=True, exist_ok=True)
            self._bucket_verified = True
            return

        if client is None:
            client = self._get_client()

        try:
            client.head_bucket(Bucket=self.settings.minio_bucket)
            self._bucket_verified = True
            return
        except ClientError as exc:
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            if status not in {403, 404}:
                raise StorageServiceError(
                    code="STORAGE_UNAVAILABLE",
                    message="Unable to verify object storage bucket.",
                    retryable=True,
                ) from exc

        try:
            client.create_bucket(Bucket=self.settings.minio_bucket)
            self._bucket_verified = True
        except ClientError as exc:
            raise StorageServiceError(
                code="STORAGE_UNAVAILABLE",
                message="Unable to create object storage bucket.",
                retryable=True,
            ) from exc

    def _get_client(self) -> BaseClient:
        if self._client is not None:
            return self._client

        endpoint = (self.settings.minio_endpoint or "").strip()
        if not endpoint:
            raise StorageServiceError(
                code="STORAGE_UNAVAILABLE",
                message="Object storage endpoint is not configured.",
                retryable=False,
            )

        if not endpoint.startswith(("http://", "https://")):
            scheme = "https" if self.settings.minio_secure else "http"
            endpoint = f"{scheme}://{endpoint}"

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=self.settings.minio_access_key,
            aws_secret_access_key=self.settings.minio_secret_key,
            use_ssl=self.settings.minio_secure,
            verify=(self.settings.minio_verify_ssl if self.settings.minio_secure else False),
        )
        return self._client
