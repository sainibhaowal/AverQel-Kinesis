from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.platform.database.sqlite_compat import sanitize_metadata_for_sqlite
from app.system.services.metrics_service import DB_CONNECTION_CHECKOUT_DURATION_SECONDS

logger = logging.getLogger(__name__)


#: The tenant identity a connection carries when no request scoped it.
#:
#: No tenant owns this identity, so a connection that reaches the pool
#: without a scope matches no row.
UNSCOPED_TENANT_ID = UUID("00000000-0000-0000-0000-000000000000")


def _observe_checkout_duration(duration_seconds: float) -> None:
    """Record DB checkout timing without breaking request flow."""
    try:
        DB_CONNECTION_CHECKOUT_DURATION_SECONDS.observe(duration_seconds)
    except Exception:  # noqa: BLE001
        logger.debug("DB checkout metric observation failed.", exc_info=True)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Create and cache the SQLAlchemy engine."""
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        path_str = settings.database_url.replace("sqlite:///", "").replace("sqlite://", "")
        if path_str and path_str != ":memory:":
            Path(path_str).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
            # Enable SQLite extension loading for sqlite-vec
            dbapi_connection.enable_load_extension(True)

            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

            def _greatest(*args: Any) -> Any:
                filtered = [a for a in args if a is not None]
                return max(filtered) if filtered else None

            def _least(*args: Any) -> Any:
                filtered = [a for a in args if a is not None]
                return min(filtered) if filtered else None

            dbapi_connection.create_function("greatest", -1, _greatest)
            dbapi_connection.create_function("least", -1, _least)

        from app.platform.database.base import Base

        sanitize_metadata_for_sqlite(Base.metadata)
        return engine

    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_use_lifo=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
    )
    return engine


@event.listens_for(Engine, "reset")
def _clear_tenant_scope(dbapi_connection: Any, connection_record: Any, reset_state: Any) -> None:
    """Drop the tenant scope when a connection returns to the pool.

    The tenant scope is session-scoped so that it survives a commit inside
    one request. A pooled connection must not carry one request's scope into
    the next, so the scope is cleared whenever the connection goes back to
    the pool.

    Args:
        dbapi_connection: The connection that is going back to the pool.
        connection_record: The pool's record for the connection.
        reset_state: The pool's state for the connection.
    """
    del connection_record
    del reset_state
    if not type(dbapi_connection).__module__.startswith("psycopg"):
        # The tenant scope is a PostgreSQL setting, and another database
        # has no scope to clear.
        return
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            (str(UNSCOPED_TENANT_ID),),
        )
        cursor.close()
        # The statement opened a transaction. Ending it leaves the
        # connection idle, which is the state the pool hands it out in.
        dbapi_connection.rollback()
    except Exception:  # noqa: BLE001
        logger.debug("Tenant scope could not be cleared when a connection was pooled.")


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Create and cache the SQLAlchemy session factory."""
    return sessionmaker(
        bind=get_engine(),
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


def SessionLocal() -> Session:  # noqa: N802
    """Compatibility alias for get_session_factory()()."""
    return get_session_factory()()


@contextmanager
def managed_db_session() -> Generator[Session]:
    """Provide a database session with the same safety boundary as ``get_db``."""
    settings = get_settings()
    checkout_start = time.perf_counter()
    db = get_session_factory()()
    _observe_checkout_duration(time.perf_counter() - checkout_start)

    role_applied = False
    is_sqlite = db.bind.dialect.name == "sqlite" if db.bind else False

    try:
        if not is_sqlite:
            db.execute(text("SET ROLE aks_app"))
            role_applied = True
            # Bound only database waits.  This prevents one blocked row or query
            # from consuming every worker and turning a normal UI read into the
            # browser's 30-second client timeout.
            db.execute(
                text(
                    "SELECT set_config('statement_timeout', :timeout, true), "
                    "set_config('lock_timeout', :lock_timeout, true)"
                ),
                {
                    "timeout": f"{settings.database_statement_timeout_seconds}s",
                    "lock_timeout": f"{settings.database_lock_timeout_seconds}s",
                },
            )
        yield db
    finally:
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("Session rollback during cleanup failed.", exc_info=True)

        # The tenant scope is session-scoped, so it outlives the request that
        # set it unless the connection is cleared before it goes back to the
        # pool.
        clear_db_tenant_context(db)

        if role_applied:
            try:
                db.execute(text("RESET ROLE"))
                db.commit()
            except Exception:  # noqa: BLE001
                logger.debug("RESET ROLE during cleanup failed.", exc_info=True)
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Session rollback after RESET ROLE failure failed.",
                        exc_info=True,
                    )

        try:
            db.close()
        except Exception:  # noqa: BLE001
            logger.warning("Database session close failed.", exc_info=True)


def get_db() -> Generator[Session]:
    """Yield a request-scoped database session with role setup and cleanup."""
    with managed_db_session() as db:
        yield db


def set_db_tenant_context(db: Session, tenant_id: UUID | str) -> None:
    """Bind tenant id into the current DB session context.

    The scope is session-scoped so a commit does not drop it and a query
    that follows a write still runs under the tenant it belongs to.  A
    pooled connection is cleared when it returns to the pool, so no
    connection carries one request's scope into the next.

    Args:
        db: The database session.
        tenant_id: The tenant the session is scoped to, or ``bypass`` for the
            deployment's own cross-tenant scope.
    """
    bind = getattr(db, "bind", None)
    if bind and getattr(bind, "dialect", None) and getattr(bind.dialect, "name", None) == "sqlite":
        return
    scope = str(tenant_id)
    info = getattr(db, "info", None)
    if isinstance(info, dict):
        info["tenant_context"] = scope
    if hasattr(db, "execute"):
        db.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
            {"tenant_id": scope},
        )


def clear_db_tenant_context(db: Session) -> None:
    """Drop the tenant scope a session applied.

    The reset value is an identity no tenant owns, so a connection that is
    reused without a scope matches no row.

    Args:
        db: The database session.
    """
    bind = getattr(db, "bind", None)
    if bind and getattr(bind, "dialect", None) and getattr(bind.dialect, "name", None) == "sqlite":
        return
    try:
        db.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
            {"tenant_id": str(UNSCOPED_TENANT_ID)},
        )
    except Exception:  # noqa: BLE001
        logger.debug("Tenant scope could not be cleared.", exc_info=True)


def reset_db_state() -> None:
    """Clear cached DB engine and session factory state."""
    get_session_factory.cache_clear()
    get_engine.cache_clear()
