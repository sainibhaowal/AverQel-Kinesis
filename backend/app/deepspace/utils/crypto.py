"""Browser-safe crypto utilities for DeepSpace.

Provides UUID minting and byte encoding helpers that work in every Python
context. Uses `secrets` module (available everywhere) for secure randomness,
matching the semantics of @deepseek-ai/dsh-util-crypto.

Usage:
    from app.deepspace.utils.crypto import random_uuid, bytes_to_base64, uuid_to_base64

    uid = random_uuid()           # RFC 9562 UUIDv4 string
    b64 = bytes_to_base64(b"data") # base64 encoding
    b64 = uuid_to_base64(uid)     # UUID as base64url
"""

from __future__ import annotations

import base64
import secrets
import uuid

__all__ = [
    "random_uuid",
    "uuid_to_base64",
    "bytes_to_base64",
    "base64_to_bytes",
    "generate_secure_token",
    "generate_secure_hex",
    "constant_time_compare",
    "UUIDv4",
]


# RFC 9562 UUID string format
UUIDv4 = str  # Type alias for UUID v4 strings


def random_uuid() -> UUIDv4:
    """Generate a random RFC 9562 UUID v4 string.

    Uses `secrets.token_bytes(16)` for cryptographically secure randomness.
    Sets version (4) in byte 6 and variant (10) in byte 8 per RFC 9562 §5.4.

    Returns:
        UUID string in format: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
    """
    # Generate 16 random bytes
    random_bytes = secrets.token_bytes(16)

    # Convert to mutable list
    bytes_list = bytearray(random_bytes)

    # RFC 9562 §5.4: Set version (4) in high nibble of byte 6
    bytes_list[6] = (bytes_list[6] & 0x0F) | 0x40

    # Set variant (10) in high bits of byte 8
    bytes_list[8] = (bytes_list[8] & 0x3F) | 0x80

    # Format as UUID string (standard format with hyphens)
    return str(uuid.UUID(bytes=bytes(bytes_list)))


def uuid_to_base64(uuid_str: str) -> str:
    """Convert a UUID string to base64url encoding (no padding).

    Args:
        uuid_str: UUID in standard format (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)

    Returns:
        Base64url-encoded string (22 chars, no padding).
    """
    # Parse UUID to bytes
    u = uuid.UUID(uuid_str)
    # Encode as base64url without padding
    return base64.urlsafe_b64encode(u.bytes).rstrip(b"=").decode("ascii")


def bytes_to_base64(data: bytes) -> str:
    """Encode bytes as standard base64 (with padding).

    Args:
        data: Bytes to encode.

    Returns:
        Base64-encoded string.
    """
    return base64.b64encode(data).decode("ascii")


def bytes_to_base64url(data: bytes) -> str:
    """Encode bytes as base64url (no padding).

    Args:
        data: Bytes to encode.

    Returns:
        Base64url-encoded string.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def base64_to_bytes(b64: str) -> bytes:
    """Decode standard base64 string to bytes.

    Args:
        b64: Base64-encoded string.

    Returns:
        Decoded bytes.

    Raises:
        ValueError: If input is not valid base64.
    """
    try:
        return base64.b64decode(b64, validate=True)
    except Exception as e:
        raise ValueError(f"Invalid base64: {e}") from e


def base64url_to_bytes(b64url: str) -> bytes:
    """Decode base64url string to bytes.

    Args:
        b64url: Base64url-encoded string (padding optional).

    Returns:
        Decoded bytes.

    Raises:
        ValueError: If input is not valid base64url.
    """
    # Validate characters first
    import re

    if not re.fullmatch(r"[A-Za-z0-9\-_]*=?=?", b64url):
        raise ValueError("Invalid base64url: contains invalid characters")

    # Add padding if needed
    padding = "=" * ((4 - len(b64url) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(b64url + padding)
    except Exception as e:
        raise ValueError(f"Invalid base64url: {e}") from e


def generate_secure_token(length: int = 32) -> str:
    """Generate a secure random token as base64url.

    Args:
        length: Number of random bytes (default 32 = 256 bits).

    Returns:
        Base64url-encoded token.
    """
    return bytes_to_base64url(secrets.token_bytes(length))


def generate_secure_hex(length: int = 32) -> str:
    """Generate a secure random token as hex string.

    Args:
        length: Number of random bytes.

    Returns:
        Hex-encoded string (2 * length chars).
    """
    return secrets.token_hex(length)


def constant_time_compare(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks.

    Args:
        a: First string.
        b: Second string.

    Returns:
        True if strings are equal, False otherwise.
    """
    return secrets.compare_digest(a, b)


def generate_uuid7() -> UUIDv4:
    """Generate a UUID v7 (timestamp-ordered) if available.

    Falls back to UUID v4 if uuid6 is not installed or fails.

    Returns:
        UUID string.
    """
    try:
        from uuid6 import uuid7

        return str(uuid7())
    except ImportError:
        # Fallback to v4
        return random_uuid()
    except Exception:
        # Any other failure, fall back
        return random_uuid()


# Convenience aliases matching common patterns
new_id = random_uuid
new_token = generate_secure_token
new_hex = generate_secure_hex


# For compatibility with existing code using uuid.uuid4()
def uuid4() -> UUIDv4:
    """Alias for random_uuid() for drop-in compatibility with uuid.uuid4()."""
    return random_uuid()


__all__ = [
    "random_uuid",
    "uuid_to_base64",
    "bytes_to_base64",
    "bytes_to_base64url",
    "base64_to_bytes",
    "base64url_to_bytes",
    "generate_secure_token",
    "generate_secure_hex",
    "constant_time_compare",
    "generate_uuid7",
    "uuid4",
    "new_id",
    "new_token",
    "new_hex",
    "UUIDv4",
]
