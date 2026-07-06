"""Tests for app.contracts — schema introspection and write validation."""
import time
from unittest.mock import MagicMock, patch

import pytest

from app.contracts import ContractValidator, TableSchema, ColumnInfo, CACHE_TTL_SECONDS


@pytest.fixture
def validator():
    return ContractValidator()


@pytest.fixture
def mock_pool_manager():
    """Create a mock PoolManager that returns predefined schema rows."""
    pm = MagicMock()
    conn = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = [
        ("id", "uuid", "NO", None),
        ("name", "character varying", "YES", None),
        ("status", "character varying", "NO", "'active'"),
        ("created_at", "timestamp without time zone", "NO", "now()"),
    ]
    conn.execute.return_value = result
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pm.get_connection.return_value = conn
    return pm


class TestContractValidator:
    def test_validate_existing_columns(self, validator, mock_pool_manager):
        errors = validator.validate_write("chat", "tasks", ["id", "name", "status"], mock_pool_manager)
        assert errors == []

    def test_validate_unknown_column(self, validator, mock_pool_manager):
        errors = validator.validate_write("chat", "tasks", ["id", "nonexistent"], mock_pool_manager)
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_validate_multiple_unknown_columns(self, validator, mock_pool_manager):
        errors = validator.validate_write("chat", "tasks", ["bad1", "bad2"], mock_pool_manager)
        assert len(errors) == 2

    def test_cache_is_used(self, validator, mock_pool_manager):
        # First call fetches from DB
        validator.validate_write("chat", "tasks", ["id"], mock_pool_manager)
        call_count_1 = mock_pool_manager.get_connection.call_count

        # Second call should use cache
        validator.validate_write("chat", "tasks", ["name"], mock_pool_manager)
        call_count_2 = mock_pool_manager.get_connection.call_count

        assert call_count_2 == call_count_1  # no additional DB call

    def test_stale_cache_refetches(self, validator, mock_pool_manager):
        validator.validate_write("chat", "tasks", ["id"], mock_pool_manager)

        # Manually expire the cache
        key = validator._cache_key("chat", "tasks")
        validator._cache[key].fetched_at = time.time() - CACHE_TTL_SECONDS - 1

        validator.validate_write("chat", "tasks", ["id"], mock_pool_manager)
        assert mock_pool_manager.get_connection.call_count == 2  # refetched

    def test_invalidate_clears_cache(self, validator, mock_pool_manager):
        validator.validate_write("chat", "tasks", ["id"], mock_pool_manager)
        assert validator._cache_key("chat", "tasks") in validator._cache

        validator.invalidate("chat", "tasks")
        assert validator._cache_key("chat", "tasks") not in validator._cache

    def test_fail_open_when_schema_unavailable(self, validator):
        """If we can't fetch schema, allow the write (Phase 1 fail-open)."""
        broken_pm = MagicMock()
        conn = MagicMock()
        conn.execute.side_effect = Exception("connection refused")
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        broken_pm.get_connection.return_value = conn

        errors = validator.validate_write("chat", "tasks", ["anything"], broken_pm)
        assert errors == []  # fail-open

    def test_get_table_schema_returns_columns(self, validator, mock_pool_manager):
        schema = validator.get_table_schema("chat", "tasks", mock_pool_manager)
        assert "id" in schema.columns
        assert schema.columns["id"].data_type == "uuid"
        assert schema.columns["name"].is_nullable is True
        assert schema.columns["status"].column_default == "'active'"
