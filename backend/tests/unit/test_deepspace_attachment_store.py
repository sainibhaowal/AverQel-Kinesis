"""Tests for the content-addressed attachment store."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.deepspace.services.attachment_store import ContentAddressedAttachmentStore
from app.system.services.storage_service import StorageService

pytestmark = pytest.mark.unit_no_db


def _settings(tmp_path: Path) -> Settings:
    """Build an isolated local-storage deployment."""
    return Settings(
        storage_backend="local",
        local_storage_path=str(tmp_path / "storage"),
        minio_bucket="test-bucket",
        minio_endpoint="local",
        upload_max_bytes=10_000_000,
    )


def _attachment_dir(settings: Settings, tenant_id: uuid.UUID) -> Path:
    """Return the explicit attachment directory for one tenant."""
    return (
        Path(str(settings.local_storage_path))
        / str(settings.minio_bucket)
        / str(tenant_id)
        / "attachments"
    )


def test_identical_bytes_are_stored_once(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = ContentAddressedAttachmentStore(settings)
    tenant_id = uuid.uuid4()

    first = store.put(tenant_id=tenant_id, payload=b"same bytes", content_type="text/plain")
    second = store.put(tenant_id=tenant_id, payload=b"same bytes", content_type="text/plain")

    assert first.sha256 == second.sha256
    assert first.object_key == second.object_key
    blobs = sorted(path.name for path in _attachment_dir(settings, tenant_id).iterdir())
    assert blobs == [first.sha256]
    assert store.get(first) == b"same bytes"


def test_different_bytes_get_different_keys(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = ContentAddressedAttachmentStore(settings)
    tenant_id = uuid.uuid4()

    first = store.put(tenant_id=tenant_id, payload=b"one", content_type="text/plain")
    second = store.put(tenant_id=tenant_id, payload=b"two", content_type="text/plain")

    assert first.sha256 != second.sha256
    assert first.object_key != second.object_key
    assert store.get(first) == b"one"
    assert store.get(second) == b"two"


def test_tenants_do_not_share_a_blob(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = ContentAddressedAttachmentStore(settings)
    first_tenant = uuid.uuid4()
    second_tenant = uuid.uuid4()

    first = store.put(tenant_id=first_tenant, payload=b"shared", content_type="text/plain")
    second = store.put(tenant_id=second_tenant, payload=b"shared", content_type="text/plain")

    assert first.sha256 == second.sha256
    assert first.object_key != second.object_key
    assert store.get(first) == b"shared"
    assert store.get(second) == b"shared"


def test_exists_reports_a_stored_fingerprint(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = ContentAddressedAttachmentStore(settings)
    tenant_id = uuid.uuid4()

    reference = store.put(tenant_id=tenant_id, payload=b"present", content_type="text/plain")

    assert store.exists(tenant_id=tenant_id, sha256=reference.sha256) is True
    assert store.exists(tenant_id=tenant_id, sha256="0" * 64) is False


@pytest.mark.skipif(os.name != "posix", reason="hard links are a POSIX filesystem feature")
def test_document_uploads_hard_link_identical_content(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = StorageService(settings)
    tenant_id = uuid.uuid4()

    first = storage.put_bytes(
        tenant_id=tenant_id,
        document_id=uuid.uuid4(),
        filename="first.txt",
        content_type="text/plain",
        payload=b"same document",
    )
    second = storage.put_bytes(
        tenant_id=tenant_id,
        document_id=uuid.uuid4(),
        filename="second.txt",
        content_type="text/plain",
        payload=b"same document",
    )

    assert first.object_key != second.object_key
    root = Path(str(settings.local_storage_path)) / str(settings.minio_bucket)
    first_path = root / first.object_key
    second_path = root / second.object_key
    assert first_path.read_bytes() == b"same document"
    assert second_path.read_bytes() == b"same document"
    assert first_path.stat().st_ino == second_path.stat().st_ino
    assert len(list(_attachment_dir(settings, tenant_id).iterdir())) == 1


@pytest.mark.skipif(os.name != "posix", reason="hard links are a POSIX filesystem feature")
def test_prune_removes_a_blob_only_after_its_last_link(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = StorageService(settings)
    tenant_id = uuid.uuid4()
    first = storage.put_bytes(
        tenant_id=tenant_id,
        document_id=uuid.uuid4(),
        filename="first.txt",
        content_type="text/plain",
        payload=b"keep me",
    )
    second = storage.put_bytes(
        tenant_id=tenant_id,
        document_id=uuid.uuid4(),
        filename="second.txt",
        content_type="text/plain",
        payload=b"keep me",
    )
    blob = next(_attachment_dir(settings, tenant_id).iterdir())
    assert blob.stat().st_nlink == 3

    storage.delete_object(bucket=settings.minio_bucket, object_key=first.object_key)

    assert storage.prune_unreferenced_blobs(tenant_id=tenant_id) == 0
    assert blob.is_file()

    storage.delete_object(bucket=settings.minio_bucket, object_key=second.object_key)

    assert storage.prune_unreferenced_blobs(tenant_id=tenant_id) == 1
    assert not blob.exists()


@pytest.mark.skipif(os.name != "posix", reason="hard links are a POSIX filesystem feature")
def test_targeted_prune_keeps_a_blob_with_another_link(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = StorageService(settings)
    tenant_id = uuid.uuid4()
    first = storage.put_bytes(
        tenant_id=tenant_id,
        document_id=uuid.uuid4(),
        filename="first.txt",
        content_type="text/plain",
        payload=b"shared",
    )
    second = storage.put_bytes(
        tenant_id=tenant_id,
        document_id=uuid.uuid4(),
        filename="second.txt",
        content_type="text/plain",
        payload=b"shared",
    )

    storage.delete_object(bucket=settings.minio_bucket, object_key=first.object_key)

    assert storage.prune_unreferenced_blob(tenant_id=tenant_id, sha256=first.etag) is False

    storage.delete_object(bucket=settings.minio_bucket, object_key=second.object_key)

    assert storage.prune_unreferenced_blob(tenant_id=tenant_id, sha256=first.etag) is True


@pytest.mark.skipif(os.name != "posix", reason="hard links are a POSIX filesystem feature")
def test_copy_object_links_to_the_content_blob(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = StorageService(settings)
    tenant_id = uuid.uuid4()
    source = storage.put_bytes(
        tenant_id=tenant_id,
        document_id=uuid.uuid4(),
        filename="source.txt",
        content_type="text/plain",
        payload=b"copy me",
    )

    copied = storage.copy_object(
        bucket=settings.minio_bucket,
        source_key=source.object_key,
        tenant_id=tenant_id,
        document_id=uuid.uuid4(),
        filename="copied.txt",
        content_type="text/plain",
    )

    root = Path(str(settings.local_storage_path)) / str(settings.minio_bucket)
    assert (root / copied.object_key).read_bytes() == b"copy me"
    assert (root / copied.object_key).stat().st_ino == (root / source.object_key).stat().st_ino
    assert len(list(_attachment_dir(settings, tenant_id).iterdir())) == 1
