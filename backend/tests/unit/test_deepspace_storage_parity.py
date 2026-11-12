"""Tests for DeepSpace storage parity with DeepSeek Harness."""

import os
import tempfile

import pytest

from app.deepspace.services.storage import (
    UNIT_NAME_RE_COMPILED,
    BackendRegistry,
    KvUnitDescriptor,
    Storage,
    StorageBackend,
    StorageError,
    storage_backend_service_key,
)
from app.deepspace.services.storage_domain import (
    DomainError,
    DomainFacility,
    DomainGlobalSpec,
    DomainSpec,
    define_domain,
    descriptor_of,
    domain_table,
)
from app.deepspace.services.storage_json import (
    SAFE_KEY_RE,
    JsonStorageBackend,
    UnitState,
    parse,
    parse_record,
    serialize,
    serialize_record,
)
from app.deepspace.services.storage_sqlite import (
    STORAGE_SQLITE_SCHEMA_VERSION,
    SqliteStorageBackend,
    open_database,
    record_table_name,
)

# === Storage Hub Tests ===


def test_unit_name_re():
    assert UNIT_NAME_RE_COMPILED.match("valid_name")
    assert not UNIT_NAME_RE_COMPILED.match("Invalid")
    assert not UNIT_NAME_RE_COMPILED.match("123start")


def test_backend_registry_basic():
    reg = BackendRegistry()

    class MockBackend(StorageBackend):
        pass

    b = MockBackend()
    disp = reg.register("json", b)
    assert reg.names() == ["json"]
    assert reg.get("json") is b
    disp()
    assert reg.names() == []


def test_backend_registry_duplicate():
    reg = BackendRegistry()

    class MockBackend(StorageBackend):
        pass

    b = MockBackend()
    reg.register("json", b)
    with pytest.raises(StorageError) as exc:
        reg.register("json", b)
    assert exc.value.code == "duplicate-backend"


def test_backend_registry_not_found():
    reg = BackendRegistry()
    with pytest.raises(StorageError) as exc:
        reg.get("nonexistent")
    assert exc.value.code == "backend-not-found"


def test_storage_hub_mount():
    s = Storage()
    disp = s.mount("test", "facility")
    assert s.form("test") == "facility"
    disp()
    with pytest.raises(StorageError) as exc:
        s.form("test")
    assert exc.value.code == "form-not-mounted"


def test_storage_hub_duplicate_mount():
    s = Storage()
    s.mount("test", "facility")
    with pytest.raises(StorageError) as exc:
        s.mount("test", "facility2")
    assert exc.value.code == "duplicate-mount"


def test_kv_unit_descriptor():
    desc = KvUnitDescriptor(
        "test", 1, ["t1", "t2"], has_global=True, layout="single", compatible_versions=[0]
    )
    d = desc.to_dict()
    assert d["name"] == "test"
    assert d["version"] == 1
    assert d["tables"] == ["t1", "t2"]
    assert d["hasGlobal"] is True
    assert d["layout"] == "single"
    assert d["compatibleVersions"] == [0]


def test_storage_backend_service_key():
    assert storage_backend_service_key("json") == "storage.backend.json"


# === Domain Layer Tests ===


def test_domain_spec_creation():
    spec = DomainSpec(name="test", version=1, tables={"t": domain_table("schema")})
    assert spec.name == "test"
    assert spec.version == 1
    assert "t" in spec.tables


def test_define_domain_valid():
    spec = DomainSpec(name="test_domain", version=1, tables={"users": domain_table("schema")})
    ds = define_domain(spec)
    assert ds.name == "test_domain"


def test_define_domain_invalid_name():
    spec = DomainSpec(name="Invalid", version=1, tables={})
    with pytest.raises(ValueError):
        define_domain(spec)


def test_define_domain_invalid_version():
    spec = DomainSpec(name="test", version=-1, tables={})
    with pytest.raises(ValueError):
        define_domain(spec)


def test_descriptor_of():
    spec = DomainSpec(name="test", version=1, tables={"users": domain_table("schema")})
    desc = descriptor_of(spec)
    assert desc.name == "test"
    assert desc.version == 1
    assert desc.tables == ["users"]
    assert desc.has_global is False


def test_descriptor_of_with_global():
    spec = DomainSpec(
        name="test",
        version=1,
        tables={"users": domain_table("schema")},
        global_spec=DomainGlobalSpec(schema=None, initial={"count": 0}),
    )
    desc = descriptor_of(spec)
    assert desc.has_global is True


# === JSON Backend Tests ===


def test_serialize_parse_roundtrip():
    desc = KvUnitDescriptor("test", 1, ["t1"], has_global=True, layout="single")
    state = UnitState(version=1, global_val={"x": 1}, tables={"t1": {"k1": {"v": 2}}})
    text = serialize("test", state)
    parsed = parse(text, desc)
    assert parsed.global_val == {"x": 1}
    assert parsed.tables["t1"] == {"k1": {"v": 2}}


def test_parse_version_mismatch():
    desc = KvUnitDescriptor("test", 2, ["t1"], has_global=False)
    text = '{"unit": {"name": "test", "version": 1}, "global": null, "tables": {"t1": {}}}'
    with pytest.raises(StorageError) as exc:
        parse(text, desc)
    assert exc.value.code == "version-mismatch"


def test_parse_malformed():
    desc = KvUnitDescriptor("test", 1, ["t1"], has_global=False)
    with pytest.raises(StorageError) as exc:
        parse("not json", desc)
    assert exc.value.code == "malformed-medium"


def test_serialize_record_parse_record():
    text = serialize_record(1, {"hello": "world"})
    value = parse_record(text, [1, 2])
    assert value == {"hello": "world"}

    # Foreign version
    value2 = parse_record(text, [2, 3])
    assert value2 is None

    # Malformed
    value3 = parse_record("not json", [1])
    assert value3 is None


def test_safe_key_re():
    assert SAFE_KEY_RE.match("valid-key_123")
    assert not SAFE_KEY_RE.match("invalid/key")
    assert not SAFE_KEY_RE.match("")


@pytest.mark.asyncio
async def test_json_single_layout():
    tmpdir = tempfile.mkdtemp()
    backend = JsonStorageBackend(tmpdir)
    desc = KvUnitDescriptor("test_unit", 1, ["users"], has_global=True, layout="single")
    unit = await backend.open(desc)
    await unit.put_record("users", "u1", {"name": "Alice"})
    result = await unit.load_all()
    assert result["tables"]["users"]["u1"] == {"name": "Alice"}
    await unit.set_global({"count": 1})
    result2 = await unit.load_all()
    assert result2["global"] == {"count": 1}
    await unit.delete_record("users", "u1")
    result3 = await unit.load_all()
    assert "u1" not in result3["tables"]["users"]
    await unit.close()
    await backend.close()


@pytest.mark.asyncio
async def test_json_per_record_layout():
    tmpdir = tempfile.mkdtemp()
    backend = JsonStorageBackend(tmpdir)
    desc = KvUnitDescriptor("test_unit2", 1, ["users"], has_global=True, layout="per-record")
    unit = await backend.open(desc)
    await unit.put_record("users", "u1", {"name": "Bob"})
    result = await unit.load_all()
    assert result["tables"]["users"]["u1"] == {"name": "Bob"}
    await unit.close()
    await backend.close()


@pytest.mark.asyncio
async def test_json_double_open():
    tmpdir = tempfile.mkdtemp()
    backend = JsonStorageBackend(tmpdir)
    desc = KvUnitDescriptor("test_unit", 1, ["users"], has_global=False)
    await backend.open(desc)
    with pytest.raises(ValueError):
        await backend.open(desc)
    await backend.close()


# === SQLite Backend Tests ===


def test_sqlite_schema_version():
    assert STORAGE_SQLITE_SCHEMA_VERSION == 1


def test_record_table_name():
    assert record_table_name("my_unit", "users") == "u_my_unit_users"


def test_open_database():
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    conn = open_database(db_path, "wal")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == STORAGE_SQLITE_SCHEMA_VERSION
    conn.close()


@pytest.mark.asyncio
async def test_sqlite_kv_basic():
    backend = SqliteStorageBackend({"path": ":memory:", "journalMode": "wal"})
    desc = KvUnitDescriptor("test_unit", 1, ["users"], has_global=True)
    unit = await backend.open(desc)
    await unit.put_record("users", "u1", {"name": "Alice"})
    result = await unit.load_all()
    assert result["tables"]["users"]["u1"] == {"name": "Alice"}
    await unit.set_global({"count": 1})
    result2 = await unit.load_all()
    assert result2["global"] == {"count": 1}
    await unit.delete_record("users", "u1")
    result3 = await unit.load_all()
    assert "u1" not in result3["tables"]["users"]
    await unit.close()
    await backend.close()


@pytest.mark.asyncio
async def test_sqlite_double_open():
    backend = SqliteStorageBackend({"path": ":memory:", "journalMode": "wal"})
    desc = KvUnitDescriptor("test_unit", 1, ["users"], has_global=False)
    await backend.open(desc)
    with pytest.raises(ValueError):
        await backend.open(desc)
    await backend.close()


@pytest.mark.asyncio
async def test_sqlite_version_mismatch():
    backend = SqliteStorageBackend({"path": ":memory:", "journalMode": "wal"})
    desc1 = KvUnitDescriptor("test_unit", 1, ["users"], has_global=False)
    await backend.open(desc1)
    await backend.close()

    # Reopen with different version (not compatible)
    # Can't test version mismatch on :memory: since it's a new db
    # Test with file-based db instead
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    backend3 = SqliteStorageBackend({"path": db_path, "journalMode": "wal"})
    desc2 = KvUnitDescriptor("test_unit", 1, ["users"], has_global=False)
    await backend3.open(desc2)
    await backend3.close()

    backend4 = SqliteStorageBackend({"path": db_path, "journalMode": "wal"})
    desc3 = KvUnitDescriptor("test_unit", 2, ["users"], has_global=False)
    with pytest.raises(StorageError) as exc:
        await backend4.open(desc3)
    assert exc.value.code == "version-mismatch"
    await backend4.close()


# === Domain Layer Integration Tests ===


@pytest.mark.asyncio
async def test_domain_with_json_backend():
    tmpdir = tempfile.mkdtemp()
    backend = JsonStorageBackend(tmpdir)
    storage = Storage()
    storage.backend.register("json", backend)

    spec = DomainSpec(
        name="test_domain",
        version=1,
        tables={"users": domain_table("schema")},
        global_spec=DomainGlobalSpec(schema=None, initial={"count": 0}),
    )
    spec = define_domain(spec)

    ctx = type("Ctx", (), {"storage": storage, "emit": lambda self, ev, data: None})()
    facility = DomainFacility(ctx, {"backend": "json"})
    domain = await facility.open(spec)

    table = domain.table("users")
    await table.put("u1", {"name": "Alice"})
    assert table.get("u1") == {"name": "Alice"}
    assert table.size == 1

    g = domain.global_handle
    assert g.get() == {"count": 0}
    await g.set({"count": 5})
    assert g.get() == {"count": 5}

    await table.delete("u1")
    assert table.get("u1") is None

    await domain.close()
    await backend.close()


@pytest.mark.asyncio
async def test_domain_with_sqlite_backend():
    backend = SqliteStorageBackend({"path": ":memory:", "journalMode": "wal"})
    storage = Storage()
    storage.backend.register("sqlite", backend)

    spec = DomainSpec(
        name="test_domain",
        version=1,
        tables={"users": domain_table("schema")},
        global_spec=DomainGlobalSpec(schema=None, initial={"count": 0}),
    )
    spec = define_domain(spec)

    ctx = type("Ctx", (), {"storage": storage, "emit": lambda self, ev, data: None})()
    facility = DomainFacility(ctx, {"backend": "sqlite"})
    domain = await facility.open(spec)

    table = domain.table("users")
    await table.put("u1", {"name": "Alice"})
    await table.put("u2", {"name": "Bob"})
    assert table.size == 2

    await domain.close()
    await backend.close()


@pytest.mark.asyncio
async def test_domain_events():
    events = []
    tmpdir = tempfile.mkdtemp()
    backend = JsonStorageBackend(tmpdir)
    storage = Storage()
    storage.backend.register("json", backend)

    spec = DomainSpec(name="test_domain", version=1, tables={"users": domain_table("schema")})
    spec = define_domain(spec)

    ctx = type(
        "Ctx", (), {"storage": storage, "emit": lambda self, ev, data: events.append((ev, data))}
    )()
    facility = DomainFacility(ctx, {"backend": "json"})
    domain = await facility.open(spec)

    table = domain.table("users")
    await table.put("u1", {"name": "Alice"})
    assert len(events) == 1
    assert events[0][0] == "domain/changed"
    assert events[0][1]["operation"] == "put"

    await table.delete("u1")
    assert len(events) == 2
    assert events[1][1]["operation"] == "deleted"

    await domain.close()
    await backend.close()


@pytest.mark.asyncio
async def test_domain_update():
    tmpdir = tempfile.mkdtemp()
    backend = JsonStorageBackend(tmpdir)
    storage = Storage()
    storage.backend.register("json", backend)

    spec = DomainSpec(name="test_domain", version=1, tables={"users": domain_table("schema")})
    spec = define_domain(spec)

    ctx = type("Ctx", (), {"storage": storage, "emit": lambda self, ev, data: None})()
    facility = DomainFacility(ctx, {"backend": "json"})
    domain = await facility.open(spec)

    table = domain.table("users")
    await table.put("u1", {"name": "Alice"})
    result = await table.update("u1", lambda v: {**v, "name": "Alicia"})
    assert result["name"] == "Alicia"
    assert table.get("u1")["name"] == "Alicia"

    with pytest.raises(DomainError):
        await table.update("nonexistent", lambda v: v)

    await domain.close()
    await backend.close()


@pytest.mark.asyncio
async def test_domain_delete_many():
    tmpdir = tempfile.mkdtemp()
    backend = JsonStorageBackend(tmpdir)
    storage = Storage()
    storage.backend.register("json", backend)

    spec = DomainSpec(name="test_domain", version=1, tables={"users": domain_table("schema")})
    spec = define_domain(spec)

    ctx = type("Ctx", (), {"storage": storage, "emit": lambda self, ev, data: None})()
    facility = DomainFacility(ctx, {"backend": "json"})
    domain = await facility.open(spec)

    table = domain.table("users")
    await table.put("u1", {"name": "Alice"})
    await table.put("u2", {"name": "Bob"})
    await table.put("u3", {"name": "Charlie"})

    count = await table.delete_many(["u1", "u2", "nonexistent"])
    assert count == 2
    assert table.size == 1
    assert table.get("u3") is not None

    await domain.close()
    await backend.close()


# === Version & Compatibility Tests ===


def test_compatible_versions():
    desc = KvUnitDescriptor("test", 2, ["t1"], has_global=False, compatible_versions=[1])
    assert 1 in desc.compatible_versions


@pytest.mark.asyncio
async def test_json_compatible_versions():
    tmpdir = tempfile.mkdtemp()
    # Write v1 data
    backend1 = JsonStorageBackend(tmpdir)
    desc1 = KvUnitDescriptor("test", 1, ["users"], has_global=False, layout="single")
    unit1 = await backend1.open(desc1)
    await unit1.put_record("users", "u1", {"name": "Alice"})
    await unit1.close()
    await backend1.close()

    # Read with v2 (compatible with v1)
    backend2 = JsonStorageBackend(tmpdir)
    desc2 = KvUnitDescriptor(
        "test", 2, ["users"], has_global=False, layout="single", compatible_versions=[1]
    )
    unit2 = await backend2.open(desc2)
    result = await unit2.load_all()
    assert result["tables"]["users"]["u1"] == {"name": "Alice"}
    await unit2.close()
    await backend2.close()
