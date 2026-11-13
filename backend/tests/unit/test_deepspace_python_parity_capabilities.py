"""DeepSpace python parity capabilities regression tests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.spill_policy import (
    estimate_text_tokens,
    result_preview,
    retain_content_blocks,
    retain_text,
    take_recovered_images,
)
from app.deepspace.services.spill_store import (
    LocalFileSpillBackend,
    ObjectStorageSpillBackend,
    SpillStore,
    SpillStoreError,
    spill_result,
)
from app.deepspace.services.tools.spill import (
    _recover_spilled_images,
    _spill_read,
    _spill_session,
)

pytestmark = pytest.mark.unit_no_db


def _settings(**values: Any) -> SimpleNamespace:
    defaults = {
        "deepspace_spill_backend": "local",
        "deepspace_spill_local_root": "",
        "deepspace_spill_retention_seconds": 90 * 86400,
        "deepspace_spill_max_chars": 2_000_000,
        "deepspace_spill_threshold_chars": 24_000,
        "minio_bucket": "unit-test",
        "minio_endpoint": "unused.invalid",
        "deepspace_skill_custom_roots": "",
        "deepspace_bundled_skills_enabled": False,
        "deepspace_skill_user_invocation_enabled": True,
        "deepspace_permission_mode": "workspace-write",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_spill_preview_keeps_both_text_edges_and_unicode_boundaries() -> None:
    result = retain_text("頭" * 200 + "middle" * 200 + "尾" * 200, 64)

    assert result.preview.startswith("頭")
    assert result.preview.endswith("尾" * 8)
    assert "omitted" in result.preview
    assert result.omitted_bytes > 0


def test_spill_preview_respects_zero_and_full_budgets() -> None:
    assert retain_text("complete", 64).preview == "complete"
    empty = retain_text("hidden", 0)
    assert empty.preview == ""
    assert empty.omitted_bytes == len(b"hidden")


def test_spill_preview_handles_empty_text_and_too_small_marker_budget() -> None:
    assert estimate_text_tokens("") == 1
    assert retain_text("", 12).preview == ""

    too_small = retain_text("sensitive", 1, token_counter=len)
    assert too_small.preview == ""
    assert too_small.omitted_bytes == len(b"sensitive")

    marker = "\n\n[… omitted …]\n\n"
    hidden = "sensitive" * 8
    marker_only = retain_text(hidden, len(marker), token_counter=len)
    assert marker_only.preview == "[… omitted …]"
    assert marker_only.omitted_bytes == len(hidden.encode())


def test_spill_preview_keeps_complete_edges_when_token_cost_is_non_additive() -> None:
    value = "abcdefghij"

    def token_counter(text: str) -> int:
        return 100 if text == value else len(text)

    result = retain_text(value, 30, token_counter=token_counter)
    assert result.preview == value
    assert result.omitted_bytes == 0


def test_content_preview_returns_complete_blocks_and_list_payloads() -> None:
    blocks = [
        {"type": "text", "text": "complete"},
        {"type": "image", "source": {"data": "small"}},
    ]
    complete = retain_content_blocks(blocks, 2_000)
    assert json.loads(complete.preview) == blocks
    assert complete.omitted_bytes == 0
    assert complete.omitted_images == 0

    listed = result_preview(blocks, "fallback", 2_000)
    assert json.loads(listed.preview) == blocks


def test_recovered_image_filter_rejects_malformed_internal_values() -> None:
    malformed = {"_images_base64": "not-a-list"}
    assert take_recovered_images(malformed) == []
    assert "_images_base64" not in malformed

    mixed = {"_images_base64": ["data:image/png;base64,AA==", "", 4, None]}
    assert take_recovered_images(mixed) == ["data:image/png;base64,AA=="]


def test_image_preview_keeps_image_blocks_atomic_and_reports_omissions() -> None:
    image_one = {"type": "image", "source": {"data": "first-image"}}
    image_two = {"type": "image", "source": {"data": "second-image"}}
    payload = [
        {"type": "text", "text": "begin"},
        image_one,
        {"type": "text", "text": "x" * 4_000},
        image_two,
        {"type": "text", "text": "end"},
    ]

    result = retain_content_blocks(payload, 600)
    retained = json.loads(result.preview)

    assert image_one in retained
    assert image_two in retained
    assert result.omitted_bytes > 0
    assert result.omitted_images == 0


def test_image_preview_falls_back_for_unknown_blocks_and_reports_zero_budget() -> None:
    unsupported = result_preview({"content": [{"type": "audio", "data": "x"}]}, "fallback", 8)
    assert unsupported.preview
    empty = retain_content_blocks([{"type": "image"}], 0)
    assert empty.omitted_images == 1
    assert empty.preview == ""


def test_image_blocks_are_not_retained_when_serialized_data_exceeds_budget() -> None:
    block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "YQ==" * 2_000},
    }
    result = retain_content_blocks([block], 1_024)
    assert result.preview
    assert "YQ==" not in result.preview
    assert result.omitted_images == 1


@pytest.mark.asyncio
async def test_spill_read_rehydrates_images_as_next_turn_model_inputs(
    tmp_path: Path, monkeypatch
) -> None:
    import app.deepspace.services.spill_store as spill_module

    monkeypatch.setattr(spill_module, "_CLEANUP_LAST_RUN", {})
    settings = _settings(deepspace_spill_local_root=str(tmp_path / "spill"))
    tenant_id = uuid.uuid4()
    image = base64.b64encode(b"\x89PNG\r\n\x1a\nexample").decode("ascii")
    serialized = json.dumps(
        {
            "content": [
                {"type": "text", "text": "Inspect the image."},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image},
                },
            ]
        }
    )
    reference = SpillStore(settings).put(tenant_id=tenant_id, payload=serialized)
    context = SimpleNamespace(settings=settings, auth=SimpleNamespace(tenant_id=tenant_id))

    recovered = await _spill_read({"spill_ref": reference.sha256}, context)
    assert recovered["images_recovered"] == 1
    assert image not in recovered["text"]
    assert "image reattached" in recovered["text"]
    assert recovered["_images_base64"] == [f"data:image/png;base64,{image}"]
    assert take_recovered_images(recovered) == [f"data:image/png;base64,{image}"]
    assert "_images_base64" not in recovered

    without_images = await _spill_read(
        {"spill_ref": reference.sha256, "include_images": False}, context
    )
    assert image in without_images["text"]
    assert "_images_base64" not in without_images


def test_image_recovery_limits_images_and_does_not_fetch_urls() -> None:
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "YQ=="}}
        for _ in range(11)
    ]
    blocks.append({"type": "image", "image_url": {"url": "https://example.invalid/image.png"}})
    display, images, omitted = _recover_spilled_images({"content": blocks})
    assert len(images) == 10
    assert omitted == 1
    assert "https://example.invalid/image.png" in json.dumps(display)
    assert "recovery limit reached" in json.dumps(display)


def test_image_recovery_redacts_duplicate_payload_fields_and_empty_data() -> None:
    duplicate = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="},
        "data": "YQ==",
    }
    empty = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}}

    display, images, omitted = _recover_spilled_images({"content": [duplicate, empty]})
    serialized = json.dumps(display)

    assert images == ["data:image/png;base64,YQ=="]
    assert omitted == 1
    assert "YQ==" not in serialized
    assert "image reattached" in serialized
    assert "invalid image data" in serialized


@pytest.mark.asyncio
async def test_spill_read_redacts_images_beyond_the_nesting_limit(tmp_path: Path) -> None:
    settings = _settings(deepspace_spill_local_root=str(tmp_path / "spill"))
    tenant_id = uuid.uuid4()
    image_data = base64.b64encode(b"deep image bytes").decode("ascii")
    nested: Any = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": image_data},
    }
    for _ in range(40):
        nested = {"nested": nested}
    reference = SpillStore(settings).put(
        tenant_id=tenant_id,
        payload=json.dumps({"content": [nested]}),
    )
    context = SimpleNamespace(settings=settings, auth=SimpleNamespace(tenant_id=tenant_id))

    result = await _spill_read({"spill_ref": reference.sha256}, context)

    assert image_data not in result["text"]
    assert "nested spill content omitted" in result["text"]
    assert result["images_omitted"] == 1


@pytest.mark.asyncio
async def test_spill_session_saves_tenant_checked_conversation_reference(
    tmp_path: Path,
) -> None:
    class Result:
        def __init__(self, *, conversation: bool = True, rows: list[Any] | None = None) -> None:
            self.conversation = conversation
            self.rows = rows or []

        def scalar_one_or_none(self) -> object | None:
            return object() if self.conversation else None

        def all(self) -> list[Any]:
            return self.rows

    class Database:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows
            self.calls = 0

        def execute(self, statement: Any) -> Result:
            del statement
            self.calls += 1
            return Result(rows=self.rows if self.calls == 2 else [])

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    settings = _settings(deepspace_spill_local_root=str(tmp_path / "spill"))
    rows = [("assistant", "answer"), ("user", "first request")]
    context = SimpleNamespace(
        db=Database(rows),
        conversation_id=conversation_id,
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        settings=settings,
    )

    result = await _spill_session({"label": "  Budget notes  "}, context)

    assert result["status"] == "spilled"
    assert result["messages_included"] == 2
    assert result["source"] == {
        "kind": "session-reference",
        "session_id": str(conversation_id),
        "label": "Budget notes",
    }
    stored = SpillStore(settings).get(tenant_id=tenant_id, sha256=result["spill_ref"])
    assert stored == "[user]\nfirst request\n\n[assistant]\nanswer"


@pytest.mark.asyncio
async def test_spill_session_refuses_unavailable_empty_and_oversized_history(
    tmp_path: Path,
) -> None:
    context = SimpleNamespace(db=None)
    assert (await _spill_session({}, context))["status"] == "unavailable"

    class Result:
        def __init__(self, conversation: bool, rows: list[Any] | None = None) -> None:
            self.conversation = conversation
            self.rows = rows or []

        def scalar_one_or_none(self) -> object | None:
            return object() if self.conversation else None

        def all(self) -> list[Any]:
            return self.rows

    class Database:
        def __init__(self, conversation: bool, rows: list[Any]) -> None:
            self.conversation = conversation
            self.rows = rows
            self.calls = 0

        def execute(self, statement: Any) -> Result:
            del statement
            self.calls += 1
            return Result(self.conversation, self.rows if self.calls == 2 else [])

    auth = SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    context = SimpleNamespace(
        db=Database(False, []),
        conversation_id=uuid.uuid4(),
        auth=auth,
        settings=_settings(),
    )
    assert (await _spill_session({}, context))["status"] == "not_found"
    context.db = Database(True, [])
    assert (await _spill_session({}, context))["status"] == "empty"

    context.db = Database(True, [("user", "x" * 200)])
    context.settings = _settings(deepspace_spill_max_chars=10)
    oversized = await _spill_session({}, context)
    assert oversized["status"] == "refused"
    assert oversized["messages_included"] == 1


def test_local_spill_store_round_trips_tenant_scoped_bytes(tmp_path: Path, monkeypatch) -> None:
    import app.deepspace.services.spill_store as spill_module

    monkeypatch.setattr(spill_module, "_CLEANUP_LAST_RUN", {})
    backend = LocalFileSpillBackend(tmp_path / "spill")
    settings = _settings()
    owner = uuid.uuid4()
    store = SpillStore(settings, backend=backend)
    reference = store.put(tenant_id=owner, payload="complete tool result")
    repeated = store.put(tenant_id=owner, payload="complete tool result")

    assert store.get(tenant_id=owner, sha256=reference.sha256) == "complete tool result"
    assert repeated == reference
    assert reference.object_key == f"{owner}/spill/{reference.sha256}"
    assert (backend.root / str(owner) / "spill" / reference.sha256).stat().st_mode & 0o077 == 0
    other_tenant = uuid.uuid4()
    with pytest.raises(SpillStoreError, match="unavailable"):
        store.get(tenant_id=other_tenant, sha256=reference.sha256)
    assert not (backend.root / str(other_tenant)).exists()


def test_local_spill_store_rejects_corrupt_and_malformed_references(tmp_path: Path) -> None:
    backend = LocalFileSpillBackend(tmp_path / "spill")
    tenant_id = uuid.uuid4()
    payload = b"original"
    digest = hashlib.sha256(payload).hexdigest()
    backend.put(tenant_id=tenant_id, digest=digest, payload=payload)
    (backend.root / str(tenant_id) / "spill" / digest).write_bytes(b"changed")
    with pytest.raises(SpillStoreError, match="integrity"):
        SpillStore(_settings(), backend=backend).get(tenant_id=tenant_id, sha256=digest)
    with pytest.raises(SpillStoreError, match="digest"):
        backend.put(tenant_id=tenant_id, digest="0" * 64, payload=payload)
    with pytest.raises(SpillStoreError, match="invalid"):
        backend.get(tenant_id=tenant_id, digest="../bad")


def test_object_spill_backend_is_tenant_scoped_and_cleans_only_expired_spills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.deepspace.services.spill_store as spill_module

    class _Storage:
        is_local = False

        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.deleted: list[tuple[uuid.UUID, str]] = []

        def object_exists(self, **kwargs: Any) -> bool:
            return kwargs["object_key"] in self.objects

        def put_object_bytes(self, *, object_key: str, payload: bytes, **kwargs: Any) -> None:
            self.objects[object_key] = payload

        def get_tenant_bytes(self, *, object_key: str, **kwargs: Any) -> bytes:
            return self.objects[object_key]

        def delete_tenant_object(
            self, *, tenant_id: uuid.UUID, object_key: str, **kwargs: Any
        ) -> None:
            self.deleted.append((tenant_id, object_key))
            self.objects.pop(object_key, None)

        def _get_client(self) -> _Storage:
            return self

        def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "Contents": [
                    {
                        "Key": f"{owner}/spill/{digest}",
                        "LastModified": SimpleNamespace(timestamp=lambda: 1),
                    },
                    {
                        "Key": f"{owner}/attachments/{digest}",
                        "LastModified": SimpleNamespace(timestamp=lambda: 1),
                    },
                    {
                        "Key": f"{owner}/spill/{'b' * 64}",
                        "LastModified": SimpleNamespace(timestamp=lambda: 3),
                    },
                    {
                        "Key": f"not-a-tenant/spill/{digest}",
                        "LastModified": SimpleNamespace(timestamp=lambda: 1),
                    },
                ],
                "IsTruncated": False,
            }

    fake = _Storage()
    monkeypatch.setattr(spill_module, "StorageService", lambda settings: fake)
    settings = _settings(deepspace_spill_backend="storage", minio_bucket="private-spills")
    backend = ObjectStorageSpillBackend(settings)
    owner = uuid.uuid4()
    payload = b"object-spill"
    digest = hashlib.sha256(payload).hexdigest()

    key = backend.put(tenant_id=owner, digest=digest, payload=payload)
    assert key == f"{owner}/spill/{digest}"
    assert backend.put(tenant_id=owner, digest=digest, payload=payload) == key
    assert backend.get(tenant_id=owner, digest=digest) == payload
    assert backend.cleanup_expired(older_than=2) == 1
    assert fake.deleted == [(owner, key)]


def test_object_spill_backend_uses_private_local_storage_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.spill_store as spill_module

    class LocalStorage:
        is_local = True

        def __init__(self) -> None:
            self.local_root = tmp_path / "storage"

    storage = LocalStorage()
    monkeypatch.setattr(spill_module, "StorageService", lambda settings: storage)
    settings = _settings(minio_bucket="private", deepspace_spill_backend="storage")
    backend = ObjectStorageSpillBackend(settings)
    tenant_id = uuid.uuid4()
    payload = b"local-object"
    digest = hashlib.sha256(payload).hexdigest()
    key = backend.put(tenant_id=tenant_id, digest=digest, payload=payload)
    target = storage.local_root / "private" / key
    assert backend.get(tenant_id=tenant_id, digest=digest) == payload
    assert backend.put(tenant_id=tenant_id, digest=digest, payload=payload) == key
    assert target.stat().st_mode & 0o077 == 0
    os.utime(target, (1, 1))
    assert backend.cleanup_expired(older_than=2) == 1
    with pytest.raises(SpillStoreError, match="unavailable"):
        backend.get(tenant_id=tenant_id, digest=digest)
    assert not (storage.local_root / "private" / str(tenant_id) / "spill").exists()


def test_object_spill_backend_rejects_bad_local_roots_and_tenant_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.spill_store as spill_module

    class LocalStorage:
        is_local = True
        local_root = tmp_path / "storage"

    storage = LocalStorage()
    monkeypatch.setattr(spill_module, "StorageService", lambda settings: storage)
    backend = ObjectStorageSpillBackend(_settings(minio_bucket="private"))
    tenant_id = uuid.uuid4()
    digest = hashlib.sha256(b"payload").hexdigest()
    key = f"{tenant_id}/spill/{digest}"

    with pytest.raises(SpillStoreError, match="object key"):
        backend._local_path("../spill/not-a-digest", create=False)
    with pytest.raises(SpillStoreError, match="tenant"):
        backend._local_path(f"not-a-uuid/spill/{digest}", create=False)
    with pytest.raises(FileNotFoundError):
        backend._local_path(key, create=False)

    bucket = storage.local_root / "private"
    bucket.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    tenant_path = bucket / str(tenant_id)
    tenant_path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SpillStoreError, match="not a real directory"):
        backend._local_path(key, create=False)


def test_object_spill_backend_rejects_tampering_and_symlink_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.spill_store as spill_module

    class LocalStorage:
        is_local = True
        local_root = tmp_path / "storage"

    storage = LocalStorage()
    monkeypatch.setattr(spill_module, "StorageService", lambda settings: storage)
    backend = ObjectStorageSpillBackend(_settings(minio_bucket="private"))
    tenant_id = uuid.uuid4()
    payload = b"expected"
    digest = hashlib.sha256(payload).hexdigest()
    target = backend._local_path(f"{tenant_id}/spill/{digest}")
    target.write_bytes(b"tampered")
    with pytest.raises(SpillStoreError, match="integrity"):
        backend.put(tenant_id=tenant_id, digest=digest, payload=payload)

    target.unlink()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(payload)
    target.symlink_to(outside)
    with pytest.raises(SpillStoreError, match="regular file"):
        backend.put(tenant_id=tenant_id, digest=digest, payload=payload)
    with pytest.raises(SpillStoreError, match="digest"):
        backend.put(tenant_id=tenant_id, digest="0" * 64, payload=payload)


def test_object_spill_backend_wraps_remote_storage_write_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.deepspace.services.spill_store as spill_module
    from app.system.services.storage_service import StorageServiceError

    class RemoteStorage:
        is_local = False

        def object_exists(self, **_kwargs: Any) -> bool:
            return False

        def put_object_bytes(self, **_kwargs: Any) -> None:
            raise StorageServiceError(
                code="unavailable", message="object storage is unavailable", retryable=True
            )

    monkeypatch.setattr(spill_module, "StorageService", lambda _settings: RemoteStorage())
    backend = ObjectStorageSpillBackend(_settings(deepspace_spill_backend="storage"))
    payload = b"durable result"

    with pytest.raises(SpillStoreError, match="could not be stored"):
        backend.put(
            tenant_id=uuid.uuid4(),
            digest=hashlib.sha256(payload).hexdigest(),
            payload=payload,
        )


def test_local_spill_backend_recovers_from_a_racing_duplicate_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.spill_store as spill_module

    backend = LocalFileSpillBackend(tmp_path / "spill")
    tenant_id = uuid.uuid4()
    payload = b"racing write"
    digest = hashlib.sha256(payload).hexdigest()
    original_link = os.link

    def create_same_target_then_raise(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        follow_symlinks: bool = True,
    ) -> None:
        del source
        Path(destination).write_bytes(payload)
        raise FileExistsError

    monkeypatch.setattr(spill_module.os, "link", create_same_target_then_raise)
    key = backend.put(tenant_id=tenant_id, digest=digest, payload=payload)
    assert backend.get(tenant_id=tenant_id, digest=digest) == payload

    def create_corrupt_target_then_raise(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        follow_symlinks: bool = True,
    ) -> None:
        del source, follow_symlinks
        Path(destination).write_bytes(b"corrupt concurrent object")
        raise FileExistsError

    monkeypatch.setattr(spill_module.os, "link", create_corrupt_target_then_raise)
    Path(backend.root / key).unlink()
    with pytest.raises(SpillStoreError, match="integrity"):
        backend.put(tenant_id=tenant_id, digest=digest, payload=payload)
    monkeypatch.setattr(spill_module.os, "link", original_link)


def test_local_spill_backend_refuses_symlinked_root_and_tenant_directories(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SpillStoreError, match="root must be a real directory"):
        LocalFileSpillBackend(root_link)

    backend = LocalFileSpillBackend(tmp_path / "spill")
    tenant_id = uuid.uuid4()
    (backend.root / str(tenant_id)).symlink_to(outside, target_is_directory=True)
    payload = b"payload"
    with pytest.raises(SpillStoreError, match="not a real directory"):
        backend.put(
            tenant_id=tenant_id,
            digest=hashlib.sha256(payload).hexdigest(),
            payload=payload,
        )


def test_local_spill_backend_put_rejects_symlink_and_bad_existing_content(
    tmp_path: Path,
) -> None:
    backend = LocalFileSpillBackend(tmp_path / "spill")
    tenant_id = uuid.uuid4()
    payload = b"payload"
    digest = hashlib.sha256(payload).hexdigest()
    key = backend.put(tenant_id=tenant_id, digest=digest, payload=payload)
    target = backend.root / key
    target.write_bytes(b"corrupt")
    with pytest.raises(SpillStoreError, match="integrity"):
        backend.put(tenant_id=tenant_id, digest=digest, payload=payload)

    target.unlink()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(payload)
    target.symlink_to(outside)
    with pytest.raises(SpillStoreError, match="regular file"):
        backend.put(tenant_id=tenant_id, digest=digest, payload=payload)


def test_object_spill_backend_cleanup_paginates_and_stops_on_backend_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.spill_store as spill_module

    owner = uuid.uuid4()
    digest = "a" * 64

    class Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            if kwargs.get("ContinuationToken") == "next":
                return {
                    "Contents": [
                        {
                            "Key": f"{owner}/spill/{digest}",
                            "LastModified": SimpleNamespace(timestamp=lambda: 1),
                        }
                    ],
                    "IsTruncated": False,
                }
            return {
                "Contents": [
                    {
                        "Key": "invalid/spill/not-a-digest",
                        "LastModified": SimpleNamespace(timestamp=lambda: 1),
                    },
                    {
                        "Key": f"{owner}/attachments/{digest}",
                        "LastModified": SimpleNamespace(timestamp=lambda: 1),
                    },
                    {"Key": f"{owner}/spill/{'b' * 64}", "LastModified": None},
                ],
                "IsTruncated": True,
                "NextContinuationToken": "next",
            }

    class RemoteStorage:
        is_local = False
        local_root = tmp_path

        def __init__(self) -> None:
            self.client = Client()
            self.deleted: list[str] = []

        def _get_client(self) -> Client:
            return self.client

        def delete_tenant_object(self, *, object_key: str, **kwargs: Any) -> None:
            self.deleted.append(object_key)

    storage = RemoteStorage()
    monkeypatch.setattr(spill_module, "StorageService", lambda settings: storage)
    backend = ObjectStorageSpillBackend(_settings(minio_bucket="private"))

    assert backend.cleanup_expired(older_than=2) == 1
    assert storage.deleted == [f"{owner}/spill/{digest}"]
    assert storage.client.calls[1]["ContinuationToken"] == "next"

    def unavailable(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise RuntimeError("storage unavailable")

    storage.client.list_objects_v2 = unavailable  # type: ignore[method-assign]
    assert backend.cleanup_expired(older_than=2) == 0

    def unavailable_client() -> Client:
        raise RuntimeError("client unavailable")

    storage._get_client = unavailable_client  # type: ignore[method-assign]
    assert backend.cleanup_expired(older_than=2) == 0


def test_spill_store_rejects_unknown_backend_and_contains_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.spill_store as spill_module

    with pytest.raises(SpillStoreError, match="Unknown spill storage backend"):
        SpillStore(_settings(deepspace_spill_backend="unknown"))

    class BrokenBackend:
        root = tmp_path

        def put(self, *, tenant_id: uuid.UUID, digest: str, payload: bytes) -> str:
            del tenant_id, payload
            return f"owner/spill/{digest}"

        def get(self, *, tenant_id: uuid.UUID, digest: str) -> bytes:
            del tenant_id, digest
            return b""

        def cleanup_expired(self, *, older_than: float) -> int:
            del older_than
            raise RuntimeError("cleanup is best effort")

    monkeypatch.setattr(spill_module, "_CLEANUP_LAST_RUN", {})
    store = SpillStore(_settings(), backend=BrokenBackend())
    reference = store.put(tenant_id=uuid.uuid4(), payload="kept")
    assert reference.size_bytes == len(b"kept")


def test_local_spill_cleanup_removes_only_expired_spill_objects(tmp_path: Path) -> None:
    backend = LocalFileSpillBackend(tmp_path / "spill")
    tenant_id = uuid.uuid4()
    old = b"old"
    old_digest = hashlib.sha256(old).hexdigest()
    current = b"current"
    current_digest = hashlib.sha256(current).hexdigest()
    backend.put(tenant_id=tenant_id, digest=old_digest, payload=old)
    backend.put(tenant_id=tenant_id, digest=current_digest, payload=current)
    old_path = backend.root / str(tenant_id) / "spill" / old_digest
    os.utime(old_path, (1, 1))
    other = backend.root / "unrelated.txt"
    other.write_text("keep", encoding="utf-8")

    assert backend.cleanup_expired(older_than=10) == 1
    assert not old_path.exists()
    assert (backend.root / str(tenant_id) / "spill" / current_digest).exists()
    assert other.read_text(encoding="utf-8") == "keep"


def test_spill_result_preserves_small_results_and_rejects_storage_ceiling(tmp_path: Path) -> None:
    settings = _settings(deepspace_spill_local_root=str(tmp_path / "spill"))
    payload = {"status": "ok", "text": "small"}
    assert (
        spill_result(settings=settings, tenant_id=uuid.uuid4(), tool_name="test", payload=payload)[
            0
        ]
        is payload
    )
    with pytest.raises(SpillStoreError, match="ceiling"):
        spill_result(
            settings=settings,
            tenant_id=uuid.uuid4(),
            tool_name="test",
            payload="x" * 200,
            threshold_chars=10,
            max_chars=100,
        )


def test_spill_result_returns_head_tail_reference_and_image_counts(tmp_path: Path) -> None:
    settings = _settings(
        deepspace_spill_local_root=str(tmp_path / "spill"),
        deepspace_spill_threshold_chars=20,
    )
    owner = uuid.uuid4()
    original = {"content": [{"type": "text", "text": "head " + "x" * 200 + " tail"}]}

    result, reference = spill_result(
        settings=settings,
        tenant_id=owner,
        tool_name="remote",
        payload=original,
        threshold_chars=20,
        preview_budget_tokens=32,
        source={"path": "reports/q4.txt"},
    )

    assert reference is not None
    assert result["spill_ref"] == reference.sha256
    assert result["preview_truncated"] is True
    assert result["source"] == {"path": "reports/q4.txt"}
    assert "head" in result["preview"] and "tail" in result["preview"]
    assert (
        SpillStore(settings).get(tenant_id=owner, sha256=reference.sha256).startswith('{"content"')
    )


def test_spill_result_falls_back_to_text_for_circular_payloads(tmp_path: Path) -> None:
    settings = _settings(deepspace_spill_local_root=str(tmp_path / "spill"))
    payload: list[Any] = []
    payload.append(payload)

    result, reference = spill_result(
        settings=settings,
        tenant_id=uuid.uuid4(),
        tool_name="recursive",
        payload=payload,
        threshold_chars=0,
        max_chars=10_000,
    )

    assert reference is not None
    assert result["status"] == "spilled"
    assert "[[...]]" in SpillStore(settings).get(
        tenant_id=uuid.UUID(reference.object_key.split("/", 1)[0]),
        sha256=reference.sha256,
    )


def test_spill_preview_falls_back_when_structured_payload_is_circular() -> None:
    from app.deepspace.services.spill_policy import result_preview

    payload: dict[str, Any] = {"type": "text"}
    content = [payload]
    payload["text"] = content

    preview = result_preview({"content": content}, "{'content': [{...}]}", 128)

    assert "{'content': [{...}]}" in preview.preview
