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
import logging
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.core.config import Settings
from app.deepspace.services.spill_policy import result_preview
from app.system.services.storage_service import StorageService, StorageServiceError

#: The key segment that separates spilled results from attachment blobs.
SPILL_PREFIX = "spill"

#: The number of characters a spilled result's preview carries.
SPILL_PREVIEW_CHARS = 4_000
SPILL_PREVIEW_TOKENS = 1_024
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LOCAL_CLEANUP_LOCK = threading.Lock()
_CLEANUP_LAST_RUN: dict[str, float] = {}
_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
_CLEANUP_BATCH_SIZE = 1_000
logger = logging.getLogger(__name__)


class SpillBackend(Protocol):
    """Storage contract used by the spill service."""

    def put(self, *, tenant_id: uuid.UUID, digest: str, payload: bytes) -> str: ...

    def get(self, *, tenant_id: uuid.UUID, digest: str) -> bytes: ...

    def cleanup_expired(self, *, older_than: float) -> int: ...


class ObjectStorageSpillBackend:
    """Tenant-prefixed durable objects using the application storage service."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = StorageService(settings)

    def _local_path(self, object_key: str, *, create: bool = True) -> Path:
        parts = Path(object_key).parts
        if len(parts) != 3 or parts[1] != SPILL_PREFIX or not _DIGEST_PATTERN.fullmatch(parts[2]):
            raise SpillStoreError("The local spill object key is invalid.")
        try:
            uuid.UUID(parts[0])
        except ValueError as exc:
            raise SpillStoreError("The local spill tenant is invalid.") from exc
        bucket_root = self.storage.local_root / self.settings.minio_bucket
        if create:
            bucket_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if bucket_root.is_symlink():
            raise SpillStoreError("The local spill bucket is not a real directory.")
        if not bucket_root.is_dir():
            raise FileNotFoundError(bucket_root)
        resolved_root = bucket_root.resolve(strict=True)
        target = bucket_root / object_key
        if create:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = bucket_root
        for part in Path(object_key).parts[:-1]:
            current = current / part
            if not current.exists():
                raise FileNotFoundError(current)
            if current.is_symlink() or not current.is_dir():
                raise SpillStoreError("A local spill directory is not a real directory.")
        if not target.parent.resolve(strict=True).is_relative_to(resolved_root):
            raise SpillStoreError("The local spill object leaves the storage root.")
        return target

    def put(self, *, tenant_id: uuid.UUID, digest: str, payload: bytes) -> str:
        if not _DIGEST_PATTERN.fullmatch(digest) or hashlib.sha256(payload).hexdigest() != digest:
            raise SpillStoreError("The spill payload does not match its content digest.")
        key = f"{tenant_id}/{SPILL_PREFIX}/{digest}"
        try:
            if self.storage.is_local:
                target = self._local_path(key)
                if target.is_symlink():
                    raise SpillStoreError("The local spill object is not a regular file.")
                if target.exists():
                    if (
                        not target.is_file()
                        or hashlib.sha256(target.read_bytes()).hexdigest() != digest
                    ):
                        raise SpillStoreError(
                            "The existing local spill object failed its integrity check."
                        )
                    return key
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(temporary, target, follow_symlinks=False)
                except FileExistsError:
                    if (
                        target.is_symlink()
                        or hashlib.sha256(target.read_bytes()).hexdigest() != digest
                    ):
                        raise SpillStoreError(
                            "The existing local spill object failed its integrity check."
                        ) from None
                finally:
                    temporary.unlink(missing_ok=True)
                os.chmod(target, 0o600, follow_symlinks=False)
                return key
            if self.storage.object_exists(
                tenant_id=tenant_id,
                bucket=self.settings.minio_bucket,
                object_key=key,
            ):
                existing = self.storage.get_tenant_bytes(
                    tenant_id=tenant_id,
                    bucket=self.settings.minio_bucket,
                    object_key=key,
                )
                if hashlib.sha256(existing).hexdigest() != digest:
                    raise SpillStoreError("The existing spill object failed its integrity check.")
            else:
                self.storage.put_object_bytes(
                    tenant_id=tenant_id,
                    object_key=key,
                    content_type="text/plain; charset=utf-8",
                    payload=payload,
                    metadata={"kind": "spill"},
                )
        except (StorageServiceError, OSError) as exc:
            raise SpillStoreError("The spilled result could not be stored.") from exc
        return key

    def get(self, *, tenant_id: uuid.UUID, digest: str) -> bytes:
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise SpillStoreError("The spilled result reference is invalid.")
        key = f"{tenant_id}/{SPILL_PREFIX}/{digest}"
        try:
            if self.storage.is_local:
                target = self._local_path(key, create=False)
                descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise SpillStoreError("The local spill object is not a regular file.")
                    return stream.read()
            return self.storage.get_tenant_bytes(
                tenant_id=tenant_id,
                bucket=self.settings.minio_bucket,
                object_key=key,
            )
        except (StorageServiceError, OSError) as exc:
            raise SpillStoreError("The spilled result is unavailable.") from exc

    def cleanup_expired(self, *, older_than: float) -> int:
        """Delete expired spill objects without touching other tenant data."""
        removed = 0
        if self.storage.is_local:
            root = self.storage.local_root / self.settings.minio_bucket
            try:
                if root.is_symlink():
                    return 0
                tenants = tuple(root.iterdir())
            except OSError:
                return 0
            for tenant_dir in tenants:
                if removed >= _CLEANUP_BATCH_SIZE:
                    break
                try:
                    uuid.UUID(tenant_dir.name)
                except ValueError:
                    continue
                if tenant_dir.is_symlink() or not tenant_dir.is_dir():
                    continue
                spill_dir = tenant_dir / SPILL_PREFIX
                if spill_dir.is_symlink() or not spill_dir.is_dir():
                    continue
                try:
                    entries = tuple(spill_dir.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    if removed >= _CLEANUP_BATCH_SIZE:
                        break
                    if not _DIGEST_PATTERN.fullmatch(entry.name) or entry.is_symlink():
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode) and info.st_mtime < older_than:
                            entry.unlink()
                            removed += 1
                    except OSError:
                        continue
                if removed >= _CLEANUP_BATCH_SIZE:
                    break
                try:
                    spill_dir.rmdir()
                except OSError:
                    pass
            return removed

        continuation: str | None = None
        scanned = 0
        try:
            client = self.storage._get_client()
            while removed < _CLEANUP_BATCH_SIZE and scanned < _CLEANUP_BATCH_SIZE * 4:
                request: dict[str, Any] = {
                    "Bucket": self.settings.minio_bucket,
                    "Prefix": "",
                    "MaxKeys": min(1_000, _CLEANUP_BATCH_SIZE * 4 - scanned),
                }
                if continuation:
                    request["ContinuationToken"] = continuation
                page = client.list_objects_v2(**request)
                for item in page.get("Contents", ()):
                    key = str(item.get("Key") or "")
                    parts = key.split("/")
                    if (
                        len(parts) != 3
                        or parts[1] != SPILL_PREFIX
                        or not _DIGEST_PATTERN.fullmatch(parts[2])
                    ):
                        continue
                    modified = item.get("LastModified")
                    if modified is None or modified.timestamp() >= older_than:
                        continue
                    try:
                        tenant_id = uuid.UUID(parts[0])
                    except ValueError:
                        continue
                    self.storage.delete_tenant_object(
                        tenant_id=tenant_id,
                        bucket=self.settings.minio_bucket,
                        object_key=key,
                    )
                    removed += 1
                    if removed >= _CLEANUP_BATCH_SIZE:
                        break
                scanned += len(page.get("Contents", ()))
                if not page.get("IsTruncated") or not page.get("NextContinuationToken"):
                    break
                continuation = str(page["NextContinuationToken"])
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort by policy.
            logger.warning(
                "Spill cleanup stopped after %d deletions: %s", removed, type(exc).__name__
            )
        return removed


class LocalFileSpillBackend:
    """Private local spill files with atomic writes and bounded cleanup."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SpillStoreError("The local spill root must be a real directory.")
        os.chmod(self.root, 0o700)
        self.root = self.root.resolve(strict=True)

    def _path(self, tenant_id: uuid.UUID, digest: str) -> Path:
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise SpillStoreError("The spilled result reference is invalid.")
        tenant_root = self.root / str(tenant_id) / SPILL_PREFIX
        return tenant_root / digest

    def _secure_parent(self, tenant_id: uuid.UUID, *, create: bool = True) -> Path:
        tenant = self.root / str(tenant_id)
        spill = tenant / SPILL_PREFIX
        for directory in (tenant, spill):
            if create:
                directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SpillStoreError("A local spill directory is not a real directory.")
            if not directory.resolve(strict=True).is_relative_to(self.root):
                raise SpillStoreError("A local spill directory leaves its configured root.")
            if create:
                os.chmod(directory, 0o700)
        return spill

    def put(self, *, tenant_id: uuid.UUID, digest: str, payload: bytes) -> str:
        if not _DIGEST_PATTERN.fullmatch(digest) or hashlib.sha256(payload).hexdigest() != digest:
            raise SpillStoreError("The spill payload does not match its content digest.")
        target = self._path(tenant_id, digest)
        self._secure_parent(tenant_id)
        if target.is_symlink():
            raise SpillStoreError("The local spill object is not a regular file.")
        if target.is_file():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise SpillStoreError("The existing local spill object failed its integrity check.")
            return f"{tenant_id}/{SPILL_PREFIX}/{digest}"
        temporary = target.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                if target.is_symlink() or not target.is_file():
                    raise SpillStoreError("The local spill object is not a regular file.") from None
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise SpillStoreError(
                        "The existing local spill object failed its integrity check."
                    ) from None
            os.chmod(target, 0o600, follow_symlinks=False)
        except OSError as exc:
            raise SpillStoreError("The spilled result could not be stored locally.") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        return f"{tenant_id}/{SPILL_PREFIX}/{digest}"

    def get(self, *, tenant_id: uuid.UUID, digest: str) -> bytes:
        target = self._path(tenant_id, digest)
        try:
            self._secure_parent(tenant_id, create=False)
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise SpillStoreError("The local spill object is not a regular file.")
                return stream.read()
        except SpillStoreError:
            raise
        except OSError as exc:
            raise SpillStoreError("The spilled result is unavailable.") from exc

    def cleanup_expired(self, *, older_than: float) -> int:
        removed = 0
        try:
            tenant_entries = tuple(self.root.iterdir())
        except OSError:
            return 0
        for tenant_dir in tenant_entries:
            try:
                uuid.UUID(tenant_dir.name)
            except ValueError:
                continue
            if tenant_dir.is_symlink() or not tenant_dir.is_dir():
                continue
            spill_dir = tenant_dir / SPILL_PREFIX
            if spill_dir.is_symlink() or not spill_dir.is_dir():
                continue
            try:
                entries = tuple(spill_dir.iterdir())
            except OSError:
                continue
            for entry in entries:
                if removed >= _CLEANUP_BATCH_SIZE:
                    break
                if not _DIGEST_PATTERN.fullmatch(entry.name) or entry.is_symlink():
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < older_than:
                        entry.unlink()
                        removed += 1
                except OSError:
                    continue
            if removed >= _CLEANUP_BATCH_SIZE:
                break
            try:
                spill_dir.rmdir()
                tenant_dir.rmdir()
            except OSError:
                pass
        return removed


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

    def __init__(self, settings: Settings, backend: SpillBackend | None = None) -> None:
        """Create the store for one deployment.

        Args:
            settings: The application settings that choose local or object
                storage.
        """
        self.settings = settings
        self.backend = backend or self._configured_backend(settings)

    @staticmethod
    def _configured_backend(settings: Settings) -> SpillBackend:
        backend_name = str(getattr(settings, "deepspace_spill_backend", "storage")).casefold()
        if backend_name == "local":
            configured_root = str(getattr(settings, "deepspace_spill_local_root", "") or "").strip()
            if configured_root:
                root = Path(configured_root)
            else:
                from app.core.averqel_home import get_averqel_home

                root = Path(get_averqel_home()) / "spill"
            return LocalFileSpillBackend(root)
        if backend_name in {"storage", "object", "minio"}:
            return ObjectStorageSpillBackend(settings)
        raise SpillStoreError(f"Unknown spill storage backend: {backend_name}")

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
        raw = payload.encode("utf-8", errors="replace")
        digest = self.fingerprint(raw)
        key = self.backend.put(tenant_id=tenant_id, digest=digest, payload=raw)
        self._maybe_cleanup()
        return SpillRef(
            sha256=digest,
            size_bytes=len(raw),
            bucket=(
                self.settings.minio_bucket
                if isinstance(self.backend, ObjectStorageSpillBackend)
                else "local"
            ),
            object_key=key,
        )

    def _maybe_cleanup(self) -> None:
        """Sweep expired spills periodically; cleanup failures never fail a result."""
        now = time.time()
        backend_key = (
            str(self.backend.root)
            if isinstance(self.backend, LocalFileSpillBackend)
            else f"object:{self.settings.minio_endpoint}:{self.settings.minio_bucket}"
        )
        with _LOCAL_CLEANUP_LOCK:
            if now - _CLEANUP_LAST_RUN.get(backend_key, 0.0) < _CLEANUP_INTERVAL_SECONDS:
                return
            _CLEANUP_LAST_RUN[backend_key] = now
        retention = max(
            60, int(getattr(self.settings, "deepspace_spill_retention_seconds", 90 * 86400))
        )
        try:
            self.backend.cleanup_expired(older_than=now - retention)
        except Exception:  # noqa: BLE001 - spill rejection/cleanup is explicitly best effort.
            logger.warning("Spill cleanup failed for backend %s", backend_key, exc_info=True)

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
        raw = self.backend.get(tenant_id=tenant_id, digest=sha256)
        if self.fingerprint(raw) != sha256:
            raise SpillStoreError("The spilled result failed its integrity check.")
        return raw.decode("utf-8", errors="replace")


def spill_result(
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    tool_name: str,
    payload: Any,
    threshold_chars: int | None = None,
    max_chars: int | None = None,
    preview_budget_tokens: int = SPILL_PREVIEW_TOKENS,
    source: dict[str, str] | None = None,
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
    preview = result_preview(payload, text, preview_budget_tokens)
    result = {
        "status": "spilled",
        "tool_name": tool_name,
        "spill_ref": reference.sha256,
        "size_bytes": reference.size_bytes,
        "preview": preview.preview,
        "preview_truncated": preview.omitted_bytes > 0 or preview.omitted_images > 0,
        "omitted_bytes": preview.omitted_bytes,
        "omitted_images": preview.omitted_images,
        "message": (
            "The complete result is stored. Read it with spill_read using the "
            "spill_ref, or use the preview above when the excerpt is enough."
        ),
    }
    if source:
        result["source"] = dict(source)
    return result, reference


__all__ = [
    "SPILL_PREFIX",
    "SPILL_PREVIEW_CHARS",
    "SPILL_PREVIEW_TOKENS",
    "SpillBackend",
    "LocalFileSpillBackend",
    "ObjectStorageSpillBackend",
    "SpillRef",
    "SpillStore",
    "SpillStoreError",
    "spill_result",
]
