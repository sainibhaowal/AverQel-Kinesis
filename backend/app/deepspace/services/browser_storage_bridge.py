"""Encrypted per-conversation bridge for sidebar-to-agent browser login state.

Cookies and ``localStorage`` exported from either the Python ``BrowserManager``
(web/API sidebar runtime) or the Electron desktop ``<webview>`` are encrypted
with the DeepSpace session-log AES-GCM cipher so:

- Credentials are never written to disk in plaintext.
- Separate Celery workers can read the latest login state for the same
  ``(tenant_id, user_id, conversation_id)``.
- Strict tenant, user, and conversation isolation is enforced by key derivation
  and explicit envelope metadata verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from app.core.averqel_home import get_averqel_home
from app.deepspace.services.session_log_crypto import get_session_log_cipher

_CACHE_LOCK = threading.Lock()
_IN_MEMORY_STATES: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, Any]] = {}


def _bridge_dir() -> Path:
    try:
        base = get_averqel_home() / "runtime" / "browser-sync"
    except Exception:
        base = Path(tempfile.gettempdir()) / "averqel-browser-sync"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def _state_file(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> Path:
    digest = hashlib.sha256(f"{tenant_id}:{user_id}:{conversation_id}".encode()).hexdigest()
    return _bridge_dir() / f"{digest}.enc"


def normalize_storage_state(raw: Any) -> dict[str, Any]:
    """Normalize a Playwright/CDP/Electron storage state payload."""
    if not isinstance(raw, dict):
        return {"cookies": [], "origins": [], "active_url": None}
    from app.deepspace.services.browser_runtime import _normalize_cdp_cookies

    cookies = _normalize_cdp_cookies(raw.get("cookies") or [])
    origins: list[dict[str, Any]] = []
    raw_origins = raw.get("origins") or []
    if isinstance(raw_origins, list):
        for entry in raw_origins:
            if not isinstance(entry, dict):
                continue
            origin = entry.get("origin")
            if not isinstance(origin, str) or (
                not origin.startswith("http://") and not origin.startswith("https://")
            ):
                continue
            items_raw = entry.get("localStorage") or []
            if not isinstance(items_raw, list):
                continue
            items = [
                {"name": str(item["name"]), "value": str(item["value"])}
                for item in items_raw
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
            if items:
                origins.append({"origin": origin, "localStorage": items})
    raw_active_url = raw.get("active_url")
    active_url = (
        raw_active_url.strip()
        if isinstance(raw_active_url, str)
        and (
            raw_active_url.strip().startswith("http://")
            or raw_active_url.strip().startswith("https://")
        )
        else None
    )
    raw_text_snapshot = raw.get("text_snapshot")
    text_snapshot = (
        raw_text_snapshot[:64_000]
        if isinstance(raw_text_snapshot, str) and raw_text_snapshot.strip()
        else None
    )
    raw_screenshot = raw.get("screenshot_base64")
    screenshot_base64 = (
        raw_screenshot
        if isinstance(raw_screenshot, str) and 0 < len(raw_screenshot) <= 4_500_000
        else None
    )
    return {
        "cookies": cookies,
        "origins": origins,
        "active_url": active_url,
        "text_snapshot": text_snapshot,
        "screenshot_base64": screenshot_base64,
    }


def save_conversation_storage_state(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    storage_state: dict[str, Any],
) -> dict[str, Any]:
    """Encrypt and store one conversation's sidebar browser login state."""
    normalized = normalize_storage_state(storage_state)
    key = (tenant_id, user_id, conversation_id)
    with _CACHE_LOCK:
        _IN_MEMORY_STATES[key] = normalized

    envelope_payload = {
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "conversation_id": str(conversation_id),
        "storage_state": normalized,
    }
    plaintext = json.dumps(envelope_payload, separators=(",", ":")).encode("utf-8")
    encrypted = get_session_log_cipher().encrypt(plaintext)
    target = _state_file(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    tmp_path = target.with_suffix(".enc.tmp")
    tmp_path.write_text(json.dumps(encrypted, separators=(",", ":")), encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, target)
    return normalized


def load_conversation_storage_state(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Load and decrypt one conversation's sidebar browser login state, if any."""
    key = (tenant_id, user_id, conversation_id)
    with _CACHE_LOCK:
        cached = _IN_MEMORY_STATES.get(key)
        if cached is not None:
            return cached

    target = _state_file(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if not target.is_file():
        return None
    try:
        raw_envelope = json.loads(target.read_text(encoding="utf-8"))
        plaintext = get_session_log_cipher().decrypt(raw_envelope)
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("tenant_id") != str(tenant_id)
        or payload.get("user_id") != str(user_id)
        or payload.get("conversation_id") != str(conversation_id)
    ):
        return None
    state = normalize_storage_state(payload.get("storage_state"))
    with _CACHE_LOCK:
        _IN_MEMORY_STATES[key] = state
    return state


def delete_conversation_storage_state(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> None:
    """Remove any cached/persisted sidebar browser login state for one conversation."""
    key = (tenant_id, user_id, conversation_id)
    with _CACHE_LOCK:
        _IN_MEMORY_STATES.pop(key, None)
    target = _state_file(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass
