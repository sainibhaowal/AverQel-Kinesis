"""Encryption at rest for the DeepSpace typed session log.

A typed event's payload holds tool arguments, tool results, and the decisions
a hook made.  The payload column in ``deepspace_session_events`` stores those
values as one AES-GCM envelope, so a database dump does not reveal them.

The column type in :mod:`app.deepspace.models.session_event` calls
:func:`get_session_log_cipher` on every bind and every load.  The cipher is
cached by its key source, so a row costs one AES call rather than a settings
read and a keyring parse.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.averqel_home import get_averqel_home
from app.core.config import Settings, get_settings

#: The envelope field that marks a value as an encrypted session log payload.
ENVELOPE_MARKER = "__encrypted__"

#: The envelope version this module writes and reads.
ENVELOPE_VERSION = 1

#: The key id the local key file uses.
FILE_KEY_ID = "local-file"

#: The AES-GCM nonce length in bytes.
_NONCE_BYTES = 12

#: The local key file's length in bytes.
_KEY_BYTES = 32

#: The key lengths AES-GCM accepts.
_KEY_LENGTHS = frozenset({16, 24, 32})

#: The file that holds the local key when no keyring is configured.
_KEY_FILE_NAME = "deepspace-session-log.key"


class SessionLogCryptoError(RuntimeError):
    """Raised when a session log payload cannot be encrypted or decrypted."""


def session_log_key_path() -> Path:
    """Return the local key file used when no keyring is configured.

    Returns:
        The path ``<averqel home>/keys/deepspace-session-log.key``.
    """
    return get_averqel_home() / "keys" / _KEY_FILE_NAME


class SessionLogCipher:
    """Encrypt and decrypt the payload of one typed session event.

    The key comes from ``deepspace_session_log_keyring_json`` and
    ``deepspace_session_log_active_kid`` when both are set.  Otherwise the
    cipher reads, or creates on first use, a 32-byte key file under the
    AverQel home directory.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        keyring_json = self.settings.deepspace_session_log_keyring_json
        active_kid = self.settings.deepspace_session_log_active_kid

        if bool(keyring_json) != bool(active_kid):
            raise SessionLogCryptoError(
                "deepspace_session_log_active_kid and "
                "deepspace_session_log_keyring_json must be set together"
            )

        if keyring_json and active_kid:
            self._keyring = _parse_keyring(keyring_json)
            key = self._keyring.get(active_kid)
            if key is None:
                raise SessionLogCryptoError("active session log key id is not in the keyring")
            self._active_kid = active_kid
        else:
            self._keyring = {FILE_KEY_ID: _load_or_create_key(session_log_key_path())}
            self._active_kid = FILE_KEY_ID

        self._cipher = AESGCM(self._keyring[self._active_kid])

    def encrypt(self, plaintext: bytes) -> dict[str, object]:
        """Encrypt one payload.

        Args:
            plaintext: The payload's serialized bytes.

        Returns:
            The envelope the payload column stores.
        """
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, plaintext, None)
        return {
            ENVELOPE_MARKER: ENVELOPE_VERSION,
            "kid": self._active_kid,
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
        }

    def decrypt(self, envelope: Mapping[str, Any]) -> bytes:
        """Decrypt one stored envelope.

        Args:
            envelope: The value read from the payload column.

        Returns:
            The payload's serialized bytes.

        Raises:
            SessionLogCryptoError: The envelope is malformed, names a key the
                cipher does not hold, or fails authentication.
        """
        if envelope.get(ENVELOPE_MARKER) != ENVELOPE_VERSION:
            raise SessionLogCryptoError("session log payload is not an encrypted envelope")

        kid = envelope.get("kid")
        if not isinstance(kid, str) or not kid:
            raise SessionLogCryptoError("session log envelope key id must be a non-empty string")

        nonce = _decode_field(envelope.get("nonce"), "nonce")
        if len(nonce) != _NONCE_BYTES:
            raise SessionLogCryptoError("session log envelope nonce must be 12 bytes")
        ciphertext = _decode_field(envelope.get("ciphertext"), "ciphertext")

        key = self._keyring.get(kid)
        if key is None:
            raise SessionLogCryptoError(f"session log key {kid!r} is not available")
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise SessionLogCryptoError("session log payload failed authentication") from exc


_CIPHER_CACHE: dict[tuple[str | None, str | None, str | None], SessionLogCipher] = {}
_CIPHER_CACHE_LOCK = threading.Lock()


def get_session_log_cipher(settings: Settings | None = None) -> SessionLogCipher:
    """Return the cached cipher for one key configuration.

    Args:
        settings: The settings to read.  The process settings are used when
            this is omitted.

    Returns:
        The cipher for the configured keyring or key file.
    """
    resolved = settings or get_settings()
    keyring_json = resolved.deepspace_session_log_keyring_json
    active_kid = resolved.deepspace_session_log_active_kid
    key_path = None if keyring_json else str(session_log_key_path())
    cache_key = (keyring_json, active_kid, key_path)

    with _CIPHER_CACHE_LOCK:
        cipher = _CIPHER_CACHE.get(cache_key)
        if cipher is None:
            cipher = SessionLogCipher(resolved)
            _CIPHER_CACHE[cache_key] = cipher
        return cipher


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: str, description: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise SessionLogCryptoError(f"session log {description} is not valid base64url") from exc


def _decode_field(value: Any, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise SessionLogCryptoError(
            f"session log envelope field {field!r} must be a non-empty base64url string"
        )
    return _decode(value, f"envelope field {field!r}")


def _parse_keyring(raw: str) -> dict[str, bytes]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SessionLogCryptoError("session log keyring is not valid JSON") from exc

    if not isinstance(parsed, dict) or not parsed:
        raise SessionLogCryptoError("session log keyring must be a non-empty JSON object")

    keyring: dict[str, bytes] = {}
    for kid, encoded_key in parsed.items():
        if not isinstance(kid, str) or not kid.strip():
            raise SessionLogCryptoError("session log key ids must be non-empty strings")
        if not isinstance(encoded_key, str) or not encoded_key.strip():
            raise SessionLogCryptoError("session log keys must be non-empty base64 strings")
        key = _decode(encoded_key, f"key for kid={kid!r}")
        if len(key) not in _KEY_LENGTHS:
            raise SessionLogCryptoError(
                "session log keys must decode to 16, 24, or 32 bytes for AES-GCM"
            )
        keyring[kid.strip()] = key
    return keyring


def _load_or_create_key(path: Path) -> bytes:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(os.urandom(_KEY_BYTES))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            # A hard link publishes the complete file or fails when another
            # process published a key first, so a reader never sees a partial
            # key and a key is never overwritten.
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError as exc:
            raise SessionLogCryptoError(f"session log key file {path} cannot be created") from exc
    finally:
        temporary.unlink(missing_ok=True)

    return _read_key(path)


def _read_key(path: Path) -> bytes:
    try:
        key = path.read_bytes()
    except OSError as exc:
        raise SessionLogCryptoError(f"session log key file {path} cannot be read") from exc
    if len(key) != _KEY_BYTES:
        raise SessionLogCryptoError(f"session log key file {path} must hold {_KEY_BYTES} bytes")
    return key
