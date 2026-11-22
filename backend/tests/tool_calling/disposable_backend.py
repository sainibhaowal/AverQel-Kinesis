"""Disposable backend for isolated tool-calling tests.

Provides a minimal backend context for executing native tools in tests.
"""

from __future__ import annotations

import contextlib
import os

# Add backend to path
import sys
import tempfile
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.auth.dependencies import AuthContext
from app.core.config import Settings
from app.deepspace.services.tool_registry import ToolRegistry, build_native_tool_registry
from app.deepspace.services.tool_result_store import ToolResultStore
from app.deepspace.services.tools import (
    NATIVE_TOOL_ARGUMENT_MODELS,
    NATIVE_TOOL_DEFINITIONS,
    NATIVE_TOOL_MODES,
    dispatch_native_tool,
    unavailable_tool_names,
)
from app.deepspace.services.tools._base import ToolContext
from app.main import create_app


class DisposableBackend:
    """A minimal isolated backend for tool-calling tests."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or self._create_test_settings()
        self.temp_dir = Path(tempfile.mkdtemp(prefix="deepspace_tool_test_"))
        self.db_path = self.temp_dir / "test.db"
        self.engine = None
        self.session_factory = None
        self.app = None
        self.client: AsyncClient | None = None
        self.tool_registry: ToolRegistry | None = None
        self.tool_result_store: ToolResultStore | None = None
        self._initialized = False

    def _create_test_settings(self) -> Settings:
        """Create settings with all tools enabled for testing."""
        os.environ.update(
            {
                "DEEPSPACE_SHELL_ENABLED": "true",
                "DEEPSPACE_SANDBOX_ENABLED": "true",
                "DEEPSPACE_TERMINAL_ENABLED": "true",
                "DEEPSPACE_BUNDLED_OFFICE_ENABLED": "true",
                "DEEPSPACE_SSH_RUNTIME_ENABLED": "false",
                "DEEPSPACE_BROWSER_AUTOMATION_ENABLED": "false",
                "DEEPSPACE_COMPUTER_USE_ENABLED": "false",
                "DATABASE_URL": f"sqlite:///{self.db_path}",
                "SECRET_KEY": "test-secret-key-for-testing-only",
                "ENVIRONMENT": "test",
            }
        )

        from app.core.config import Settings as SettingsClass

        return SettingsClass()

    async def initialize(self) -> None:
        """Initialize the backend: DB, app, tool registry."""
        if self._initialized:
            return

        # Create database
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )

        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        from app.core.database import Base

        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )

        # Initialize tool registry (same as production)
        extra_contracts = {
            name: (model, NATIVE_TOOL_MODES[name])
            for name, model in NATIVE_TOOL_ARGUMENT_MODELS.items()
            if name in NATIVE_TOOL_MODES
        }
        self.tool_registry = build_native_tool_registry(
            NATIVE_TOOL_DEFINITIONS,
            extra_contracts=extra_contracts,
        )

        # Initialize tool result store
        self.tool_result_store = ToolResultStore(self.session_factory)

        # Create FastAPI app with test overrides
        self.app = create_app(self.settings)

        def get_test_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        from app.core.database import get_db as get_db_original

        self.app.dependency_overrides[get_db_original] = get_test_db

        # Create async client
        transport = ASGITransport(app=self.app)
        self.client = AsyncClient(transport=transport, base_url="http://test")

        self._initialized = True

    async def close(self) -> None:
        """Clean up resources."""
        if self.client:
            await self.client.aclose()
            self.client = None
        if self.engine:
            self.engine.dispose()
            self.engine = None
        import shutil

        with contextlib.suppress(Exception):
            shutil.rmtree(self.temp_dir)

    def get_db(self) -> Session:
        """Get a database session."""
        return self.session_factory()

    def get_auth_context(
        self, tenant_id: str = "test-tenant", user_id: str = "test-user"
    ) -> AuthContext:
        """Create an auth context for tool calls."""
        return AuthContext(
            tenant_id=(
                uuid.UUID(tenant_id)
                if isinstance(tenant_id, str) and len(tenant_id) == 36
                else uuid.uuid4()
            ),
            user_id=(
                uuid.UUID(user_id)
                if isinstance(user_id, str) and len(user_id) == 36
                else uuid.uuid4()
            ),
            roles=["admin"],
            permissions=["*"],
        )

    def create_tool_context(
        self,
        conversation_id: uuid.UUID | None = None,
        auth: AuthContext | None = None,
    ) -> ToolContext:
        """Create a minimal ToolContext for native tool execution."""
        return ToolContext(
            auth=auth or self.get_auth_context(),
            conversation_id=conversation_id or uuid.uuid4(),
            settings=self.settings,
            db=self.get_db(),
            task_store=SimpleNamespace(),
            tool_result_store=self.tool_result_store,
            provider_registry=SimpleNamespace(),
            provider_selection=SimpleNamespace(),
            web_search_options={},
            mcp_bindings={},
            native_executor=None,
            tool_policy=None,
            workspace_root=self.temp_dir,
        )

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a native tool through the real dispatch pipeline."""
        if not self._initialized:
            await self.initialize()

        # Validate against registry
        if self.tool_registry and self.tool_registry.contains(name):
            try:
                validated_args = self.tool_registry.validate(name, arguments)
            except Exception as e:
                return {"status": "validation_error", "error": str(e)}
        else:
            validated_args = arguments

        # Execute via dispatch_native_tool
        context = self.create_tool_context()
        try:
            result = await dispatch_native_tool(name, validated_args, context)
            return result or {"status": "no_result"}
        except Exception as e:
            return {"status": "execution_error", "error": str(e)}

    def get_available_tools(self) -> list[dict[str, Any]]:
        """Get all available tool definitions for the model."""
        unavailable = unavailable_tool_names(self.settings)
        return [
            self.tool_registry.emit(name)
            for name in self.tool_registry.names()
            if name not in unavailable
        ]


@pytest_asyncio.fixture(scope="function")
async def disposable_backend() -> AsyncGenerator[DisposableBackend]:
    """Pytest fixture providing a disposable backend per test."""
    backend = DisposableBackend()
    try:
        await backend.initialize()
        yield backend
    finally:
        await backend.close()


@pytest_asyncio.fixture(scope="session")
async def session_backend() -> AsyncGenerator[DisposableBackend]:
    """Pytest fixture providing a shared backend for the test session."""
    backend = DisposableBackend()
    try:
        await backend.initialize()
        yield backend
    finally:
        await backend.close()


class BackendTestClient:
    """Convenience wrapper for executing tools in tests."""

    def __init__(self, backend: DisposableBackend):
        self.backend = backend

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.backend.execute_tool(name, arguments)

    def get_available_tools(self) -> list[dict[str, Any]]:
        return self.backend.get_available_tools()

    async def health_check(self) -> bool:
        try:
            response = await self.backend.client.get("/health")
            return response.status_code == 200
        except Exception:
            return False


@pytest_asyncio.fixture
async def backend_client(disposable_backend: DisposableBackend) -> BackendTestClient:
    return BackendTestClient(disposable_backend)
