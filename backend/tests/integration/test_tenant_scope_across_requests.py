"""Tests for the tenant scope across requests.

A request's tenant scope must not reach the next request. The scope is
session-scoped so it survives a commit inside one request, so the request
path has to clear it before the connection goes back to the pool.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.platform.database.session import get_session_factory
from tests.conftest import SeededUser


def _auth_headers(seeded: SeededUser) -> dict[str, str]:
    """Build the request headers for one seeded user.

    Args:
        seeded: The seeded user.

    Returns:
        The headers.
    """
    from app.auth.dependencies import create_access_token
    from app.core.config import get_settings

    token = create_access_token(
        user_id=seeded.user_id,
        tenant_id=seeded.tenant_id,
        roles={"admin"},
        settings=get_settings(),
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": str(seeded.tenant_id),
    }


def test_a_request_does_not_leave_its_tenant_scope_on_the_connection(
    client: TestClient,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    first = seed_user("Scope One", "scope-one@example.com", "StrongPass!1234", ("admin",))
    second = seed_user("Scope Two", "scope-two@example.com", "StrongPass!1234", ("admin",))

    first_response = client.get("/api/v1/auth/profile", headers=_auth_headers(first))
    assert first_response.status_code == 200
    assert first_response.json()["tenant_id"] == str(first.tenant_id)

    second_response = client.get("/api/v1/auth/profile", headers=_auth_headers(second))
    assert second_response.status_code == 200
    assert second_response.json()["tenant_id"] == str(second.tenant_id)


def test_clearing_the_scope_leaves_the_unscoped_identity(
    db_session: Session,
    seed_user: Callable[[str, str, str, tuple[str, ...]], SeededUser],
) -> None:
    from app.platform.database.session import (
        UNSCOPED_TENANT_ID,
        clear_db_tenant_context,
        set_db_tenant_context,
    )

    seeded = seed_user("Scope Tenant", "scope-pool@example.com", "StrongPass!1234", ("admin",))
    session = get_session_factory()()
    try:
        set_db_tenant_context(session, seeded.tenant_id)
        scope = session.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar()
        assert str(scope or "") == str(seeded.tenant_id)

        clear_db_tenant_context(session)
        cleared = session.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar()
        assert str(cleared or "") == str(UNSCOPED_TENANT_ID)
    finally:
        session.rollback()
        session.close()
    del db_session
