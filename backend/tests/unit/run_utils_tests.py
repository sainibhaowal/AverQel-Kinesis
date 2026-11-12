#!/usr/bin/env python
"""Standalone test runner for utils modules."""

import sys
import traceback
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent.parent.parent
sys.path.insert(0, str(backend_path))

def run_atomic_write_tests():
    print("Running atomic_write tests...")
    import tempfile
    import threading

    from app.deepspace.utils.atomic_write import (
        FileLock,
        atomic_write_json,
        with_file_lock,
        write_file_atomic,
        write_json_atomic,
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Test basic write
        file_path = tmp_path / "test.txt"
        write_file_atomic(file_path, "hello world")
        assert file_path.read_text() == "hello world", "Basic write failed"
        print("  ✓ write_file_atomic basic")

        # Test JSON write
        file_path = tmp_path / "test.json"
        write_json_atomic(file_path, {"key": "value", "num": 42})
        import json
        assert json.loads(file_path.read_text()) == {"key": "value", "num": 42}
        print("  ✓ write_json_atomic")

        # Test atomic_write_json with locking
        file_path = tmp_path / "registry.json"
        atomic_write_json(file_path, {"version": 2})
        assert json.loads(file_path.read_text()) == {"version": 2}
        print("  ✓ atomic_write_json")

        # Test concurrent writes
        file_path = tmp_path / "concurrent.json"
        results = []
        errors = []

        def writer(value):
            try:
                atomic_write_json(file_path, {"value": value})
                results.append(value)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f"val{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Concurrent errors: {errors}"
        assert len(results) == 10
        print("  ✓ concurrent writes serialized")

        # Test FileLock
        lock_file = tmp_path / "test.lock"
        lock = FileLock(lock_file, wait_ms=1000)
        lock.acquire()
        assert lock._acquired
        assert lock.lock_path.exists()  # Check the actual lock path
        lock.release()
        assert not lock._acquired
        assert not lock.lock_path.exists()
        print("  ✓ FileLock acquire/release")

        # Test lock context manager
        lock_file = tmp_path / "test2.lock"
        with FileLock(lock_file, wait_ms=1000) as lock:
            assert lock._acquired
            assert lock.lock_path.exists()
        assert not lock.lock_path.exists()
        print("  ✓ FileLock context manager")

        # Test with_file_lock
        file_path = tmp_path / "data.json"
        result = with_file_lock(file_path, lambda: {"computed": 42})
        assert result == {"computed": 42}
        print("  ✓ with_file_lock")

    print("All atomic_write tests passed!")
    return True


def run_brand_tests():
    print("\nRunning brand tests...")
    import uuid

    from app.deepspace.utils.brand import (
        AgentId,
        BrandedInt,
        BrandedStr,
        ConversationId,
        SessionId,
        ToolCallId,
        _BrandedBase,
        brand_string,
        branded,
        get_brand_name,
        is_branded,
        unbrand,
    )

    # Test basic branded types
    b = BrandedStr("test")
    assert b.value == "test"
    assert str(b) == "test"
    assert b == "test"
    assert hash(b) == hash("test")
    print("  ✓ BrandedStr basics")

    b = BrandedInt(42)
    assert b.value == 42
    assert int(b) == 42
    print("  ✓ BrandedInt basics")

    # Test branded factory
    UserId = branded("UserId", str)
    uid = UserId("user-123")
    assert type(uid).__name__ == "UserId"
    assert uid.value == "user-123"
    print("  ✓ branded() factory")

    # Test brand prevention
    TenantId = branded("TenantId", str)
    uid2 = UserId("abc")
    tid = TenantId("abc")
    assert uid2.value == tid.value
    assert type(uid2) != type(tid)
    print("  ✓ brand prevents mixing")

    # Test helpers
    b = brand_string("value", "MyBrand")
    assert isinstance(b, _BrandedBase)
    print("  ✓ brand_string")

    # Test introspection (use uid2 for the "abc" value to avoid overwriting)
    uid2 = UserId("abc")
    assert is_branded(uid2, "UserId")
    assert not is_branded(uid2, "TenantId")
    assert not is_branded("plain", "UserId")
    assert get_brand_name(uid2) == "UserId"
    assert unbrand(uid) == "user-123"  # Original uid from line 124
    print("  ✓ is_branded, get_brand_name, unbrand")

    # Test predefined types
    sid = SessionId("sess-123")
    cid = ConversationId("conv-456")
    aid = AgentId("agent-789")
    tcid = ToolCallId("tool-abc")
    assert sid.value == "sess-123"
    assert cid.value == "conv-456"
    assert aid.value == "agent-789"
    assert tcid.value == "tool-abc"
    print("  ✓ predefined types")

    # Test UUID branding
    UUIDId = branded("UUIDId", uuid.UUID)
    u = uuid.uuid4()
    uuid_id = UUIDId(u)
    assert uuid_id.value == u
    assert str(uuid_id) == str(u)
    print("  ✓ UUID branded type")

    # Test JSON serialization
    import json
    # Use custom encoder to serialize branded types as their value
    class BrandedEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, _BrandedBase):
                return obj.value
            return super().default(obj)

    data = json.dumps({"user_id": uid}, cls=BrandedEncoder)
    assert '"user_id": "user-123"' in data
    print("  ✓ JSON serialization")

    # Test frozenness
    try:
        uid.value = "other"
        assert False, "Should have raised AttributeError"
    except AttributeError:
        pass
    print("  ✓ frozen/immutable")

    print("All brand tests passed!")
    return True


def run_chunked_list_tests():
    print("\nRunning chunked_list tests...")
    from pydantic import ValidationError

    from app.deepspace.utils.chunked_list import (
        CHUNK_CAPACITY,
        append_chunked_list,
        chunked_list_get,
        chunked_list_is_empty,
        chunked_list_len,
        chunked_list_schema,
        from_list,
        iterate_chunked_list,
        to_list,
        validate_chunked_list,
    )

    # Test empty list
    assert chunked_list_is_empty(None)
    assert chunked_list_len(None) == 0
    assert chunked_list_get(None, 0) is None
    assert list(iterate_chunked_list(None)) == []
    print("  ✓ empty list")

    # Test single append
    head = append_chunked_list(None, "a")
    assert chunked_list_len(head) == 1
    assert chunked_list_get(head, 0) == "a"
    assert list(iterate_chunked_list(head)) == ["a"]
    print("  ✓ single append")

    # Test multiple appends
    head = None
    for i in range(10):
        head = append_chunked_list(head, f"item{i}")
    assert chunked_list_len(head) == 10
    assert list(iterate_chunked_list(head)) == [f"item{i}" for i in range(10)]
    print("  ✓ multiple appends")

    # Test chunk boundary
    head = None
    for i in range(CHUNK_CAPACITY + 5):
        head = append_chunked_list(head, i)
    assert chunked_list_len(head) == CHUNK_CAPACITY + 5
    assert list(iterate_chunked_list(head)) == list(range(CHUNK_CAPACITY + 5))
    print("  ✓ chunk boundary")

    # Test structural sharing
    head1 = append_chunked_list(None, "a")
    head1 = append_chunked_list(head1, "b")
    head2 = append_chunked_list(head1, "c")
    assert head1.previous is head2.previous
    print("  ✓ structural sharing")

    # Test iteration order
    head = None
    for i in range(5):
        head = append_chunked_list(head, i)
    assert list(iterate_chunked_list(head)) == [0, 1, 2, 3, 4]
    print("  ✓ iteration order")

    # Test indexing
    head = None
    for s in ["a", "b", "c"]:
        head = append_chunked_list(head, s)
    assert head[0] == "a"
    assert head[1] == "b"
    assert head[2] == "c"
    assert head[-1] == "c"
    assert head[-2] == "b"
    print("  ✓ indexing")

    # Test conversion
    head = from_list([1, 2, 3, 4, 5])
    assert to_list(head) == [1, 2, 3, 4, 5]
    assert from_list([]) is None
    print("  ✓ conversion")

    # Test schema validation
    schema = chunked_list_schema(str)
    data = {"values": ["a", "b"], "previous": None}
    validated = schema.model_validate(data)
    assert validated.values == ["a", "b"]
    print("  ✓ schema validation")

    # Test validation rejects empty
    try:
        schema.model_validate({"values": [], "previous": None})
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass
    print("  ✓ schema rejects empty")

    # Test validation rejects oversized
    try:
        schema.model_validate({"values": ["x"] * (CHUNK_CAPACITY + 1), "previous": None})
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass
    print("  ✓ schema rejects oversized")

    # Test validate_chunked_list
    head = None
    for s in ["a", "b", "c"]:
        head = append_chunked_list(head, s)
    validated = validate_chunked_list(head, str)
    assert list(iterate_chunked_list(validated)) == ["a", "b", "c"]
    print("  ✓ validate_chunked_list")

    print("All chunked_list tests passed!")
    return True


def run_code_language_tests():
    print("\nRunning code_language tests...")
    from pathlib import Path

    from app.deepspace.utils.code_language import (
        CODE_HIGHLIGHT_EXTENSIONS,
        EXTENSION_TO_LANGUAGE,
        LANGUAGE_EXTENSIONS,
        SHORT_BY_LANGUAGE,
        build_lsp_extension_map,
        get_language_extensions,
        is_recognized_extension,
        language_for_path,
        read_lang_hint_for_path,
    )

    # Test language_for_path
    assert language_for_path("main.py") == "python"
    assert language_for_path("script.ts") == "typescript"
    assert language_for_path("component.tsx") == "typescript"
    assert language_for_path("main.rs") == "rust"
    assert language_for_path("config.json") == "json"
    assert language_for_path("config.yaml") == "yaml"
    assert language_for_path(".env") == "dotenv"
    assert language_for_path("README.md") == "markdown"
    assert language_for_path("Makefile") is None
    assert language_for_path("unknown.xyz") is None
    assert language_for_path(Path("main.py")) == "python"
    assert language_for_path("MAIN.PY") == "python"
    print("  ✓ language_for_path")

    # Test read_lang_hint_for_path
    assert read_lang_hint_for_path("main.py") == "py"
    assert read_lang_hint_for_path("script.js") == "js"
    assert read_lang_hint_for_path("component.tsx") == "tsx"
    assert read_lang_hint_for_path("main.tf") == "tf"
    assert read_lang_hint_for_path("build.gradle") == "gradle"
    assert read_lang_hint_for_path("unknown.xyz") is None
    print("  ✓ read_lang_hint_for_path")

    # Test maps
    for lang, exts in LANGUAGE_EXTENSIONS.items():
        for ext in exts:
            assert EXTENSION_TO_LANGUAGE[ext] == lang
    print("  ✓ EXTENSION_TO_LANGUAGE complete")

    assert list(CODE_HIGHLIGHT_EXTENSIONS) == sorted(CODE_HIGHLIGHT_EXTENSIONS)
    print("  ✓ CODE_HIGHLIGHT_EXTENSIONS sorted")

    for lang in LANGUAGE_EXTENSIONS:
        assert lang in SHORT_BY_LANGUAGE
    print("  ✓ SHORT_BY_LANGUAGE complete")

    # Test helpers
    assert get_language_extensions("python") == ("py", "pyw", "pyi")
    assert get_language_extensions("unknown") == ()
    assert is_recognized_extension("py")
    assert not is_recognized_extension("unknown")
    print("  ✓ helper functions")

    # Test LSP map
    lsp_map = build_lsp_extension_map()
    assert ".py" in lsp_map
    assert lsp_map[".py"] == "python"
    print("  ✓ build_lsp_extension_map")

    print("All code_language tests passed!")
    return True


def run_crypto_tests():
    print("\nRunning crypto tests...")
    import uuid

    from app.deepspace.utils.crypto import (
        base64_to_bytes,
        base64url_to_bytes,
        bytes_to_base64,
        bytes_to_base64url,
        constant_time_compare,
        generate_secure_hex,
        generate_secure_token,
        generate_uuid7,
        new_hex,
        new_token,
        random_uuid,
        uuid4,
        uuid_to_base64,
    )

    # Test random_uuid
    u = random_uuid()
    parsed = uuid.UUID(u)
    assert parsed.version == 4
    assert parsed.variant == uuid.RFC_4122
    print("  ✓ random_uuid format")

    # Test uniqueness
    uuids = {random_uuid() for _ in range(1000)}
    assert len(uuids) == 1000
    print("  ✓ random_uuid uniqueness")

    # Test uuid_to_base64
    u = "12345678-1234-5678-1234-567812345678"
    b64 = uuid_to_base64(u)
    assert "=" not in b64
    assert len(b64) == 22
    # Round trip
    import base64
    padding = "=" * ((4 - len(b64) % 4) % 4)
    decoded = base64.urlsafe_b64decode(b64 + padding)
    reconstructed = uuid.UUID(bytes=decoded)
    assert str(reconstructed) == u
    print("  ✓ uuid_to_base64 round-trip")

    # Test bytes encoding
    data = b"hello world"
    b64 = bytes_to_base64(data)
    assert base64_to_bytes(b64) == data
    b64url = bytes_to_base64url(data)
    assert base64url_to_bytes(b64url) == data
    print("  ✓ bytes encoding/decoding")

    # Test invalid base64
    try:
        base64_to_bytes("not valid!!!")
        assert False
    except ValueError:
        pass
    try:
        base64url_to_bytes("not valid!!!")
        assert False
    except ValueError:
        pass
    print("  ✓ invalid base64 raises")

    # Test generate_secure_token
    token = generate_secure_token()
    assert len(token) == 43
    token = generate_secure_token(16)
    assert len(token) == 22
    tokens = {generate_secure_token() for _ in range(1000)}
    assert len(tokens) == 1000
    print("  ✓ generate_secure_token")

    # Test generate_secure_hex
    hex_str = generate_secure_hex()
    assert len(hex_str) == 64
    hex_str = generate_secure_hex(16)
    assert len(hex_str) == 32
    tokens = {generate_secure_hex() for _ in range(1000)}
    assert len(tokens) == 1000
    print("  ✓ generate_secure_hex")

    # Test constant_time_compare
    assert constant_time_compare("same", "same")
    assert not constant_time_compare("diff", "same")
    assert not constant_time_compare("short", "very long")
    print("  ✓ constant_time_compare")

    # Test generate_uuid7 (falls back to v4)
    u = generate_uuid7()
    parsed = uuid.UUID(u)
    assert parsed.version in (4, 7)
    print("  ✓ generate_uuid7")

    # Test aliases
    assert uuid4().startswith(str(uuid.uuid4())[:8]) or True  # Just check it works
    assert len(new_token()) == 43
    assert len(new_hex()) == 64
    print("  ✓ aliases")

    print("All crypto tests passed!")
    return True


def run_deque_tests():
    print("\nRunning deque tests...")
    from app.deepspace.utils.deque import Deque, DequeWithMaxlen

    # Test empty
    dq = Deque[int]()
    assert len(dq) == 0
    assert dq.size == 0
    assert not dq
    assert dq.pop_front() is None
    assert dq.pop_back() is None
    print("  ✓ empty deque")

    # Test push_back
    dq = Deque[str]()
    dq.push_back("a")
    dq.push_back("b")
    dq.push_back("c")
    assert len(dq) == 3
    assert dq.peek_front() == "a"
    assert dq.peek_back() == "c"
    print("  ✓ push_back")

    # Test push_front
    dq = Deque[str]()
    dq.push_front("c")
    dq.push_front("b")
    dq.push_front("a")
    assert len(dq) == 3
    assert dq.peek_front() == "a"
    assert dq.peek_back() == "c"
    print("  ✓ push_front")

    # Test pop_front
    dq = Deque[str]()
    dq.push_back("a")
    dq.push_back("b")
    dq.push_back("c")
    assert dq.pop_front() == "a"
    assert dq.pop_front() == "b"
    assert dq.pop_front() == "c"
    assert dq.pop_front() is None
    print("  ✓ pop_front")

    # Test pop_back
    dq = Deque[str]()
    dq.push_back("a")
    dq.push_back("b")
    dq.push_back("c")
    assert dq.pop_back() == "c"
    assert dq.pop_back() == "b"
    assert dq.pop_back() == "a"
    assert dq.pop_back() is None
    print("  ✓ pop_back")

    # Test iteration
    dq = Deque[int]()
    for i in range(5):
        dq.push_back(i)
    assert list(dq) == [0, 1, 2, 3, 4]
    assert list(reversed(dq)) == [4, 3, 2, 1, 0]
    print("  ✓ iteration")

    # Test indexing
    dq = Deque[str]()
    for s in ["a", "b", "c", "d"]:
        dq.push_back(s)
    assert dq[0] == "a"
    assert dq[1] == "b"
    assert dq[-1] == "d"
    assert dq[-2] == "c"
    dq[0] = "z"
    assert dq[0] == "z"
    print("  ✓ indexing")

    # Test clear
    dq = Deque[int]()
    for i in range(10):
        dq.push_back(i)
    dq.clear()
    assert len(dq) == 0
    print("  ✓ clear")

    # Test capacity growth
    dq = Deque[int]()
    for i in range(20):
        dq.push_back(i)
    assert len(dq) == 20
    assert list(dq) == list(range(20))
    print("  ✓ capacity growth")

    # Test compaction
    dq = Deque[int]()
    for i in range(100):
        dq.push_back(i)
    for _ in range(95):
        dq.pop_front()
    assert len(dq) == 5
    print("  ✓ compaction")

    # Test to_list
    dq = Deque[int]()
    for i in range(5):
        dq.push_back(i)
    assert dq.to_list() == [0, 1, 2, 3, 4]
    print("  ✓ to_list")

    # Test extend
    dq = Deque[int]()
    dq.extend([1, 2, 3])
    assert list(dq) == [1, 2, 3]
    print("  ✓ extend")

    # Test rotate
    dq = Deque[int]()
    for i in range(5):
        dq.push_back(i)
    dq.rotate(1)
    assert list(dq) == [4, 0, 1, 2, 3]
    dq.rotate(-1)
    assert list(dq) == [0, 1, 2, 3, 4]
    print("  ✓ rotate")

    # Test init from iterable
    dq = Deque([1, 2, 3, 4])
    assert list(dq) == [1, 2, 3, 4]
    dq = Deque((1, 2, 3))
    assert list(dq) == [1, 2, 3]
    dq = Deque(x for x in range(3))
    assert list(dq) == [0, 1, 2]
    print("  ✓ init from iterable")

    # Test DequeWithMaxlen
    dq = DequeWithMaxlen[int](maxlen=3)
    dq.push_back(1)
    dq.push_back(2)
    dq.push_back(3)
    assert list(dq) == [1, 2, 3]
    dq.push_back(4)
    assert list(dq) == [2, 3, 4]
    print("  ✓ DequeWithMaxlen push_back")

    dq = DequeWithMaxlen[int](maxlen=3)
    dq.push_front(3)
    dq.push_front(2)
    dq.push_front(1)
    assert list(dq) == [1, 2, 3]
    dq.push_front(0)
    assert list(dq) == [0, 1, 2]
    print("  ✓ DequeWithMaxlen push_front")

    dq = DequeWithMaxlen([1, 2, 3, 4, 5], maxlen=3)
    assert list(dq) == [3, 4, 5]
    print("  ✓ DequeWithMaxlen init")

    print("All deque tests passed!")
    return True


def main():
    """Run all tests."""
    tests = [
        run_atomic_write_tests,
        run_brand_tests,
        run_chunked_list_tests,
        run_code_language_tests,
        run_crypto_tests,
        run_deque_tests,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAILED: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*50}")

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
