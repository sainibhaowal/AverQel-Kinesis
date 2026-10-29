"""Tests for encrypted DeepSpace session log payloads.

The checks write through the real ``SessionEventStore`` and read the raw
column back, so they prove both that the ORM keeps returning plaintext and
that the database holds an envelope.  They run on a local SQLite database and
never touch the isolated services.
"""

from __future__ import annotations

import base64
import json
import stat
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session, sessionmaker

from app.core.averqel_home import get_averqel_home
from app.core.config import Settings, get_settings
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services.session_events import TOOL_RESULT, SessionEventStore
from app.deepspace.services.session_log_crypto import (
    ENVELOPE_MARKER,
    FILE_KEY_ID,
    SessionLogCipher,
    SessionLogCryptoError,
    session_log_key_path,
)
from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite

pytestmark = pytest.mark.unit_no_db

KEYRING_MARKER = "keyring-plaintext-marker"
AT_REST_MARKER = "at-rest-plaintext-marker"
LEGACY_MARKER = "legacy-plaintext-marker"


def _key() -> str:
    """Return one base64url AES-256 key.

    Returns:
        The encoded key.
    """
    return base64.urlsafe_b64encode(b"7" * 32).decode("ascii")


def _keyring_json(kid: str = "session-kid") -> str:
    """Return a one-key keyring.

    Args:
        kid: The key id.

    Returns:
        The keyring's JSON.
    """
    return json.dumps({kid: _key()})


@pytest.fixture(scope="module")
def session_log_database(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Build the database schema once for the module.

    Args:
        tmp_path_factory: The pytest temporary directory factory.

    Returns:
        The SQLite engine.
    """
    root = tmp_path_factory.mktemp("session-log-crypto")
    engine = create_engine(f"sqlite:///{root / 'session-log.db'}")
    sanitize_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture(autouse=True)
def session_log_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Point the local key file at a fresh home for every check.

    Args:
        monkeypatch: The pytest environment patcher.
        tmp_path: The test's temporary directory.

    Yields:
        Nothing; the environment is restored by pytest.
    """
    monkeypatch.setenv("AVERQEL_HOME", str(tmp_path / "averqel-home"))
    monkeypatch.setenv("AKS_DEEPSPACE_SESSION_LOG_ENCRYPTION", "true")
    monkeypatch.delenv("AKS_DEEPSPACE_SESSION_LOG_ACTIVE_KID", raising=False)
    monkeypatch.delenv("AKS_DEEPSPACE_SESSION_LOG_KEYRING_JSON", raising=False)
    get_averqel_home.cache_clear()
    get_settings.cache_clear()
    yield
    get_averqel_home.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
def db(session_log_database: Any) -> Any:
    """Return an empty database session for one check.

    Args:
        session_log_database: The module's SQLite engine.

    Yields:
        The database session.
    """
    session = sessionmaker(bind=session_log_database)()
    session.execute(text("DELETE FROM deepspace_session_events"))
    session.execute(text("DELETE FROM conversations"))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _account(db: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create one conversation and return its owners.

    Args:
        db: The database session to write through.

    Returns:
        The tenant id, the user id, and the conversation id.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    db.add(
        Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            kind="deepspace",
            title="Session log encryption",
        )
    )
    db.commit()
    return tenant_id, user_id, conversation_id


def _append(
    db: Session,
    account: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    payload: dict[str, Any],
) -> DeepSpaceSessionEvent:
    """Append one event through the store.

    Args:
        db: The database session to write through.
        account: The tenant, user, and conversation the event belongs to.
        payload: The event's payload.

    Returns:
        The appended event.
    """
    tenant_id, user_id, conversation_id = account
    event = SessionEventStore(db).append(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        event_type=TOOL_RESULT,
        payload=payload,
    )
    db.commit()
    return event


def _raw_value(db: Session) -> Any:
    """Read the payload column without the ORM type.

    Args:
        db: The database session to read through.

    Returns:
        The column value as the driver returns it.
    """
    return db.execute(text("select payload from deepspace_session_events limit 1")).scalar_one()


def _raw_envelope(db: Session) -> dict[str, Any]:
    """Read the only event's encrypted envelope.

    Args:
        db: The database session to read through.

    Returns:
        The envelope.
    """
    value = _raw_value(db)
    envelope = json.loads(value) if isinstance(value, str) else value
    assert isinstance(envelope, dict)
    return envelope


def test_a_payload_round_trips_through_the_store(db: Session) -> None:
    payload = {"text": "round-trip marker", "nested": {"count": 1}}
    account = _account(db)
    event = _append(db, account, payload)

    events = SessionEventStore(db).events(
        tenant_id=account[0],
        user_id=account[1],
        conversation_id=account[2],
    )

    assert [stored.id for stored in events] == [event.id]
    assert events[0].payload == payload


def test_the_payload_column_holds_an_envelope_at_rest(db: Session) -> None:
    payload = {"text": AT_REST_MARKER}
    account = _account(db)
    _append(db, account, payload)

    raw = _raw_value(db)
    envelope = json.loads(raw) if isinstance(raw, str) else raw

    assert envelope[ENVELOPE_MARKER] == 1
    assert "kid" in envelope
    assert "nonce" in envelope
    assert "ciphertext" in envelope
    assert AT_REST_MARKER not in json.dumps(envelope)

    db.expire_all()
    stored = db.execute(select(DeepSpaceSessionEvent)).scalar_one()
    assert stored.payload == payload


def test_a_legacy_plaintext_row_reads_back_unchanged(db: Session) -> None:
    account = _account(db)
    tenant_id, user_id, conversation_id = account
    event_id = uuid.uuid4()
    legacy = {"text": LEGACY_MARKER}

    db.execute(
        text(
            "INSERT INTO deepspace_session_events "
            "(id, tenant_id, user_id, conversation_id, run_id, subagent_id, seq, "
            "event_type, turn, step, tool_name, payload, created_at) "
            "VALUES (:id, :tenant_id, :user_id, :conversation_id, NULL, NULL, 1, "
            ":event_type, NULL, NULL, NULL, :payload, :created_at)"
        ),
        {
            "id": event_id.hex,
            "tenant_id": tenant_id.hex,
            "user_id": user_id.hex,
            "conversation_id": conversation_id.hex,
            "event_type": TOOL_RESULT,
            "payload": json.dumps(legacy),
            "created_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
        },
    )
    db.commit()
    db.expire_all()

    stored = db.execute(select(DeepSpaceSessionEvent)).scalar_one()
    assert stored.payload == legacy


def test_keyring_mode_carries_the_kid_and_decrypts(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKS_DEEPSPACE_SESSION_LOG_ACTIVE_KID", "session-kid")
    monkeypatch.setenv("AKS_DEEPSPACE_SESSION_LOG_KEYRING_JSON", _keyring_json())
    get_settings.cache_clear()

    payload = {"text": KEYRING_MARKER}
    account = _account(db)
    event = _append(db, account, payload)

    envelope = _raw_envelope(db)
    assert envelope["kid"] == "session-kid"
    assert KEYRING_MARKER not in json.dumps(envelope)
    assert event.payload == payload
    assert not session_log_key_path().exists()


def test_a_flipped_ciphertext_byte_raises(db: Session) -> None:
    account = _account(db)
    event = _append(db, account, {"text": "tamper marker"})

    envelope = _raw_envelope(db)
    ciphertext = bytearray(base64.urlsafe_b64decode(envelope["ciphertext"]))
    ciphertext[0] ^= 0xFF
    envelope["ciphertext"] = base64.urlsafe_b64encode(bytes(ciphertext)).decode("ascii")

    db.execute(
        text("update deepspace_session_events set payload = :payload where id = :id"),
        {"payload": json.dumps(envelope), "id": event.id.hex},
    )
    db.commit()
    db.expire_all()

    with pytest.raises(SessionLogCryptoError, match="authentication"):
        db.execute(select(DeepSpaceSessionEvent)).scalar_one()


def test_disabled_encryption_stores_plaintext(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKS_DEEPSPACE_SESSION_LOG_ENCRYPTION", "false")
    get_settings.cache_clear()

    payload = {"text": AT_REST_MARKER}
    account = _account(db)
    event = _append(db, account, payload)

    raw = _raw_value(db)
    rendered = raw if isinstance(raw, str) else json.dumps(raw)
    assert ENVELOPE_MARKER not in rendered
    assert AT_REST_MARKER in rendered
    assert event.payload == payload


def test_an_append_without_a_key_writes_nothing(db: Session) -> None:
    account = _account(db)
    path = session_log_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"short")

    with pytest.raises(StatementError) as raised:
        _append(db, account, {"text": AT_REST_MARKER})
    assert isinstance(raised.value.orig, SessionLogCryptoError)
    db.rollback()

    assert db.execute(text("select payload from deepspace_session_events")).all() == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes are not available")
def test_key_file_mode_creates_a_private_key(db: Session) -> None:
    account = _account(db)
    _append(db, account, {"text": "key file marker"})

    path = session_log_key_path()
    assert path.is_file()
    assert path.stat().st_size == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _raw_envelope(db)["kid"] == FILE_KEY_ID


def test_cipher_round_trips_and_reports_a_bad_key_file() -> None:
    settings = Settings(_env_file=None)
    cipher = SessionLogCipher(settings)
    envelope = cipher.encrypt(b"plaintext")

    assert cipher.decrypt(envelope) == b"plaintext"

    path = session_log_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"short")

    with pytest.raises(SessionLogCryptoError, match="must hold 32 bytes"):
        SessionLogCipher(Settings(_env_file=None))


def test_a_cipher_without_its_active_key_raises() -> None:
    settings = Settings.model_construct(
        deepspace_session_log_active_kid="missing-kid",
        deepspace_session_log_keyring_json=_keyring_json("present-kid"),
    )

    with pytest.raises(SessionLogCryptoError, match="not in the keyring"):
        SessionLogCipher(settings)


def test_session_log_keyring_requires_the_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKS_DEEPSPACE_SESSION_LOG_ACTIVE_KID", raising=False)
    monkeypatch.delenv("AKS_DEEPSPACE_SESSION_LOG_KEYRING_JSON", raising=False)

    with pytest.raises(ValueError, match="must be set together"):
        Settings(_env_file=None, deepspace_session_log_active_kid="kid-only")


def test_session_log_keyring_requires_the_active_kid() -> None:
    with pytest.raises(ValueError, match="must exist in deepspace_session_log_keyring_json"):
        Settings(
            _env_file=None,
            deepspace_session_log_active_kid="missing-kid",
            deepspace_session_log_keyring_json=_keyring_json("present-kid"),
        )


def test_session_log_keyring_accepts_a_valid_key() -> None:
    settings = Settings(
        _env_file=None,
        deepspace_session_log_active_kid="session-kid",
        deepspace_session_log_keyring_json=_keyring_json(),
    )

    assert settings.deepspace_session_log_active_kid == "session-kid"
    assert settings.deepspace_session_log_encryption is True


def test_production_requires_a_shared_session_log_keyring() -> None:
    settings = Settings.model_construct(
        env="production",
        database_url="postgresql+psycopg://db",
        jwt_secret="a" * 40,
        refresh_token_hash_secret="b" * 40,
        minio_access_key="production-minio",
        minio_secret_key="production-minio-secret",
        deepspace_session_log_encryption=True,
        deepspace_session_log_active_kid=None,
        deepspace_session_log_keyring_json=None,
    )

    with pytest.raises(ValueError, match="requires deepspace_session_log_keyring_json"):
        settings.validate_production_hardening()
