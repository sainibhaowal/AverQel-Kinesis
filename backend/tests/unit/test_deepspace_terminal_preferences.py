from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.auth.dependencies import AuthContext
from app.deepspace.api import workspace as workspace_api
from app.deepspace.services.storage_runtime import DeepSpaceStorageRuntime
from app.deepspace.services.terminal_preferences import (
    TERMINAL_PREFERENCES_DOMAIN,
    TerminalPreferencesStore,
)

pytestmark = pytest.mark.unit_no_db


def _auth(tenant_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None) -> AuthContext:
    return AuthContext(
        user_id=user_id or uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        roles=frozenset({"user"}),
        token_id="test",
    )


def test_terminal_preferences_persist_and_are_isolated_by_tenant_and_user(tmp_path: Path) -> None:
    async def exercise() -> None:
        runtime = DeepSpaceStorageRuntime(tmp_path)
        try:
            domain = await runtime.storage.domain.open(TERMINAL_PREFERENCES_DOMAIN)
            store = TerminalPreferencesStore(domain)
            tenant = uuid.uuid4()
            owner = _auth(tenant, uuid.uuid4())
            another_user = _auth(tenant, uuid.uuid4())
            another_tenant = _auth(uuid.uuid4(), owner.user_id)

            assert (await store.get(owner)).as_dict() == {
                "default_shell": None,
                "rows": 40,
                "columns": 160,
            }
            saved = await store.put(
                owner,
                default_shell="pwsh",
                rows=48,
                columns=132,
            )
            assert saved.as_dict() == {
                "default_shell": "pwsh",
                "rows": 48,
                "columns": 132,
            }
            assert (await store.get(owner)) == saved
            assert (await store.get(another_user)).default_shell is None
            assert (await store.get(another_tenant)).default_shell is None
        finally:
            await runtime.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("default_shell", "rows", "columns"),
    [("fish", 40, 160), ("bash", 9, 160), ("bash", 40, 401), ("bash", True, 160)],
)
def test_terminal_preferences_reject_invalid_values(
    tmp_path: Path,
    default_shell: str,
    rows: int,
    columns: int,
) -> None:
    async def exercise() -> None:
        runtime = DeepSpaceStorageRuntime(tmp_path)
        try:
            domain = await runtime.storage.domain.open(TERMINAL_PREFERENCES_DOMAIN)
            store = TerminalPreferencesStore(domain)
            with pytest.raises(ValueError):
                await store.put(
                    _auth(),
                    default_shell=default_shell,  # type: ignore[arg-type]
                    rows=rows,
                    columns=columns,
                )
        finally:
            await runtime.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("persisted", "expected"),
    [
        ([], {"default_shell": None, "rows": 40, "columns": 160}),
        (
            {"default_shell": "fish", "rows": 48, "columns": 132},
            {"default_shell": None, "rows": 48, "columns": 132},
        ),
        (
            {"default_shell": "bash", "rows": True, "columns": 132},
            {"default_shell": "bash", "rows": 40, "columns": 132},
        ),
        (
            {"default_shell": "bash", "rows": 48, "columns": 401},
            {"default_shell": "bash", "rows": 48, "columns": 160},
        ),
    ],
)
def test_terminal_preferences_recover_from_invalid_persisted_values(
    tmp_path: Path,
    persisted: object,
    expected: dict[str, object],
) -> None:
    async def exercise() -> None:
        runtime = DeepSpaceStorageRuntime(tmp_path)
        try:
            domain = await runtime.storage.domain.open(TERMINAL_PREFERENCES_DOMAIN)
            store = TerminalPreferencesStore(domain)
            auth = _auth()
            await domain.table("users").put(store._key(auth), persisted)
            assert (await store.get(auth)).as_dict() == expected
        finally:
            await runtime.close()

    asyncio.run(exercise())


def test_open_terminal_uses_saved_defaults_and_explicit_values_override_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        runtime = DeepSpaceStorageRuntime(tmp_path)
        try:
            domain = await runtime.storage.domain.open(TERMINAL_PREFERENCES_DOMAIN)
            store = TerminalPreferencesStore(domain)
            auth = _auth()
            await store.put(auth, default_shell="bash", rows=50, columns=140)
            request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(deepspace_terminal_preferences=store))
            )
            captured: dict[str, object] = {}
            session = SimpleNamespace(
                id="terminal-1",
                shell="bash",
                pty=True,
                cwd=tmp_path,
                process=SimpleNamespace(peek=lambda _size: "ready"),
            )

            class Manager:
                async def open(self, **kwargs):
                    captured.update(kwargs)
                    return session

            monkeypatch.setattr(workspace_api, "_conversation_workspace", lambda *_: tmp_path)
            monkeypatch.setattr(workspace_api, "terminal_manager", lambda: Manager())
            monkeypatch.setattr(workspace_api, "available_shells", lambda: ("bash", "pwsh"))
            monkeypatch.setattr(workspace_api, "default_shell", lambda: "pwsh")
            settings = SimpleNamespace(deepspace_terminal_enabled=True)

            default_result = await workspace_api.open_terminal_session(
                uuid.uuid4(), {}, request, auth, object(), settings
            )
            assert default_result["session_id"] == "terminal-1"
            assert captured["shell"] == "bash"
            assert captured["rows"] == 50
            assert captured["columns"] == 140

            await workspace_api.open_terminal_session(
                uuid.uuid4(),
                {"shell": "pwsh", "rows": 30, "columns": 100},
                request,
                auth,
                object(),
                settings,
            )
            assert captured["shell"] == "pwsh"
            assert captured["rows"] == 30
            assert captured["columns"] == 100
        finally:
            await runtime.close()

    asyncio.run(exercise())
