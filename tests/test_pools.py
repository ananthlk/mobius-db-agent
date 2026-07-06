"""Tests for app.pools — connection pool manager."""
from unittest.mock import MagicMock, patch

import pytest

from app.config import DbAgentConfig, PoolConfig
from app.pools import PoolManager


@pytest.fixture
def config():
    return DbAgentConfig(
        db_urls={"testdb": "postgresql://user:pass@localhost:5432/testdb"},
        pool_configs={"testdb": PoolConfig(min_size=1, max_size=3)},
    )


class TestPoolManager:
    def test_available_databases(self, config):
        pm = PoolManager(config)
        assert pm.available_databases() == ["testdb"]

    def test_no_databases_configured(self):
        pm = PoolManager(DbAgentConfig())
        assert pm.available_databases() == []

    def test_get_connection_unknown_db_raises(self, config):
        pm = PoolManager(config)
        with pytest.raises(ValueError, match="No URL configured"):
            with pm.get_connection("nonexistent"):
                pass

    def test_engine_created_lazily(self, config):
        pm = PoolManager(config)
        assert pm._engines == {}
        # Patch _create_engine to avoid real DB connection
        mock_engine = MagicMock()
        with patch.object(pm, "_create_engine", return_value=mock_engine) as mock_create:
            pm._get_engine("testdb")
            assert mock_create.call_count == 1
            # Second call reuses cached engine
            pm._get_engine("testdb")
            assert mock_create.call_count == 1

    def test_shutdown_disposes_engines(self, config):
        pm = PoolManager(config)
        mock_engine = MagicMock()
        pm._engines["testdb"] = mock_engine
        pm.shutdown()
        mock_engine.dispose.assert_called_once()
        assert pm._engines == {}

    def test_health_check_returns_dict(self, config):
        pm = PoolManager(config)
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn
        # Inject engine directly, bypassing _create_engine
        pm._engines["testdb"] = mock_engine

        result = pm.health_check()
        assert result == {"testdb": True}

    def test_health_check_catches_failures(self, config):
        pm = PoolManager(config)
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = Exception("connection refused")
        pm._engines["testdb"] = mock_engine

        result = pm.health_check()
        assert result == {"testdb": False}

    def test_create_engine_settings(self, config):
        """Verify _create_engine passes correct pool settings (requires real SQLAlchemy)."""
        pm = PoolManager(config)
        with patch("app.pools.create_engine") as mock_ce:
            # Return a real-enough mock that supports event listeners
            from sqlalchemy import create_engine as real_ce
            real_engine = real_ce("sqlite:///:memory:")
            mock_ce.return_value = real_engine
            try:
                engine = pm._create_engine("testdb")
                call_kwargs = mock_ce.call_args[1]
                assert call_kwargs["pool_size"] == 1
                assert call_kwargs["max_overflow"] == 2
                assert call_kwargs["pool_pre_ping"] is True
                assert call_kwargs["pool_recycle"] == 300
                assert call_kwargs["pool_reset_on_return"] == "rollback"
            finally:
                real_engine.dispose()
