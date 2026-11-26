"""Tests for the VitalsService (DB-independent parts)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the module, not the class directly
import app.system.services.vitals_service as vitals_service_module

pytestmark = pytest.mark.unit_no_db


def _mock_collect():
    """Mock for _collect_database_vitals that returns a default dict."""
    return {
        "llm": "disconnected",
        "web_search": "unavailable",
        "sources": 0,
        "connector_statuses": {},
    }


class TestVitalsServiceInternet:
    """Tests for internet connectivity check."""

    @patch(
        "app.system.services.vitals_service.VitalsService._collect_database_vitals",
        return_value={
            "llm": "disconnected",
            "web_search": "unavailable",
            "sources": 0,
            "connector_statuses": {},
        },
    )
    @patch("httpx.AsyncClient")
    async def test_get_system_vitals_internet_connected(
        self, mock_client_class, mock_collect
    ) -> None:
        """Test internet connectivity check when connected."""
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_client.get.return_value = mock_response
        # Mock the async context manager
        mock_client_class.return_value.__aenter__.return_value = mock_client

        vitals = await vitals_service_module.VitalsService.get_system_vitals(
            tenant_id="test-tenant"
        )

        assert vitals["internet"] == "connected"
        mock_client.get.assert_called_once_with("https://www.google.com")

    @patch(
        "app.system.services.vitals_service.VitalsService._collect_database_vitals",
        return_value={
            "llm": "disconnected",
            "web_search": "unavailable",
            "sources": 0,
            "connector_statuses": {},
        },
    )
    @patch("httpx.AsyncClient")
    async def test_get_system_vitals_internet_disconnected(
        self, mock_client_class, mock_collect
    ) -> None:
        """Test internet connectivity check when disconnected."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection failed")
        # Mock the async context manager
        mock_client_class.return_value.__aenter__.return_value = mock_client

        vitals = await vitals_service_module.VitalsService.get_system_vitals(
            tenant_id="test-tenant"
        )

        assert vitals["internet"] == "disconnected"

    @patch(
        "app.system.services.vitals_service.VitalsService._collect_database_vitals",
        side_effect=TimeoutError(),
    )
    async def test_get_system_vitals_timeout(self, mock_collect) -> None:
        """Test timeout handling for database vitals."""
        vitals = await vitals_service_module.VitalsService.get_system_vitals(
            tenant_id="test-tenant"
        )

        assert "internet" in vitals
        assert "llm" in vitals
        assert "web_search" in vitals
        assert "sources" in vitals
        assert "connector_statuses" in vitals

    @patch(
        "app.system.services.vitals_service.VitalsService._collect_database_vitals",
        side_effect=Exception("DB error"),
    )
    async def test_get_system_vitals_database_error(self, mock_collect) -> None:
        """Test handling of database errors."""
        vitals = await vitals_service_module.VitalsService.get_system_vitals(
            tenant_id="test-tenant"
        )

        assert "internet" in vitals
        assert "llm" in vitals
        assert "web_search" in vitals
        assert "sources" in vitals
        assert "connector_statuses" in vitals


class TestVitalsServiceDatabase:
    """Tests for _collect_database_vitals method."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        return db

    @pytest.fixture
    def mock_settings(self):
        # Use a plain object with instance attribute (not class attribute)
        class Settings:
            def __init__(self):
                self.llm_provider = "disabled"

        return Settings()

    def _make_result(self, first_return=None, scalar_return=None, iter_return=None):
        """Create a mock result object for db.execute()."""
        mock_result = MagicMock()
        # Always configure scalars().first() to return first_return (even if None)
        mock_result.scalars.return_value.first.return_value = first_return
        if scalar_return is not None:
            # Make scalar() a callable that returns the value
            mock_result.scalar = MagicMock(return_value=scalar_return)
        else:
            mock_result.scalar = MagicMock(return_value=None)
        if iter_return is not None:
            # Make __iter__ return an iterator
            mock_result.__iter__ = MagicMock(return_value=iter(iter_return))
        else:
            mock_result.__iter__ = MagicMock(return_value=iter([]))
        return mock_result

    def _make_timeout_result(self):
        """Create a mock result for the SET LOCAL statement_timeout query."""
        return self._make_result()

    def _setup_db_mock(self, mock_db, execute_results):
        """Configure mock_db.execute to return different results for each call."""
        mock_execute = MagicMock()
        mock_execute.side_effect = execute_results
        mock_db.execute = mock_execute
        return mock_db

    def test_collect_database_vitals_llm_assignment(self, mock_db, mock_settings) -> None:
        """Test LLM assignment logic."""
        mock_assignment = MagicMock()
        # Query 0: SET LOCAL statement_timeout
        # Query 1: chat assignment returns assignment (query 2 skipped)
        # Query 3: web_search assignment returns None
        # Query 4: web_search config returns None
        # Query 5: sources count returns 0
        # Query 6: connector statuses returns empty
        execute_results = [
            self._make_timeout_result(),  # query 0: SET LOCAL
            self._make_result(first_return=mock_assignment),  # query 1: chat assignment
            self._make_result(first_return=None),  # query 3: web_search assignment
            self._make_result(first_return=None),  # query 4: web_search config
            self._make_result(scalar_return=0),  # query 5: sources count
            self._make_result(iter_return=[]),  # query 6: connector statuses
        ]
        self._setup_db_mock(mock_db, execute_results)

        with patch(
            "app.system.services.vitals_service.get_session_factory", return_value=lambda: mock_db
        ):
            result = vitals_service_module.VitalsService._collect_database_vitals(
                "test-tenant", mock_settings
            )

        assert result["llm"] == "connected"

    def test_collect_database_vitals_llm_config(self, mock_db, mock_settings) -> None:
        """Test LLM config fallback when no assignment."""
        # Query 0: SET LOCAL statement_timeout
        # Query 1: chat assignment returns None
        # Query 2: chat config returns config
        # Query 3: web_search assignment returns None
        # Query 4: web_search config returns None
        # Query 5: sources count returns 0
        # Query 6: connector statuses returns empty
        execute_results = [
            self._make_timeout_result(),  # query 0: SET LOCAL
            self._make_result(first_return=None),  # query 1: chat assignment
            self._make_result(first_return=MagicMock()),  # query 2: chat config
            self._make_result(first_return=None),  # query 3: web_search assignment
            self._make_result(first_return=None),  # query 4: web_search config
            self._make_result(scalar_return=0),  # query 5: sources count
            self._make_result(iter_return=[]),  # query 6: connector statuses
        ]
        self._setup_db_mock(mock_db, execute_results)

        with patch(
            "app.system.services.vitals_service.get_session_factory", return_value=lambda: mock_db
        ):
            result = vitals_service_module.VitalsService._collect_database_vitals(
                "test-tenant", mock_settings
            )

        assert result["llm"] == "connected"

    def test_collect_database_vitals_llm_disabled(self, mock_db, mock_settings) -> None:
        """Test LLM disabled case."""
        # Query 0: SET LOCAL statement_timeout
        # Query 1: chat assignment returns None
        # Query 2: chat config returns None
        # Query 3: web_search assignment returns None
        # Query 4: web_search config returns None
        # Query 5: sources count returns 0
        # Query 6: connector statuses returns empty
        execute_results = [
            self._make_timeout_result(),  # query 0: SET LOCAL
            self._make_result(first_return=None),  # query 1: chat assignment
            self._make_result(first_return=None),  # query 2: chat config
            self._make_result(first_return=None),  # query 3: web_search assignment
            self._make_result(first_return=None),  # query 4: web_search config
            self._make_result(scalar_return=0),  # query 5: sources count
            self._make_result(iter_return=[]),  # query 6: connector statuses
        ]
        self._setup_db_mock(mock_db, execute_results)

        with patch(
            "app.system.services.vitals_service.get_session_factory", return_value=lambda: mock_db
        ):
            result = vitals_service_module.VitalsService._collect_database_vitals(
                "test-tenant", mock_settings
            )

        assert result["llm"] == "disconnected"

    def test_collect_database_vitals_web_search_assignment(self, mock_db, mock_settings) -> None:
        """Test web search assignment logic."""
        # Query 0: SET LOCAL statement_timeout
        # Query 1: chat assignment returns None
        # Query 2: chat config returns None
        # Query 3: web_search assignment returns assignment
        # Query 4: web_search config skipped
        # Query 5: sources count returns 0
        # Query 6: connector statuses returns empty
        execute_results = [
            self._make_timeout_result(),  # query 0: SET LOCAL
            self._make_result(first_return=None),  # query 1: chat assignment
            self._make_result(first_return=None),  # query 2: chat config
            self._make_result(first_return=MagicMock()),  # query 3: web_search assignment
            self._make_result(scalar_return=0),  # query 5: sources count
            self._make_result(iter_return=[]),  # query 6: connector statuses
        ]
        self._setup_db_mock(mock_db, execute_results)

        with patch(
            "app.system.services.vitals_service.get_session_factory", return_value=lambda: mock_db
        ):
            result = vitals_service_module.VitalsService._collect_database_vitals(
                "test-tenant", mock_settings
            )

        assert result["web_search"] == "available"

    def test_collect_database_vitals_web_search_config(self, mock_db, mock_settings) -> None:
        """Test web search config fallback."""
        # Query 0: SET LOCAL statement_timeout
        # Query 1: chat assignment returns None
        # Query 2: chat config returns None
        # Query 3: web_search assignment returns None
        # Query 4: web_search config returns config
        # Query 5: sources count returns 0
        # Query 6: connector statuses returns empty
        execute_results = [
            self._make_timeout_result(),  # query 0: SET LOCAL
            self._make_result(first_return=None),  # query 1: chat assignment
            self._make_result(first_return=None),  # query 2: chat config
            self._make_result(first_return=None),  # query 3: web_search assignment
            self._make_result(first_return=MagicMock()),  # query 4: web_search config
            self._make_result(scalar_return=0),  # query 5: sources count
            self._make_result(iter_return=[]),  # query 6: connector statuses
        ]
        self._setup_db_mock(mock_db, execute_results)

        with patch(
            "app.system.services.vitals_service.get_session_factory", return_value=lambda: mock_db
        ):
            result = vitals_service_module.VitalsService._collect_database_vitals(
                "test-tenant", mock_settings
            )

        assert result["web_search"] == "available"

    def test_collect_database_vitals_web_search_perplexity(self, mock_db, mock_settings) -> None:
        """Test web search with perplexity provider."""
        # Query 0: SET LOCAL statement_timeout
        # Query 1: chat assignment returns None
        # Query 2: chat config returns None
        # Query 3: web_search assignment returns None
        # Query 4: web_search config returns None
        # Query 5: sources count returns 0
        # Query 6: connector statuses returns empty
        # But settings.llm_provider == "perplexity" so web_search should be available
        execute_results = [
            self._make_timeout_result(),  # query 0: SET LOCAL
            self._make_result(first_return=None),  # query 1: chat assignment
            self._make_result(first_return=None),  # query 2: chat config
            self._make_result(first_return=None),  # query 3: web_search assignment
            self._make_result(first_return=None),  # query 4: web_search config
            self._make_result(scalar_return=0),  # query 5: sources count
            self._make_result(iter_return=[]),  # query 6: connector statuses
        ]
        self._setup_db_mock(mock_db, execute_results)

        mock_settings.llm_provider = "perplexity"

        with patch(
            "app.system.services.vitals_service.get_session_factory", return_value=lambda: mock_db
        ):
            result = vitals_service_module.VitalsService._collect_database_vitals(
                "test-tenant", mock_settings
            )

        assert result["web_search"] == "available"

    def test_collect_database_vitals_sources_count(self, mock_db) -> None:
        """Test sources count logic."""
        # Query 0: SET LOCAL statement_timeout
        # Query 1: chat assignment returns None
        # Query 2: chat config returns None
        # Query 3: web_search assignment returns None
        # Query 4: web_search config returns None
        # Query 5: sources count returns 5
        # Query 6: connector statuses returns empty
        execute_results = [
            self._make_timeout_result(),  # query 0: SET LOCAL
            self._make_result(first_return=None),  # query 1: chat assignment
            self._make_result(first_return=None),  # query 2: chat config
            self._make_result(first_return=None),  # query 3: web_search assignment
            self._make_result(first_return=None),  # query 4: web_search config
            self._make_result(scalar_return=5),  # query 5: sources count
            self._make_result(iter_return=[]),  # query 6: connector statuses
        ]
        self._setup_db_mock(mock_db, execute_results)

        with patch(
            "app.system.services.vitals_service.get_session_factory", return_value=lambda: mock_db
        ):
            result = vitals_service_module.VitalsService._collect_database_vitals(
                "test-tenant", MagicMock()
            )

        assert result["sources"] == 5

    def test_collect_database_vitals_connector_statuses(self, mock_db) -> None:
        """Test connector statuses aggregation."""
        # Query 0: SET LOCAL statement_timeout
        # Query 1: chat assignment returns None
        # Query 2: chat config returns None
        # Query 3: web_search assignment returns None
        # Query 4: web_search config returns None
        # Query 5: sources count returns 0
        # Query 6: connector statuses returns ACTIVE: 3
        mock_status = MagicMock()
        mock_status.value = "ACTIVE"
        execute_results = [
            self._make_timeout_result(),  # query 0: SET LOCAL
            self._make_result(first_return=None),  # query 1: chat assignment
            self._make_result(first_return=None),  # query 2: chat config
            self._make_result(first_return=None),  # query 3: web_search assignment
            self._make_result(first_return=None),  # query 4: web_search config
            self._make_result(scalar_return=0),  # query 5: sources count
            self._make_result(iter_return=[(mock_status, 3)]),  # query 6: connector statuses
        ]
        self._setup_db_mock(mock_db, execute_results)

        with patch(
            "app.system.services.vitals_service.get_session_factory", return_value=lambda: mock_db
        ):
            result = vitals_service_module.VitalsService._collect_database_vitals(
                "test-tenant", MagicMock()
            )

        assert result["connector_statuses"] == {"ACTIVE": 3}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
