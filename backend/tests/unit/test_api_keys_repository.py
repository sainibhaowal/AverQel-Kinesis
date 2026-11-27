"""Tests for the API keys repository (DB-independent parts)."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.auth.models.api_key import ApiKey
from app.auth.repositories.api_keys import ApiKeysRepository


class TestHashKey:
    """Tests for the hash_key static method."""

    def test_hash_key_produces_sha256(self) -> None:
        raw_key = "test-key"
        expected = hashlib.sha256(raw_key.encode()).hexdigest()
        result = ApiKeysRepository.hash_key(raw_key)
        assert result == expected

    def test_hash_key_empty_string(self) -> None:
        raw_key = ""
        expected = hashlib.sha256(raw_key.encode()).hexdigest()
        result = ApiKeysRepository.hash_key(raw_key)
        assert result == expected

    def test_hash_key_unicode(self) -> None:
        raw_key = "key-with-unicode-émojis-🔑"
        expected = hashlib.sha256(raw_key.encode()).hexdigest()
        result = ApiKeysRepository.hash_key(raw_key)
        assert result == expected

    def test_hash_key_consistent(self) -> None:
        raw_key = "consistent-key"
        result1 = ApiKeysRepository.hash_key(raw_key)
        result2 = ApiKeysRepository.hash_key(raw_key)
        assert result1 == result2

    def test_hash_key_different_inputs(self) -> None:
        result1 = ApiKeysRepository.hash_key("key1")
        result2 = ApiKeysRepository.hash_key("key2")
        assert result1 != result2


class TestApiKeysRepositoryWithMockDB:
    """Tests for ApiKeysRepository using mock DB session."""

    def setup_method(self) -> None:
        self.mock_db = MagicMock()
        self.repo = ApiKeysRepository(self.mock_db)

    def test_init(self) -> None:
        assert self.repo.db is self.mock_db

    def test_create(self) -> None:
        api_key = MagicMock(spec=ApiKey)
        api_key.tenant_id = uuid.uuid4()

        with patch.object(self.repo, "apply_tenant_scope"):
            result = self.repo.create(api_key)

        assert result is api_key
        self.mock_db.add.assert_called_once_with(api_key)
        self.mock_db.flush.assert_called_once()

    def test_create_applies_tenant_scope(self) -> None:
        api_key = MagicMock(spec=ApiKey)
        api_key.tenant_id = uuid.uuid4()

        with patch.object(self.repo, "apply_tenant_scope") as mock_scope:
            self.repo.create(api_key)
            mock_scope.assert_called_once_with(api_key.tenant_id)

    def test_get_by_hash_found(self) -> None:
        mock_api_key = MagicMock(spec=ApiKey)
        self.mock_db.execute.return_value.scalar_one_or_none.return_value = mock_api_key

        result = self.repo.get_by_hash(key_hash="test_hash")

        assert result is mock_api_key
        self.mock_db.execute.assert_called_once()

    def test_get_by_hash_not_found(self) -> None:
        self.mock_db.execute.return_value.scalar_one_or_none.return_value = None

        result = self.repo.get_by_hash(key_hash="nonexistent_hash")

        assert result is None

    def test_get_by_hash_does_not_apply_tenant_scope(self) -> None:
        # get_by_hash intentionally doesn't apply tenant scope
        self.mock_db.execute.return_value.scalar_one_or_none.return_value = None

        with patch.object(self.repo, "apply_tenant_scope") as mock_scope:
            self.repo.get_by_hash(key_hash="test_hash")
            mock_scope.assert_not_called()

    def test_update_last_used(self) -> None:
        key_id = uuid.uuid4()

        self.repo.update_last_used(key_id=key_id)

        self.mock_db.execute.assert_called_once()

    def test_list_by_tenant(self) -> None:
        mock_api_keys = [MagicMock(spec=ApiKey), MagicMock(spec=ApiKey)]
        self.mock_db.execute.return_value.scalars.return_value.all.return_value = mock_api_keys

        tenant_id = uuid.uuid4()
        result = self.repo.list_by_tenant(tenant_id=tenant_id)

        assert result == mock_api_keys
        # execute is called twice: once for apply_tenant_scope, once for the query
        assert self.mock_db.execute.call_count == 2

    def test_list_by_tenant_applies_tenant_scope(self) -> None:
        tenant_id = uuid.uuid4()
        self.mock_db.execute.return_value.scalars.return_value.all.return_value = []

        with patch.object(self.repo, "apply_tenant_scope") as mock_scope:
            self.repo.list_by_tenant(tenant_id=tenant_id)
            mock_scope.assert_called_once_with(tenant_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
