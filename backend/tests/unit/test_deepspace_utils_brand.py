"""Tests for branded/nominal types utilities."""

# Uppercase locals intentionally mirror branded type constructors.
# ruff: noqa: N806

from __future__ import annotations

import uuid
from dataclasses import is_dataclass

import pytest

from app.deepspace.utils.brand import (
    AgentId,
    Branded,
    BrandedBytes,
    BrandedFloat,
    BrandedInt,
    BrandedStr,
    BrandedUUID,
    ConversationId,
    IntId,
    ModelId,
    ProviderId,
    RunId,
    SessionId,
    StrId,
    TenantId,
    ToolCallId,
    UserId,
    UUIDId,
    WorkspaceId,
    brand_int,
    brand_string,
    brand_uuid,
    branded,
    get_brand_name,
    is_branded,
    unbrand,
)


class TestBrandedBasics:
    """Tests for Branded base class."""

    def test_branded_is_dataclass(self) -> None:
        assert is_dataclass(Branded)

    def test_branded_str_creation(self) -> None:
        b = BrandedStr("test")
        assert b.value == "test"
        assert str(b) == "test"

    def test_branded_int_creation(self) -> None:
        b = BrandedInt(42)
        assert b.value == 42
        assert int(b) == 42

    def test_branded_float_creation(self) -> None:
        b = BrandedFloat(3.14)
        assert b.value == 3.14
        assert float(b) == 3.14

    def test_branded_uuid_creation(self) -> None:
        u = uuid.uuid4()
        b = BrandedUUID(u)
        assert b.value == u
        assert str(b) == str(u)

    def test_branded_bytes_creation(self) -> None:
        b = BrandedBytes(b"data")
        assert b.value == b"data"
        assert bytes(b) == b"data"

    def test_branded_repr(self) -> None:
        b = BrandedStr("test")
        assert repr(b) == "BrandedStr('test')"

    def test_branded_equality_with_same_type(self) -> None:
        a = BrandedStr("same")
        b = BrandedStr("same")
        assert a == b

    def test_branded_equality_with_raw_value(self) -> None:
        b = BrandedStr("test")
        assert b == "test"
        assert "test" == b

    def test_branded_inequality(self) -> None:
        a = BrandedStr("a")
        b = BrandedStr("b")
        assert a != b
        assert a != "b"

    def test_branded_hash(self) -> None:
        b = BrandedStr("test")
        assert hash(b) == hash("test")
        # Can be used in sets/dicts
        s = {b, "test"}
        assert len(s) == 1

    def test_branded_bool(self) -> None:
        assert bool(BrandedStr("nonempty"))
        assert not bool(BrandedStr(""))
        assert bool(BrandedInt(1))
        assert not bool(BrandedInt(0))

    def test_branded_len(self) -> None:
        b = BrandedStr("hello")
        assert len(b) == 5


class TestBrandedFunction:
    """Tests for branded() factory function."""

    def test_create_branded_str_type(self) -> None:
        UserId = branded("UserId", str)
        uid = UserId("user-123")
        assert type(uid).__name__ == "UserId"
        assert uid.value == "user-123"
        assert isinstance(uid, BrandedStr)

    def test_create_branded_int_type(self) -> None:
        PortNumber = branded("PortNumber", int)
        port = PortNumber(8080)
        assert type(port).__name__ == "PortNumber"
        assert port.value == 8080
        assert isinstance(port, BrandedInt)

    def test_create_branded_uuid_type(self) -> None:
        SessionId = branded("SessionId", uuid.UUID)
        u = uuid.uuid4()
        sid = SessionId(u)
        assert type(sid).__name__ == "SessionId"
        assert sid.value == u
        assert isinstance(sid, BrandedUUID)

    def test_brand_prevents_mixing(self) -> None:
        UserId = branded("UserId", str)
        TenantId = branded("TenantId", str)

        uid = UserId("abc")
        tid = TenantId("abc")

        # Values are equal but types differ
        assert uid.value == tid.value
        # But they are not the same type
        assert type(uid) is not type(tid)

    def test_brand_docstring(self) -> None:
        UserId = branded("UserId", str)
        assert "Branded str for UserId" in UserId.__doc__


class TestBrandHelpers:
    """Tests for brand_string, brand_int, brand_uuid helpers."""

    def test_brand_string(self) -> None:
        b = brand_string("value", "MyBrand")
        assert isinstance(b, BrandedStr)
        assert b.value == "value"

    def test_brand_int(self) -> None:
        b = brand_int(42, "MyBrand")
        assert isinstance(b, BrandedInt)
        assert b.value == 42

    def test_brand_uuid(self) -> None:
        u = uuid.uuid4()
        b = brand_uuid(u, "MyBrand")
        assert isinstance(b, BrandedUUID)
        assert b.value == u


class TestBrandIntrospection:
    """Tests for is_branded, get_brand_name, unbrand."""

    def test_is_branded_true(self) -> None:
        UserId = branded("UserId", str)
        uid = UserId("test")
        assert is_branded(uid, "UserId")

    def test_is_branded_false_wrong_brand(self) -> None:
        UserId = branded("UserId", str)
        TenantId = branded("TenantId", str)
        uid = UserId("test")
        assert not is_branded(uid, "TenantId")
        assert is_branded(TenantId("tenant"), "TenantId")

    def test_is_branded_false_not_branded(self) -> None:
        assert not is_branded("plain string", "UserId")
        assert not is_branded(42, "PortNumber")
        assert not is_branded(None, "Anything")

    def test_get_brand_name(self) -> None:
        UserId = branded("UserId", str)
        uid = UserId("test")
        assert get_brand_name(uid) == "UserId"

    def test_unbrand(self) -> None:
        UserId = branded("UserId", str)
        uid = UserId("test")
        assert unbrand(uid) == "test"
        assert isinstance(unbrand(uid), str)


class TestPredefinedTypes:
    """Tests for predefined branded types."""

    def test_session_id(self) -> None:
        sid = SessionId("sess-123")
        assert sid.value == "sess-123"
        assert isinstance(sid, BrandedStr)

    def test_conversation_id(self) -> None:
        cid = ConversationId("conv-456")
        assert cid.value == "conv-456"

    def test_agent_id(self) -> None:
        aid = AgentId("agent-789")
        assert aid.value == "agent-789"

    def test_tool_call_id(self) -> None:
        tcid = ToolCallId("tool-abc")
        assert tcid.value == "tool-abc"

    def test_run_id(self) -> None:
        rid = RunId("run-xyz")
        assert rid.value == "run-xyz"

    def test_tenant_id(self) -> None:
        tid = TenantId("tenant-1")
        assert tid.value == "tenant-1"

    def test_user_id(self) -> None:
        uid = UserId("user-1")
        assert uid.value == "user-1"

    def test_workspace_id(self) -> None:
        wid = WorkspaceId("ws-1")
        assert wid.value == "ws-1"

    def test_model_id(self) -> None:
        mid = ModelId("gpt-4")
        assert mid.value == "gpt-4"

    def test_provider_id(self) -> None:
        pid = ProviderId("openai")
        assert pid.value == "openai"

    def test_type_aliases(self) -> None:
        # StrId, IntId, UUIDId are type aliases
        sid: StrId = SessionId("test")
        assert sid.value == "test"

        port: IntId = branded("Port", int)(8080)
        assert port.value == 8080

        uid: UUIDId = branded("UUID", uuid.UUID)(uuid.uuid4())
        assert isinstance(uid.value, uuid.UUID)


class TestBrandedUsagePatterns:
    """Tests for common usage patterns."""

    def test_dict_keys(self) -> None:
        UserId = branded("UserId", str)
        data = {UserId("a"): 1, UserId("b"): 2}
        assert data[UserId("a")] == 1
        assert len(data) == 2

    def test_set_membership(self) -> None:
        UserId = branded("UserId", str)
        ids = {UserId("a"), UserId("b"), UserId("a")}
        assert len(ids) == 2

    def test_sorting(self) -> None:
        UserId = branded("UserId", str)
        ids = [UserId("c"), UserId("a"), UserId("b")]
        sorted_ids = sorted(ids)
        assert [u.value for u in sorted_ids] == ["a", "b", "c"]

    def test_f_string(self) -> None:
        UserId = branded("UserId", str)
        uid = UserId("user-123")
        assert f"{uid}" == "user-123"

    def test_json_serialization(self) -> None:
        import json

        UserId = branded("UserId", str)
        uid = UserId("user-123")
        # Branded types should serialize as their value
        serialized = json.dumps({"user_id": uid})
        assert '"user_id": "user-123"' in serialized


class TestBrandedFrozen:
    """Tests that branded types are frozen/immutable."""

    def test_cannot_modify_value(self) -> None:
        UserId = branded("UserId", str)
        uid = UserId("test")
        # Should not be able to modify the frozen dataclass
        with pytest.raises(AttributeError):
            uid.value = "other"  # type: ignore[misc]

    def test_slots_prevent_new_attributes(self) -> None:
        UserId = branded("UserId", str)
        uid = UserId("test")
        with pytest.raises(AttributeError):
            uid.new_attr = "value"  # type: ignore[misc]


class TestBrandedWithUUID:
    """Tests for UUID-specific branded types."""

    def test_uuid_generation(self) -> None:
        SessionId = branded("SessionId", uuid.UUID)
        # Can create from uuid4
        sid = SessionId(uuid.uuid4())
        assert isinstance(sid.value, uuid.UUID)

    def test_uuid_equality(self) -> None:
        SessionId = branded("SessionId", uuid.UUID)
        u = uuid.uuid4()
        a = SessionId(u)
        b = SessionId(u)
        assert a == b
        assert a == u  # Equality with raw UUID

    def test_uuid_string_conversion(self) -> None:
        SessionId = branded("SessionId", uuid.UUID)
        u = uuid.UUID("12345678-1234-5678-1234-567812345678")
        sid = SessionId(u)
        assert str(sid) == "12345678-1234-5678-1234-567812345678"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
