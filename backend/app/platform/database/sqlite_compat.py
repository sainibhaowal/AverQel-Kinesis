from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import MetaData, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.ext.compiler import compiles

logger = logging.getLogger(__name__)

# Register type compilation for SQLite
try:
    from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]

    @compiles(Vector, "sqlite")
    def _compile_vector_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
        return "TEXT"

except Exception:  # noqa: BLE001
    pass


@compiles(TSVECTOR, "sqlite")
def _compile_tsvector_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "TEXT"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


import sqlite3
import uuid

sqlite3.register_adapter(uuid.UUID, lambda u: str(u))

from sqlalchemy.dialects.postgresql import UUID as PG_UUID


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(36)"


if not hasattr(SQLiteTypeCompiler, "visit_UUID"):
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "CHAR(36)"  # type: ignore[method-assign]

# Ensure SQLiteTypeCompiler knows how to render these types directly
if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
    SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"  # type: ignore[method-assign]
if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore[method-assign]
if not hasattr(SQLiteTypeCompiler, "visit_TSVECTOR"):
    SQLiteTypeCompiler.visit_TSVECTOR = lambda self, type_, **kw: "TEXT"  # type: ignore[method-assign]


from sqlalchemy.schema import DefaultClause


def sanitize_metadata_for_sqlite(metadata: MetaData) -> None:
    """Sanitize PostgreSQL-specific DDL clauses in table metadata when running on SQLite."""
    for table in metadata.tables.values():
        for col in table.columns:
            if col.server_default is not None:
                sd_arg = getattr(col.server_default, "arg", None)
                if sd_arg is not None:
                    sd_text = str(sd_arg)
                    if "::" in sd_text:
                        # Strip postgres casts like '::jsonb' or '::uuid[]'
                        cleaned = re.sub(r"::[a-zA-Z_]+(?:\[\])?", "", sd_text)
                        col.server_default = DefaultClause(text(cleaned))
                    if "gen_random_uuid()" in sd_text:
                        col.server_default = DefaultClause(text("(lower(hex(randomblob(16))))"))


def repair_local_schema_drift(engine: Any) -> bool:
    """Add missing additive DeepSpace columns to an existing local SQLite database.

    ``Base.metadata.create_all`` creates missing tables but does not add new
    columns to tables created by an earlier local build. Add each compatible
    field independently so a partially upgraded local database can recover
    without dropping the user's records.

    Args:
        engine: SQLAlchemy engine used by the local application.

    Returns:
        Whether the local schema was changed.
    """
    if getattr(engine.dialect, "name", None) != "sqlite":
        return False
    inspector = sa_inspect(engine)
    changed = False
    if inspector.has_table("deepspace_goals"):
        columns = {column["name"] for column in inspector.get_columns("deepspace_goals")}
        additions = {
            "status": "VARCHAR(32) NOT NULL DEFAULT 'active'",
            "phase": "VARCHAR(32) NOT NULL DEFAULT 'active'",
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "max_goal_rounds": "INTEGER NOT NULL DEFAULT 3",
            "rounds_started": "INTEGER NOT NULL DEFAULT 0",
            "blocked_reason": "JSON",
        }
        missing = [(name, ddl) for name, ddl in additions.items() if name not in columns]
        if missing:
            with engine.begin() as connection:
                for name, ddl in missing:
                    connection.execute(text(f"ALTER TABLE deepspace_goals ADD COLUMN {name} {ddl}"))
                connection.execute(
                    text(
                        "UPDATE deepspace_goals "
                        "SET phase = CASE "
                        "WHEN status = 'reached' THEN 'complete' "
                        "WHEN status = 'abandoned' THEN 'blocked' "
                        "ELSE 'active' END"
                    )
                )
            changed = True
        index_names = {index["name"] for index in inspector.get_indexes("deepspace_goals")}
        if "ix_deepspace_goals_phase" not in index_names:
            with engine.begin() as connection:
                connection.execute(
                    text("CREATE INDEX ix_deepspace_goals_phase " "ON deepspace_goals (phase)")
                )
            changed = True

    if inspector.has_table("deepspace_team_tasks"):
        task_columns = {column["name"] for column in inspector.get_columns("deepspace_team_tasks")}
        if "write_scopes" not in task_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE deepspace_team_tasks "
                        "ADD COLUMN write_scopes JSON NOT NULL DEFAULT '[]'"
                    )
                )
            changed = True

    return changed


import json
import math

from sqlalchemy import event
from sqlalchemy.engine import Engine


def _parse_vec(v: Any) -> list[float]:
    if isinstance(v, (list, tuple)):
        return [float(x) for x in v]
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                return [float(x) for x in json.loads(s)]
            except Exception:
                pass
        return [float(x) for x in s.split(",") if x.strip()]
    return []


def _sqlite_l2_distance(a: Any, b: Any) -> float:
    try:
        va, vb = _parse_vec(a), _parse_vec(b)
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(va, vb, strict=False)))
    except Exception:
        return 0.0


def _sqlite_cosine_distance(a: Any, b: Any) -> float:
    try:
        va, vb = _parse_vec(a), _parse_vec(b)
        dot = sum(x * y for x, y in zip(va, vb, strict=False))
        na = math.sqrt(sum(x * x for x in va))
        nb = math.sqrt(sum(x * x for x in vb))
        if na == 0 or nb == 0:
            return 1.0
        return max(0.0, 1.0 - (dot / (na * nb)))
    except Exception:
        return 1.0


def _sqlite_match_ts(content: Any, query: Any) -> int:
    if not content or not query:
        return 1
    tokens = [t.lower() for t in str(query).split() if len(t) > 2]
    val_lower = str(content).lower()
    return 1 if (not tokens or any(t in val_lower for t in tokens)) else 0


@event.listens_for(Engine, "connect")
def _register_sqlite_engine_functions(dbapi_connection: Any, connection_record: Any) -> None:
    """Register PostgreSQL and vector emulation functions on SQLite connections."""
    try:
        dbapi_connection.create_function("l2_distance", 2, _sqlite_l2_distance)
        dbapi_connection.create_function("cosine_distance", 2, _sqlite_cosine_distance)
        dbapi_connection.create_function("hashtext", 1, lambda v: hash(str(v)) % 2147483647)
        dbapi_connection.create_function("plainto_tsquery", 2, lambda c, q: str(q))
        dbapi_connection.create_function("to_tsquery", 2, lambda c, q: str(q))
        dbapi_connection.create_function("websearch_to_tsquery", 2, lambda c, q: str(q))
        dbapi_connection.create_function("ts_rank_cd", 2, lambda v, q: 1.0)
        dbapi_connection.create_function("match_ts", 2, _sqlite_match_ts)
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))
    except Exception:  # noqa: BLE001
        pass


@event.listens_for(Engine, "before_cursor_execute", retval=True)
def _intercept_sqlite_cursor_execute(
    conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
) -> tuple[str, Any]:
    if conn.dialect.name == "sqlite":
        stmt_clean = " ".join(statement.strip().split()).upper()
        if (
            stmt_clean.startswith("SET ROLE ")
            or stmt_clean == "RESET ROLE"
            or stmt_clean.startswith("SET LOCAL ROLE ")
            or "SET_CONFIG(" in stmt_clean
            or "PG_ADVISORY" in stmt_clean
            or "PG_TRY_ADVISORY" in stmt_clean
        ):
            dummy_params = () if isinstance(parameters, (list, tuple)) else {}
            return "SELECT 1", dummy_params

        # Rewrite pgvector and tsquery operators for SQLite
        if "<->" in statement:
            statement = re.sub(
                r"([a-zA-Z0-9_\.\"]+)\s*<\->\s*([a-zA-Z0-9_\.\?\:\$\(\)\,\'\" ]+?)(?=\s+(?:ORDER|WHERE|LIMIT|AND|OR|AS|\)|$))",
                r"l2_distance(\1, \2)",
                statement,
                flags=re.IGNORECASE,
            )
        if "<=>" in statement:
            statement = re.sub(
                r"([a-zA-Z0-9_\.\"]+)\s*<=>\s*([a-zA-Z0-9_\.\?\:\$\(\)\,\'\" ]+?)(?=\s+(?:ORDER|WHERE|LIMIT|AND|OR|AS|\)|$))",
                r"cosine_distance(\1, \2)",
                statement,
                flags=re.IGNORECASE,
            )
        if "@@" in statement:
            statement = re.sub(
                r"([a-zA-Z0-9_\.\"]+)\s*@@\s*([a-zA-Z0-9_\.\?\:\$\(\)\,\'\" ]+?)(?=\s+(?:ORDER|WHERE|LIMIT|AND|OR|AS|\)|$))",
                r"match_ts(\1, \2)",
                statement,
                flags=re.IGNORECASE,
            )
    return statement, parameters
