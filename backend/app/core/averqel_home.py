"""Unified local home directory for AverQel desktop data.

All runtime artefacts (database, document storage, cache, encrypted secrets,
MCP metadata) are rooted under a single per-user ``.averqel`` directory so
the application behaves like other system tools (e.g., ``.codex``, ``.ssh``)
and never scatters data across the working directory or system-wide paths.

Resolution order (first wins):
1. ``AVERQEL_HOME`` environment variable — set by Electron before spawning the
   backend so the desktop app can pin the directory next to the binary or in
   the platform user-data folder.
2. ``~/.averqel`` — the default per-user directory on every platform.

Sub-directory layout inside ``AVERQEL_HOME``:
    db/          SQLite databases (``knowledge.db``, ``averqel.db``)
    storage/     User-uploaded file blobs
    cache/       Transient artefacts (embeddings cache, search index, tmp chunks)
    secrets/     Encrypted credential store (AES-GCM, never plaintext)
    mcp/         MCP server metadata and configuration cache
    logs/        Application log files
    exports/     User-initiated export archives
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Sub-directory names — authoritative list for the whole backend.
# ---------------------------------------------------------------------------

_SUBDIRS: tuple[str, ...] = (
    "db",
    "storage",
    "cache",
    "secrets",
    "mcp",
    "logs",
    "exports",
    "models",
)


@lru_cache(maxsize=1)
def get_averqel_home() -> Path:
    """Return the resolved and initialised ``.averqel`` home directory.

    Creates the directory tree on first call.  The result is cached for the
    process lifetime so repeated calls are free.
    """
    raw = os.environ.get("AVERQEL_HOME", "").strip()
    if raw:
        home = Path(raw).expanduser().resolve()
    else:
        home = Path.home() / ".averqel"

    home.mkdir(parents=True, exist_ok=True)
    for sub in _SUBDIRS:
        (home / sub).mkdir(exist_ok=True)

    return home


def averqel_db_path(name: str = "knowledge.db") -> Path:
    """Return an absolute path inside the ``db/`` sub-directory.

    Args:
        name: Database filename (e.g., ``"knowledge.db"``).

    Returns:
        Absolute :class:`~pathlib.Path` inside ``<averqel_home>/db/``.
    """
    return get_averqel_home() / "db" / name


def averqel_storage_path() -> Path:
    """Return the user file-blob storage directory.

    Returns:
        Absolute :class:`~pathlib.Path` for ``<averqel_home>/storage/``.
    """
    return get_averqel_home() / "storage"


def averqel_cache_path() -> Path:
    """Return the transient cache directory.

    Returns:
        Absolute :class:`~pathlib.Path` for ``<averqel_home>/cache/``.
    """
    return get_averqel_home() / "cache"


def averqel_secrets_path() -> Path:
    """Return the encrypted secrets directory.

    Returns:
        Absolute :class:`~pathlib.Path` for ``<averqel_home>/secrets/``.
    """
    return get_averqel_home() / "secrets"


def ensure_local_provider_secret_keyring() -> tuple[str, str]:
    """Return a stable local provider-secret keyring, creating it if needed.

    Local desktop mode needs the same encryption key across backend restarts so
    provider credentials can be rotated and decrypted. Explicit deployment
    keyring environment variables remain the authority for non-local modes.

    Returns:
        The active key id and its compact JSON keyring representation.
    """
    keyring_path = averqel_secrets_path() / "provider-secret-keyring.json"
    active_kid = "local"
    if keyring_path.exists():
        try:
            payload = json.loads(keyring_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("The local provider secret keyring cannot be read") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get(active_kid), str):
            raise RuntimeError("The local provider secret keyring is invalid")
        return active_kid, json.dumps(payload, separators=(",", ":"))

    payload = {active_kid: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")}
    encoded = json.dumps(payload, separators=(",", ":"))
    try:
        descriptor = os.open(
            keyring_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return ensure_local_provider_secret_keyring()
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.write("\n")
    except Exception:
        keyring_path.unlink(missing_ok=True)
        raise
    keyring_path.chmod(0o600)
    return active_kid, encoded


def averqel_mcp_path() -> Path:
    """Return the MCP metadata directory.

    Returns:
        Absolute :class:`~pathlib.Path` for ``<averqel_home>/mcp/``.
    """
    return get_averqel_home() / "mcp"


def averqel_exports_path() -> Path:
    """Return the user exports directory.

    Returns:
        Absolute :class:`~pathlib.Path` for ``<averqel_home>/exports/``.
    """
    return get_averqel_home() / "exports"


def averqel_logs_path() -> Path:
    """Return the application logs directory.

    Returns:
        Absolute :class:`~pathlib.Path` for ``<averqel_home>/logs/``.
    """
    return get_averqel_home() / "logs"


def averqel_models_path() -> Path:
    """Return the local embedding models storage directory.

    Returns:
        Absolute :class:`~pathlib.Path` for ``<averqel_home>/models/embeddings/``.
    """
    path = get_averqel_home() / "models" / "embeddings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def averqel_rerankers_path() -> Path:
    """Return the local reranker models storage directory.

    Returns:
        Absolute :class:`~pathlib.Path` for ``<averqel_home>/models/rerankers/``.
    """
    path = get_averqel_home() / "models" / "rerankers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sqlite_url(name: str = "knowledge.db") -> str:
    """Return a SQLite connection URL that points inside ``.averqel/db/``.

    Args:
        name: Database filename.

    Returns:
        SQLAlchemy-compatible ``sqlite:///`` URL string using an absolute path.
    """
    return f"sqlite:///{averqel_db_path(name)}"
