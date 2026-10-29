import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth.tenancy import TenantContext, get_tenant_context
from app.main import create_app
from app.platform.database.session import get_db

pytestmark = pytest.mark.unit_no_db


def test_dashboard_routes():
    app = create_app()
    mock_db = MagicMock()
    mock_db.execute.return_value.all.return_value = []
    mock_db.execute.return_value.scalar.return_value = 0

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )

    client = TestClient(app)

    # Test query-stats route
    res = client.get("/api/v1/dashboard/query-stats")
    assert res.status_code == 200
    data = res.json()
    assert "stats" in data
    assert "document_breakdown" in data
    assert "provider_runtimes" in data

    # Test stats route
    res = client.get("/api/v1/dashboard/stats")
    assert res.status_code == 200
    data = res.json()
    assert "total_documents" in data
    assert "total_queries" in data

    # Test overview route
    res = client.get("/api/v1/dashboard/overview")
    assert res.status_code == 200
    data = res.json()
    assert "stats" in data
    assert "document_breakdown" in data
