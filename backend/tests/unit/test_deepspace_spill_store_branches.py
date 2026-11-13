"""Focused filesystem and object-storage edge cases for spill storage."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services import spill_store

pytestmark = pytest.mark.unit_no_db


def _settings(**changes: Any) -> SimpleNamespace:
    values = {
        "deepspace_spill_backend": "local",
        "deepspace_spill_local_root": "",
        "deepspace_spill_retention_seconds": 100,
        "minio_bucket": "spill-test",
        "minio_endpoint": "unused.invalid",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _object_local_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    class LocalStorage:
        is_local = True
        local_root = tmp_path / "storage"

    monkeypatch.setattr(spill_store, "StorageService", lambda _: LocalStorage())
    return spill_store.ObjectStorageSpillBackend(_settings())


def _object_remote_backend(monkeypatch: pytest.MonkeyPatch, storage: Any) -> Any:
    monkeypatch.setattr(spill_store, "StorageService", lambda _: storage)
    return spill_store.ObjectStorageSpillBackend(_settings(deepspace_spill_backend="storage"))


def _key(tenant_id: uuid.UUID, digest: str) -> str:
    return f"{tenant_id}/spill/{digest}"


def test_object_local_path_rejects_symlinked_bucket_and_resolved_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _object_local_backend(tmp_path, monkeypatch)
    bucket = backend.storage.local_root / backend.settings.minio_bucket
    outside = tmp_path / "outside"
    outside.mkdir()
    bucket.parent.mkdir(parents=True)
    bucket.symlink_to(outside, target_is_directory=True)
    with pytest.raises(spill_store.SpillStoreError, match="bucket is not a real directory"):
        backend._local_path(_key(uuid.uuid4(), "a" * 64))

    bucket.unlink()
    bucket.mkdir()
    tenant_id = uuid.uuid4()
    (bucket / str(tenant_id) / "spill").mkdir(parents=True)
    destination = bucket / str(tenant_id) / "spill"
    original_resolve = Path.resolve

    def escaping_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == destination:
            return outside
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", escaping_resolve)
    with pytest.raises(spill_store.SpillStoreError, match="leaves the storage root"):
        backend._local_path(_key(tenant_id, "b" * 64), create=False)


@pytest.mark.parametrize("race_result", ["valid", "corrupt", "symlink"])
def test_object_local_atomic_write_handles_racing_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race_result: str
) -> None:
    backend = _object_local_backend(tmp_path, monkeypatch)
    tenant_id = uuid.uuid4()
    payload = b"spill bytes"
    digest = hashlib.sha256(payload).hexdigest()
    outside = tmp_path / "outside"
    outside.write_bytes(payload)

    def racing_link(source: Any, destination: Any, *, follow_symlinks: bool = True) -> None:
        del source, follow_symlinks
        target = Path(destination)
        if race_result == "symlink":
            target.symlink_to(outside)
        else:
            target.write_bytes(payload if race_result == "valid" else b"corrupt")
        raise FileExistsError

    monkeypatch.setattr(spill_store.os, "link", racing_link)
    if race_result == "valid":
        assert backend.put(tenant_id=tenant_id, digest=digest, payload=payload) == _key(
            tenant_id, digest
        )
        assert backend.get(tenant_id=tenant_id, digest=digest) == payload
    else:
        with pytest.raises(spill_store.SpillStoreError, match="integrity|regular file"):
            backend.put(tenant_id=tenant_id, digest=digest, payload=payload)


def test_object_spill_rejects_existing_remote_corruption_and_local_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"remote content"

    class RemoteStorage:
        is_local = False

        def object_exists(self, **_: Any) -> bool:
            return True

        def get_tenant_bytes(self, **_: Any) -> bytes:
            return b"different content"

    remote = _object_remote_backend(monkeypatch, RemoteStorage())
    with pytest.raises(spill_store.SpillStoreError, match="integrity"):
        remote.put(
            tenant_id=uuid.uuid4(),
            digest=hashlib.sha256(payload).hexdigest(),
            payload=payload,
        )


def test_object_backend_rejects_symlink_created_by_atomic_write_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _object_local_backend(tmp_path, monkeypatch)
    tenant_id = uuid.uuid4()
    payload = b"expected"
    digest = hashlib.sha256(payload).hexdigest()
    outside = tmp_path / "outside"
    outside.write_bytes(payload)

    def racing_link(source: Any, destination: Any, **_: Any) -> None:
        del source
        Path(destination).symlink_to(outside)
        raise FileExistsError

    monkeypatch.setattr(spill_store.os, "link", racing_link)
    with pytest.raises(spill_store.SpillStoreError, match="integrity"):
        backend.put(tenant_id=tenant_id, digest=digest, payload=payload)

    local = _object_local_backend(tmp_path, monkeypatch)
    original_open = os.open

    def fail_temp_open(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        if str(path).endswith(".tmp"):
            raise PermissionError("write denied")
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(spill_store.os, "open", fail_temp_open)
    with pytest.raises(spill_store.SpillStoreError, match="could not be stored"):
        local.put(
            tenant_id=uuid.uuid4(),
            digest=hashlib.sha256(payload).hexdigest(),
            payload=payload,
        )


def test_object_local_get_rejects_non_regular_open_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _object_local_backend(tmp_path, monkeypatch)
    tenant_id = uuid.uuid4()
    payload = b"file"
    digest = hashlib.sha256(payload).hexdigest()
    path = backend._local_path(_key(tenant_id, digest))
    path.write_bytes(payload)
    monkeypatch.setattr(
        spill_store.os,
        "fstat",
        lambda _: SimpleNamespace(st_mode=stat.S_IFDIR),
    )
    with pytest.raises(spill_store.SpillStoreError, match="regular file"):
        backend.get(tenant_id=tenant_id, digest=digest)


def test_object_backend_rejects_invalid_digest_before_storage_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RemoteStorage:
        is_local = False

    backend = _object_remote_backend(monkeypatch, RemoteStorage())
    with pytest.raises(spill_store.SpillStoreError, match="reference is invalid"):
        backend.get(tenant_id=uuid.uuid4(), digest="../not-a-digest")


def test_object_local_cleanup_handles_missing_and_symlinked_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _object_local_backend(tmp_path, monkeypatch)
    assert backend.cleanup_expired(older_than=10) == 0

    root = backend.storage.local_root / backend.settings.minio_bucket
    root.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    assert backend.cleanup_expired(older_than=10) == 0


def test_object_local_cleanup_filters_bad_entries_and_continues_after_io_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _object_local_backend(tmp_path, monkeypatch)
    root = backend.storage.local_root / backend.settings.minio_bucket
    tenant_id = uuid.uuid4()
    tenant = root / str(tenant_id)
    spill_dir = tenant / "spill"
    spill_dir.mkdir(parents=True)
    (root / "not-a-tenant").mkdir()
    (root / "regular-file").write_text("ignore", encoding="utf-8")
    skipped_tenant = root / str(uuid.uuid4())
    skipped_tenant.mkdir()
    (skipped_tenant / "spill").write_text("ignore", encoding="utf-8")
    outside = tmp_path / "outside-file"
    outside.write_text("safe", encoding="utf-8")
    (root / str(uuid.uuid4())).symlink_to(outside, target_is_directory=True)
    (root / str(uuid.uuid4())).write_text("not a tenant directory", encoding="utf-8")
    (spill_dir / ("d" * 64)).symlink_to(outside)
    (spill_dir / "not-a-digest").write_text("ignore", encoding="utf-8")
    recent = spill_dir / ("e" * 64)
    recent.write_text("keep", encoding="utf-8")
    expired_stat = spill_dir / ("a" * 64)
    expired_stat.write_text("stat error", encoding="utf-8")
    expired_unlink = spill_dir / ("b" * 64)
    expired_unlink.write_text("unlink error", encoding="utf-8")
    expired_ok = spill_dir / ("c" * 64)
    expired_ok.write_text("remove", encoding="utf-8")
    non_regular = spill_dir / ("f" * 64)
    non_regular.write_text("fake directory entry", encoding="utf-8")
    for path in (expired_stat, expired_unlink, expired_ok):
        os.utime(path, (1, 1))

    original_stat = Path.stat
    original_is_symlink = Path.is_symlink
    original_unlink = Path.unlink

    def selective_stat(path: Path, *, follow_symlinks: bool = True) -> Any:
        if path == expired_stat:
            raise PermissionError("stat denied")
        if path == non_regular:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_mtime=1)
        return original_stat(path, follow_symlinks=follow_symlinks)

    def selective_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == expired_unlink:
            raise PermissionError("unlink denied")
        original_unlink(path, *args, **kwargs)

    def selective_is_symlink(path: Path) -> bool:
        if path == expired_stat:
            return False
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "stat", selective_stat)
    monkeypatch.setattr(Path, "unlink", selective_unlink)
    monkeypatch.setattr(Path, "is_symlink", selective_is_symlink)
    assert backend.cleanup_expired(older_than=10) == 1
    assert not expired_ok.exists()
    assert original_stat(expired_stat).st_size > 0
    assert expired_unlink.exists()
    assert recent.exists() and non_regular.exists()


def test_object_local_cleanup_obeys_batch_limit_and_iteration_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _object_local_backend(tmp_path, monkeypatch)
    root = backend.storage.local_root / backend.settings.minio_bucket
    tenant = uuid.uuid4()
    spill_dir = root / str(tenant) / "spill"
    spill_dir.mkdir(parents=True)
    for letter in "ab":
        item = spill_dir / (letter * 64)
        item.write_bytes(letter.encode())
        os.utime(item, (1, 1))
    (root / str(uuid.uuid4()) / "spill").mkdir(parents=True)

    monkeypatch.setattr(spill_store, "_CLEANUP_BATCH_SIZE", 1)
    assert backend.cleanup_expired(older_than=10) == 1

    backend = _object_local_backend(tmp_path / "other", monkeypatch)
    root = backend.storage.local_root / backend.settings.minio_bucket
    tenant_dir = root / str(uuid.uuid4())
    spill_dir = tenant_dir / "spill"
    spill_dir.mkdir(parents=True)
    original_iterdir = Path.iterdir

    def fail_spill_iteration(path: Path) -> Any:
        if path == spill_dir:
            raise PermissionError("directory denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_spill_iteration)
    assert backend.cleanup_expired(older_than=10) == 0

    zero_limit_backend = _object_local_backend(tmp_path / "zero-limit", monkeypatch)
    zero_limit_root = (
        zero_limit_backend.storage.local_root / zero_limit_backend.settings.minio_bucket
    )
    (zero_limit_root / str(uuid.uuid4())).mkdir(parents=True)
    monkeypatch.setattr(spill_store, "_CLEANUP_BATCH_SIZE", 0)
    assert zero_limit_backend.cleanup_expired(older_than=10) == 0


def test_local_backend_closes_descriptor_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = spill_store.LocalFileSpillBackend(tmp_path / "local")
    payload = b"fdopen failure"
    digest = hashlib.sha256(payload).hexdigest()
    descriptors: list[int] = []
    original_open = os.open
    original_close = os.close

    def capture_open(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        descriptor = original_open(path, flags, mode, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(spill_store.os, "open", capture_open)
    monkeypatch.setattr(
        spill_store.os,
        "fdopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fdopen failed")),
    )
    closed: list[int] = []

    def capture_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(spill_store.os, "close", capture_close)
    with pytest.raises(spill_store.SpillStoreError, match="stored locally"):
        backend.put(tenant_id=uuid.uuid4(), digest=digest, payload=payload)
    assert closed == descriptors


def test_local_backend_get_rethrows_policy_errors_and_wraps_os_errors(
    tmp_path: Path,
) -> None:
    backend = spill_store.LocalFileSpillBackend(tmp_path / "local")
    tenant_id = uuid.uuid4()
    tenant = backend.root / str(tenant_id)
    tenant.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(spill_store.SpillStoreError, match="not a real directory"):
        backend.get(tenant_id=tenant_id, digest="a" * 64)
    with pytest.raises(spill_store.SpillStoreError, match="unavailable"):
        backend.get(tenant_id=uuid.uuid4(), digest="a" * 64)


def test_local_backend_checks_resolved_parent_and_regular_file_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = spill_store.LocalFileSpillBackend(tmp_path / "local")
    tenant_id = uuid.uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    tenant_path = backend.root / str(tenant_id)
    original_resolve = Path.resolve

    def escape_parent(path: Path, *, strict: bool = False) -> Path:
        if path == tenant_path:
            return outside
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", escape_parent)
    with pytest.raises(spill_store.SpillStoreError, match="leaves its configured root"):
        backend.put(
            tenant_id=tenant_id,
            digest=hashlib.sha256(b"payload").hexdigest(),
            payload=b"payload",
        )

    monkeypatch.setattr(Path, "resolve", original_resolve)
    digest = hashlib.sha256(b"stored").hexdigest()
    backend.put(tenant_id=tenant_id, digest=digest, payload=b"stored")
    monkeypatch.setattr(
        spill_store.os,
        "fstat",
        lambda _: SimpleNamespace(st_mode=stat.S_IFDIR),
    )
    with pytest.raises(spill_store.SpillStoreError, match="regular file"):
        backend.get(tenant_id=tenant_id, digest=digest)


def test_local_backend_rejects_symlink_won_duplicate_write_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = spill_store.LocalFileSpillBackend(tmp_path / "local")
    tenant_id = uuid.uuid4()
    payload = b"race"
    digest = hashlib.sha256(payload).hexdigest()
    outside = tmp_path / "outside"
    outside.write_bytes(payload)

    def racing_link(source: Any, destination: Any, **_: Any) -> None:
        del source
        Path(destination).symlink_to(outside)
        raise FileExistsError

    monkeypatch.setattr(spill_store.os, "link", racing_link)
    with pytest.raises(spill_store.SpillStoreError, match="regular file"):
        backend.put(tenant_id=tenant_id, digest=digest, payload=payload)


def test_local_cleanup_handles_invalid_roots_directories_and_unlink_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = spill_store.LocalFileSpillBackend(tmp_path / "local")
    assert backend.cleanup_expired(older_than=10) == 0

    (backend.root / "invalid-tenant").mkdir()
    tenant_id = uuid.uuid4()
    tenant = backend.root / str(tenant_id)
    tenant.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tenant / "spill").symlink_to(outside, target_is_directory=True)
    (backend.root / str(uuid.uuid4())).symlink_to(outside, target_is_directory=True)
    (backend.root / str(uuid.uuid4())).write_text("not a directory", encoding="utf-8")
    spill_file_tenant = backend.root / str(uuid.uuid4())
    spill_file_tenant.mkdir()
    (spill_file_tenant / "spill").write_text("not a directory", encoding="utf-8")
    original_iterdir = Path.iterdir

    def fail_root_iteration(path: Path) -> Any:
        if path == backend.root:
            raise PermissionError("root denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_root_iteration)
    assert backend.cleanup_expired(older_than=10) == 0
    monkeypatch.setattr(Path, "iterdir", original_iterdir)

    spill_iteration_tenant = backend.root / str(uuid.uuid4())
    inaccessible_spill = spill_iteration_tenant / "spill"
    inaccessible_spill.mkdir(parents=True)
    ignored_entries = backend.root / str(uuid.uuid4()) / "spill"
    ignored_entries.mkdir(parents=True)
    (ignored_entries / "not-a-digest").write_text("ignore", encoding="utf-8")
    (ignored_entries / ("d" * 64)).symlink_to(outside / "outside-file")

    def fail_spill_iteration(path: Path) -> Any:
        if path == inaccessible_spill:
            raise PermissionError("spill directory denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_spill_iteration)
    assert backend.cleanup_expired(older_than=10) == 0
    monkeypatch.setattr(Path, "iterdir", original_iterdir)

    (tenant / "spill").unlink()
    spill_dir = tenant / "spill"
    spill_dir.mkdir()
    item = spill_dir / ("a" * 64)
    item.write_bytes(b"old")
    os.utime(item, (1, 1))
    original_unlink = Path.unlink

    def fail_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == item:
            raise PermissionError("unlink denied")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    assert backend.cleanup_expired(older_than=10) == 0
    assert item.exists()


def test_local_cleanup_stops_at_limit_and_removes_empty_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = spill_store.LocalFileSpillBackend(tmp_path / "local")
    monkeypatch.setattr(spill_store, "_CLEANUP_BATCH_SIZE", 1)
    tenant_id = uuid.uuid4()
    for content in (b"first", b"second"):
        digest = hashlib.sha256(content).hexdigest()
        key = backend.put(tenant_id=tenant_id, digest=digest, payload=content)
        path = backend.root / key
        os.utime(path, (1, 1))
    assert backend.cleanup_expired(older_than=10) == 1

    empty_backend = spill_store.LocalFileSpillBackend(tmp_path / "local-empty")
    empty_tenant = uuid.uuid4()
    (empty_backend.root / str(empty_tenant) / "spill").mkdir(parents=True)
    assert empty_backend.cleanup_expired(older_than=10) == 0
    assert not (empty_backend.root / str(empty_tenant)).exists()


def test_store_defaults_and_reference_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "app-home"
    monkeypatch.setattr("app.core.averqel_home.get_averqel_home", lambda: home)
    settings = _settings(deepspace_spill_local_root=" ")
    backend = spill_store.SpillStore._configured_backend(settings)
    assert isinstance(backend, spill_store.LocalFileSpillBackend)
    assert backend.root == (home / "spill").resolve()

    for alias in ("object", "minio", "storage"):
        configured = spill_store.SpillStore._configured_backend(
            _settings(deepspace_spill_backend=alias)
        )
        assert isinstance(configured, spill_store.ObjectStorageSpillBackend)

    reference = spill_store.SpillRef("a" * 64, 3, "local", "tenant/spill/key")
    assert reference.as_dict() == {
        "sha256": "a" * 64,
        "size_bytes": 3,
        "bucket": "local",
        "object_key": "tenant/spill/key",
    }
    store = spill_store.SpillStore(settings, backend=backend)
    assert store.object_key(uuid.UUID(int=1), "a" * 64) == f"{uuid.UUID(int=1)}/spill/{'a' * 64}"
    assert spill_store.SpillStore.fingerprint(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_store_cleanup_interval_and_retention_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Backend:
        calls: list[float] = []

        def put(self, *, tenant_id: uuid.UUID, digest: str, payload: bytes) -> str:
            del payload
            return _key(tenant_id, digest)

        def get(self, *, tenant_id: uuid.UUID, digest: str) -> bytes:
            del tenant_id
            return bytes.fromhex(digest[:0])

        def cleanup_expired(self, *, older_than: float) -> int:
            self.calls.append(older_than)
            return 0

    backend = Backend()
    now = 1_000_000.0
    monkeypatch.setattr(spill_store, "_CLEANUP_LAST_RUN", {})
    monkeypatch.setattr(spill_store.time, "time", lambda: now)
    store = spill_store.SpillStore(_settings(deepspace_spill_retention_seconds=10), backend=backend)
    owner = uuid.uuid4()
    store.put(tenant_id=owner, payload="first")
    store.put(tenant_id=owner, payload="second")
    assert backend.calls == [now - 60]


def test_remote_cleanup_stops_at_limit_and_handles_missing_page_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid.uuid4()
    digest = "a" * 64

    class Client:
        def list_objects_v2(self, **_: Any) -> dict[str, Any]:
            return {
                "Contents": [
                    {
                        "Key": _key(owner, digest),
                        "LastModified": SimpleNamespace(timestamp=lambda: 1),
                    },
                    {
                        "Key": _key(owner, "b" * 64),
                        "LastModified": SimpleNamespace(timestamp=lambda: 1),
                    },
                ],
                "IsTruncated": True,
                "NextContinuationToken": "next-page",
            }

    class RemoteStorage:
        is_local = False

        def __init__(self) -> None:
            self.deleted: list[str] = []

        def _get_client(self) -> Client:
            return Client()

        def delete_tenant_object(self, *, object_key: str, **_: Any) -> None:
            self.deleted.append(object_key)

    storage = RemoteStorage()
    backend = _object_remote_backend(monkeypatch, storage)
    monkeypatch.setattr(spill_store, "_CLEANUP_BATCH_SIZE", 1)
    assert backend.cleanup_expired(older_than=2) == 1
    assert storage.deleted == [_key(owner, digest)]
