from sqlalchemy import create_engine, inspect, text

from app.platform.database.sqlite_compat import repair_local_schema_drift


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
            "revision",
            "max_goal_rounds",
            "rounds_started",
            "blocked_reason",
        }
    )
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
