import pytest
from sqlalchemy import create_engine, inspect, text

from app.platform.database.sqlite_compat import repair_local_schema_drift

pytestmark = pytest.mark.unit_no_db


def test_repair_local_schema_drift_adds_goal_runtime_columns() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE deepspace_goals (id TEXT PRIMARY KEY)"))

    assert repair_local_schema_drift(engine) is True
    columns = {column["name"] for column in inspect(engine).get_columns("deepspace_goals")}
    assert columns.issuperset(
        {
            "status",
            "phase",
            "activation",
            "revision",
            "max_goal_rounds",
            "rounds_started",
            "blocked_reason",
        }
    )
    assert repair_local_schema_drift(engine) is False


def test_repair_local_schema_drift_adds_goal_activation_and_index_preserving_rows() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE deepspace_goals "
                "(id TEXT PRIMARY KEY, status VARCHAR(32) NOT NULL DEFAULT 'active', "
                "phase VARCHAR(32) NOT NULL DEFAULT 'active')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO deepspace_goals (id, status, phase) "
                "VALUES ('goal-1', 'active', 'active')"
            )
        )

    assert repair_local_schema_drift(engine) is True
    columns = {column["name"] for column in inspect(engine).get_columns("deepspace_goals")}
    assert "activation" in columns
    indexes = {index["name"] for index in inspect(engine).get_indexes("deepspace_goals")}
    assert "ix_deepspace_goals_activation" in indexes
    with engine.connect() as connection:
        goal = connection.execute(
            text("SELECT id, activation FROM deepspace_goals WHERE id = 'goal-1'")
        ).one()
    assert tuple(goal) == ("goal-1", "disarmed")
    assert repair_local_schema_drift(engine) is False


def test_repair_local_schema_drift_adds_team_task_write_scopes() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE deepspace_team_tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO deepspace_team_tasks (id, title) VALUES ('task-1', 'Keep me')")
        )

    assert repair_local_schema_drift(engine) is True
    columns = {column["name"] for column in inspect(engine).get_columns("deepspace_team_tasks")}
    assert "write_scopes" in columns
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT id, title, write_scopes FROM deepspace_team_tasks")
        ).one()
    assert tuple(row) == ("task-1", "Keep me", "[]")
    assert repair_local_schema_drift(engine) is False
