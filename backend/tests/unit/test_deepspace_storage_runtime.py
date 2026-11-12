"""Production storage-hub startup and persistence tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.deepspace.services.storage_domain import DomainSpec, define_domain, domain_table
from app.deepspace.services.storage_runtime import DeepSpaceStorageRuntime

pytestmark = pytest.mark.unit_no_db


@pytest.mark.asyncio
async def test_storage_runtime_mounts_persistent_backends_and_domain(tmp_path: Path) -> None:
    spec = define_domain(
        DomainSpec(
            name="runtime_probe",
            version=1,
            tables={"items": domain_table("json")},
        )
    )
    runtime = DeepSpaceStorageRuntime(tmp_path)
    assert runtime.storage.backend.names() == ["json", "sqlite"]
    assert runtime.storage.domain is runtime.domain
    assert (tmp_path / "db" / "deepspace-kv.sqlite3").is_file()

    domain = await runtime.storage.domain.open(spec)
    await domain.table("items").put("persisted", {"value": 7})
    await runtime.close()
    await runtime.close()

    reopened = DeepSpaceStorageRuntime(tmp_path)
    try:
        restored = await reopened.storage.domain.open(spec)
        assert restored.table("items").get("persisted") == {"value": 7}
    finally:
        await reopened.close()
