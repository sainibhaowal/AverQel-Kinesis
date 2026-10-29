from __future__ import annotations

import os

import uvicorn

# Resolve the .averqel home directory before any settings are loaded so that
# the DATABASE_URL and LOCAL_STORAGE_PATH env overrides are visible to
# pydantic-settings when it constructs the Settings singleton.
from app.core.averqel_home import (
    averqel_storage_path,
    ensure_local_provider_secret_keyring,
    get_averqel_home,
    sqlite_url,
)


def _configure_averqel_env() -> None:
    """Inject .averqel-rooted paths into the environment.

    This must run before ``get_settings()`` is first called so pydantic-settings
    picks up the correct paths when it reads ``AKS_DATABASE_URL`` and
    ``AKS_LOCAL_STORAGE_PATH``.

    The local deployment also turns the DeepSpace browser runtime on.  It is
    the single-machine app, where a browser on the host is the normal case,
    and the choice is made here so that it is the deployment's own decision
    rather than a rule hidden in the runtime.  A deployment that sets the
    variable keeps its own value.
    """
    home = get_averqel_home()
    # Only set if not already overridden by an operator (e.g. Docker deployment).
    os.environ.setdefault("AKS_DATABASE_URL", sqlite_url("knowledge.db"))
    os.environ.setdefault("AKS_LOCAL_STORAGE_PATH", str(averqel_storage_path()))
    os.environ.setdefault("AKS_DEEPSPACE_BROWSER_ENABLED", "true")
    os.environ.setdefault("AKS_DEEPSPACE_BROWSER_AUTOMATION_ENABLED", "true")
    os.environ.setdefault("AKS_DEEPSPACE_SANDBOX_ENABLED", "true")
    os.environ.setdefault("AKS_DEEPSPACE_SANDBOX_URL", "http://127.0.0.1:3020")
    os.environ.setdefault("AKS_DEEPSPACE_SANDBOX_TOKEN", "local-sandbox-e2e-token")
    # The standalone app has no Docker service name to resolve.  SearXNG is
    # installed into the user's AverQel home and supervised by the local API;
    # container deployments keep their explicit service endpoint instead.
    if not os.path.exists("/.dockerenv") and not os.environ.get("AKS_DOCKER_CONTAINER"):
        os.environ.setdefault("AKS_SEARXNG_BASE_URL", "http://127.0.0.1:8888")
        os.environ.setdefault("AKS_SEARXNG_LOCAL_RUNTIME_ENABLED", "true")
    if not os.environ.get("AKS_PROVIDER_SECRET_ACTIVE_KID") and not os.environ.get(
        "AKS_PROVIDER_SECRET_KEYRING_JSON"
    ):
        active_kid, keyring_json = ensure_local_provider_secret_keyring()
        os.environ["AKS_PROVIDER_SECRET_ACTIVE_KID"] = active_kid
        os.environ["AKS_PROVIDER_SECRET_KEYRING_JSON"] = keyring_json
    print(f"✓ AverQel home: {home}")


# Configure environment BEFORE importing database modules, which call get_settings()
# at module level (e.g., app.platform.database.session line 39).
_configure_averqel_env()

from datetime import UTC

from app.platform.database import model_registry  # noqa: F401
from app.platform.database.base import Base
from app.platform.database.session import get_engine
from app.platform.database.sqlite_compat import (
    repair_local_schema_drift,
    sanitize_metadata_for_sqlite,
)
from app.platform.embedded_redis import setup_embedded_redis


def seed_local_admin() -> None:
    """Ensure local workspace and full admin operator exist in SQLite."""
    import uuid
    from datetime import datetime

    from app.auth.models.role import Role
    from app.auth.models.tenant import Tenant
    from app.auth.models.user import User
    from app.auth.models.user_role import UserRole
    from app.platform.database.session import managed_db_session

    LOCAL_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
    LOCAL_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

    with managed_db_session() as session:
        tenant = session.query(Tenant).filter_by(id=LOCAL_TENANT_ID).first()
        if not tenant:
            tenant = Tenant(id=LOCAL_TENANT_ID, name="Local Default Workspace")
            session.add(tenant)
            session.flush()

        roles_list = [
            "admin",
            "staff",
            "feedback_manager",
            "collection_moderator",
            "support_manager",
            "user",
        ]
        for r_name in roles_list:
            role = session.query(Role).filter_by(name=r_name).first()
            if not role:
                role = Role(name=r_name, description=f"Local {r_name} role")
                session.add(role)
        session.flush()

        from app.auth.security import hash_password

        user = session.query(User).filter_by(id=LOCAL_USER_ID).first()
        if not user:
            user = User(
                id=LOCAL_USER_ID,
                tenant_id=LOCAL_TENANT_ID,
                email="admin@averqel.local",
                display_name="Local Admin",
                collection_code="LOCAL01",
                password_hash=hash_password("local_first_nopass"),
                is_active=True,
                onboarding_completed_at=datetime.now(UTC),
            )
            session.add(user)
            session.flush()
        elif user.password_hash == "local_first_nopass":
            user.password_hash = hash_password("local_first_nopass")
            session.flush()

        for r_name in [
            "admin",
            "staff",
            "feedback_manager",
            "collection_moderator",
            "support_manager",
        ]:
            role = session.query(Role).filter_by(name=r_name).first()
            if role:
                ur = (
                    session.query(UserRole)
                    .filter_by(tenant_id=LOCAL_TENANT_ID, user_id=LOCAL_USER_ID, role_id=role.id)
                    .first()
                )
                if not ur:
                    session.add(
                        UserRole(tenant_id=LOCAL_TENANT_ID, user_id=LOCAL_USER_ID, role_id=role.id)
                    )

        session.commit()
        print("✓ Local default admin user & workspace verified (admin@averqel.local)")


def _seed_user_default_conversation() -> None:
    """Create the pseudo-conversation used for user default presets.

    The pseudo-conversation has UUID int=0 and is used to store the
    user's default agent mode choice in the session log without a
    real conversation.
    """
    import uuid

    from app.deepspace.models.conversation import Conversation
    from app.platform.database.session import managed_db_session

    USER_DEFAULT_CONVERSATION_ID = uuid.UUID(int=0)
    LOCAL_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
    LOCAL_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

    with managed_db_session() as session:
        conv = session.query(Conversation).filter_by(id=USER_DEFAULT_CONVERSATION_ID).first()
        if not conv:
            conv = Conversation(
                id=USER_DEFAULT_CONVERSATION_ID,
                tenant_id=LOCAL_TENANT_ID,
                user_id=LOCAL_USER_ID,
                title="__user_default_preset__",
                kind="deepspace",
            )
            session.add(conv)
            session.commit()


def init_local_system() -> None:
    """Prepare database, storage, and embedded cache for serverless/dockerless execution."""
    # 0. Wire .averqel paths into environment before settings singleton loads.
    _configure_averqel_env()

    # 1. Initialize embedded in-memory Redis fallback
    setup_embedded_redis()

    # 2. Initialize database inside ~/.averqel/db/
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        sanitize_metadata_for_sqlite(Base.metadata)
        Base.metadata.create_all(bind=engine)
        repair_local_schema_drift(engine)
        from app.core.averqel_home import averqel_db_path

        print(f"✓ Local SQLite database initialized at {averqel_db_path('knowledge.db')}")
        seed_local_admin()
        _seed_user_default_conversation()

    # Allow skipping MCP catalog sync for faster local startup (e.g., CI or dev).
    if os.environ.get("AKS_SKIP_MCP_CATALOG_SYNC", "").strip().lower() not in {"1", "true", "yes"}:
        try:
            from scripts.seed_mcp_catalog import seed_mcp_catalog

            mcp_res = seed_mcp_catalog()
            print(f"✓ Local MCP catalog verified ({mcp_res.get('total', 0)} connectors)")
        except Exception as exc:
            print("Notice: could not auto-seed MCP catalog:", exc)
    else:
        print("⚠ Skipping MCP catalog sync (AKS_SKIP_MCP_CATALOG_SYNC set)")

    # 3. Confirm storage directory inside ~/.averqel/storage/
    storage = averqel_storage_path()
    print(f"✓ Local storage initialized at {storage}")


if __name__ == "__main__":
    init_local_system()
    port = int(os.environ.get("PORT", os.environ.get("BACKEND_PORT", 10000)))
    reload = os.environ.get("RELOAD", "false").lower() in ("true", "1")
    reload_dirs = [os.path.join(os.path.dirname(__file__), "app")] if reload else None

    # Ensure port is available before trying to bind
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    is_open = sock.connect_ex(("127.0.0.1", port)) == 0
    sock.close()
    if is_open:
        print(f"✓ Backend server is already running and active on http://127.0.0.1:{port}")
        import sys

        sys.exit(0)

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=reload,
        reload_dirs=reload_dirs,
    )
