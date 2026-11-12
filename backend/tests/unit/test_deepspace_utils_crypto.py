"""Tests for crypto utilities."""

from __future__ import annotations

import uuid

import pytest

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
    new_id,
    new_token,
    random_uuid,
    uuid4,
    uuid_to_base64,
)


class TestRandomUUID:
    """Tests for random_uuid (RFC 9562 UUIDv4)."""

    def test_returns_string(self) -> None:
        u = random_uuid()
        assert isinstance(u, str)

    def test_format_matches_uuid(self) -> None:
        u = random_uuid()
        # Should parse as valid UUID
        parsed = uuid.UUID(u)
        assert str(parsed) == u

    def test_version_4(self) -> None:
        u = random_uuid()
        parsed = uuid.UUID(u)
        assert parsed.version == 4

    def test_variant_rfc4122(self) -> None:
        u = random_uuid()
        parsed = uuid.UUID(u)
        # Variant should be RFC 4122 (10xx)
        assert parsed.variant == uuid.RFC_4122

    def test_uniqueness(self) -> None:
        uuids = {random_uuid() for _ in range(1000)}
        assert len(uuids) == 1000  # No collisions in 1000

    def test_format_xxxxxxxx_xxxx_4xxx_yxxx_xxxxxxxxxxxx(self) -> None:
        u = random_uuid()
        parts = u.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4
        assert parts[2][0] == "4"  # Version 4
        assert len(parts[3]) == 4
        # Variant bits: 10xx -> 8, 9, a, b
        assert parts[3][0] in "89ab"
        assert len(parts[4]) == 12


class TestUUIDToBase64:
    """Tests for uuid_to_base64."""

    def test_converts_to_base64url(self) -> None:
        u = "12345678-1234-5678-1234-567812345678"
        b64 = uuid_to_base64(u)
        assert isinstance(b64, str)
        assert "=" not in b64  # No padding
        assert len(b64) == 22  # 16 bytes -> 22 base64url chars

    def test_round_trip(self) -> None:
        u = "12345678-1234-5678-1234-567812345678"
        b64 = uuid_to_base64(u)
        # Decode back
        import base64

        padding = "=" * ((4 - len(b64) % 4) % 4)
        decoded = base64.urlsafe_b64decode(b64 + padding)
        assert len(decoded) == 16
        reconstructed = uuid.UUID(bytes=decoded)
        assert str(reconstructed) == u

    def test_random_uuid_round_trip(self) -> None:
        u = random_uuid()
        b64 = uuid_to_base64(u)
        import base64

        padding = "=" * ((4 - len(b64) % 4) % 4)
        decoded = base64.urlsafe_b64decode(b64 + padding)
        reconstructed = uuid.UUID(bytes=decoded)
        assert str(reconstructed) == u


class TestBytesToBase64:
    """Tests for bytes_to_base64 and bytes_to_base64url."""

    def test_bytes_to_base64(self) -> None:
        data = b"hello world"
        b64 = bytes_to_base64(data)
        import base64

        assert b64 == base64.b64encode(data).decode("ascii")

    def test_bytes_to_base64url(self) -> None:
        data = b"hello world"
        b64url = bytes_to_base64url(data)
        import base64

        expected = base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
        assert b64url == expected

    def test_empty_bytes(self) -> None:
        assert bytes_to_base64(b"") == ""
        assert bytes_to_base64url(b"") == ""

    def test_unicode_bytes(self) -> None:
        data = "hello 世界".encode()
        b64 = bytes_to_base64(data)
        assert base64_to_bytes(b64) == data


class TestBase64ToBytes:
    """Tests for base64_to_bytes and base64url_to_bytes."""

    def test_base64_to_bytes(self) -> None:
        data = b"hello world"
        b64 = bytes_to_base64(data)
        decoded = base64_to_bytes(b64)
        assert decoded == data

    def test_base64url_to_bytes(self) -> None:
        data = b"hello world"
        b64url = bytes_to_base64url(data)
        decoded = base64url_to_bytes(b64url)
        assert decoded == data

    def test_base64url_to_bytes_with_padding(self) -> None:
        # Should handle both padded and unpadded
        data = b"hello"
        b64url = bytes_to_base64url(data)
        # Add padding manually
        padded = b64url + "="
        decoded = base64url_to_bytes(padded)
        assert decoded == data

    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(ValueError):
            base64_to_bytes("not valid base64!!!")

    def test_invalid_base64url_raises(self) -> None:
        with pytest.raises(ValueError):
            base64url_to_bytes("not valid base64url!!!")


class TestGenerateSecureToken:
    """Tests for generate_secure_token."""

    def test_default_length(self) -> None:
        token = generate_secure_token()
        # 32 bytes -> 43 base64url chars (no padding)
        assert len(token) == 43

    def test_custom_length(self) -> None:
        token = generate_secure_token(16)
        # 16 bytes -> 22 base64url chars
        assert len(token) == 22

    def test_uniqueness(self) -> None:
        tokens = {generate_secure_token() for _ in range(1000)}
        assert len(tokens) == 1000

    def test_url_safe_chars(self) -> None:
        token = generate_secure_token()
        # Should only contain URL-safe base64 chars
        assert all(
            c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in token
        )
        assert "=" not in token  # No padding


class TestGenerateSecureHex:
    """Tests for generate_secure_hex."""

    def test_default_length(self) -> None:
        hex_str = generate_secure_hex()
        assert len(hex_str) == 64  # 32 bytes * 2

    def test_custom_length(self) -> None:
        hex_str = generate_secure_hex(16)
        assert len(hex_str) == 32  # 16 bytes * 2

    def test_hex_chars_only(self) -> None:
        hex_str = generate_secure_hex()
        assert all(c in "0123456789abcdef" for c in hex_str)

    def test_uniqueness(self) -> None:
        tokens = {generate_secure_hex() for _ in range(1000)}
        assert len(tokens) == 1000


class TestConstantTimeCompare:
    """Tests for constant_time_compare."""

    def test_equal_strings(self) -> None:
        assert constant_time_compare("same", "same")

    def test_different_strings(self) -> None:
        assert not constant_time_compare("diff", "same")

    def test_different_lengths(self) -> None:
        assert not constant_time_compare("short", "very long string")

    def test_empty_strings(self) -> None:
        assert constant_time_compare("", "")
        assert not constant_time_compare("", "a")


class TestGenerateUUID7:
    """Tests for generate_uuid7 (falls back to v4)."""

    def test_returns_uuid_string(self) -> None:
        u = generate_uuid7()
        assert isinstance(u, str)
        parsed = uuid.UUID(u)
        assert str(parsed) == u

    def test_version_4_or_7(self) -> None:
        u = generate_uuid7()
        parsed = uuid.UUID(u)
        # Should be either v4 or v7
        assert parsed.version in (4, 7)


class TestAliases:
    """Tests for convenience aliases."""

    def test_uuid4_alias(self) -> None:
        u = uuid4()
        assert isinstance(u, str)
        parsed = uuid.UUID(u)
        assert parsed.version == 4

    def test_new_id_alias(self) -> None:
        u = new_id()
        assert isinstance(u, str)
        parsed = uuid.UUID(u)
        assert parsed.version == 4

    def test_new_token_alias(self) -> None:
        t = new_token()
        assert isinstance(t, str)
        assert len(t) == 43

    def test_new_hex_alias(self) -> None:
        h = new_hex()
        assert isinstance(h, str)
        assert len(h) == 64


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
