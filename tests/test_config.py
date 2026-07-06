"""Tests for app.config — URL resolution and environment handling."""
import os
import pytest
from unittest.mock import patch


class TestNormalisePgUrl:
    def test_strips_asyncpg_driver(self):
        from app.config import normalise_pg_url
        url = "postgresql+asyncpg://user:pass@host:5432/db"
        result = normalise_pg_url(url)
        assert result.startswith("postgresql://user:pass@host:5432/db")

    def test_strips_psycopg_driver(self):
        from app.config import normalise_pg_url
        url = "postgresql+psycopg://user:pass@host:5432/db"
        result = normalise_pg_url(url)
        assert result.startswith("postgresql://user:pass@host:5432/db")

    def test_adds_connect_timeout(self):
        from app.config import normalise_pg_url
        url = "postgresql://user:pass@host:5432/db"
        result = normalise_pg_url(url)
        assert "connect_timeout=10" in result

    def test_preserves_existing_connect_timeout(self):
        from app.config import normalise_pg_url
        url = "postgresql://user:pass@host:5432/db?connect_timeout=5"
        result = normalise_pg_url(url)
        assert "connect_timeout=5" in result
        # Should not add a second one
        assert result.count("connect_timeout") == 1

    @patch.dict(os.environ, {"USE_CLOUD_SQL_PROXY": "1"})
    def test_cloud_sql_proxy_rewrite(self):
        from app.config import normalise_pg_url
        url = "postgresql://user:pass@34.135.72.145:5432/db"
        result = normalise_pg_url(url)
        assert "127.0.0.1:5433" in result
        assert "34.135.72.145" not in result

    @patch.dict(os.environ, {"USE_CLOUD_SQL_PROXY": "0"})
    def test_no_proxy_rewrite_when_disabled(self):
        from app.config import normalise_pg_url
        url = "postgresql://user:pass@34.135.72.145:5432/db"
        result = normalise_pg_url(url)
        assert "34.135.72.145" in result

    def test_empty_url_returns_empty(self):
        from app.config import normalise_pg_url
        assert normalise_pg_url("") == ""


class TestDeriveUrl:
    def test_swaps_database_name(self):
        from app.config import _derive_url
        base = "postgresql://user:pass@host:5432/mobius_chat"
        result = _derive_url(base, "mobius_user")
        assert result.endswith("/mobius_user")
        assert "user:pass@host:5432" in result

    def test_empty_base_returns_empty(self):
        from app.config import _derive_url
        assert _derive_url("", "mobius_user") == ""


class TestDbAgentConfig:
    @patch.dict(os.environ, {
        "CHAT_RAG_DATABASE_URL": "postgresql://u:p@host:5432/mobius_chat",
        "DATABASE_URL": "postgresql+asyncpg://u:p@host:5432/mobius_rag",
        "USE_CLOUD_SQL_PROXY": "0",
    }, clear=False)
    def test_from_env_resolves_all_databases(self):
        from app.config import DbAgentConfig
        cfg = DbAgentConfig.from_env()
        assert "chat" in cfg.db_urls
        assert "rag" in cfg.db_urls
        assert "user" in cfg.db_urls  # derived from chat URL
        # rag URL should have asyncpg stripped
        assert "+asyncpg" not in cfg.db_urls["rag"]

    @patch.dict(os.environ, {
        "DB_AGENT_POOL_CHAT_MAX": "8",
        "DB_AGENT_POOL_TOTAL_MAX": "20",
        "CHAT_RAG_DATABASE_URL": "postgresql://u:p@host:5432/mobius_chat",
        "USE_CLOUD_SQL_PROXY": "0",
    }, clear=False)
    def test_pool_config_from_env(self):
        from app.config import DbAgentConfig
        cfg = DbAgentConfig.from_env()
        assert cfg.pool_configs["chat"].max_size == 8
        assert cfg.pool_total_max == 20
