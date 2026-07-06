"""Centralized connection pool manager — one SQLAlchemy Engine per database."""
import logging
from dataclasses import dataclass
from typing import Generator

from contextlib import contextmanager
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from app.config import DbAgentConfig, PoolConfig

logger = logging.getLogger(__name__)


@dataclass
class PoolMetrics:
    pool_size: int
    checked_out: int
    checked_in: int
    overflow: int
    reachable: bool


class PoolManager:
    """Manages one SQLAlchemy sync engine per database with shared pool limits."""

    def __init__(self, config: DbAgentConfig) -> None:
        self._config = config
        self._engines: dict[str, Engine] = {}

    # ------------------------------------------------------------------
    # Engine lifecycle
    # ------------------------------------------------------------------

    def _create_engine(self, db_name: str) -> Engine:
        url = self._config.db_urls.get(db_name)
        if not url:
            raise ValueError(f"No URL configured for database '{db_name}'")

        pool_cfg: PoolConfig = self._config.pool_configs.get(
            db_name, PoolConfig()
        )

        engine = create_engine(
            url,
            pool_size=pool_cfg.min_size,
            max_overflow=pool_cfg.max_size - pool_cfg.min_size,
            pool_pre_ping=pool_cfg.pool_pre_ping,
            pool_recycle=pool_cfg.pool_recycle,
            pool_reset_on_return="rollback",
            pool_timeout=10,
        )

        # Execute ROLLBACK on checkout to ensure clean state
        # (pattern from mobius-os/backend/app/db/postgres.py)
        @event.listens_for(engine, "checkout")
        def _on_checkout(dbapi_conn, connection_record, connection_proxy):
            try:
                dbapi_conn.rollback()
            except Exception:
                pass

        logger.info(
            "Created engine for %s (pool_size=%d, max_overflow=%d)",
            db_name, pool_cfg.min_size, pool_cfg.max_size - pool_cfg.min_size,
        )
        return engine

    def _get_engine(self, db_name: str) -> Engine:
        if db_name not in self._engines:
            self._engines[db_name] = self._create_engine(db_name)
        return self._engines[db_name]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @contextmanager
    def get_connection(self, db_name: str) -> Generator[Connection, None, None]:
        """Yield a connection from the named database's pool."""
        engine = self._get_engine(db_name)
        with engine.connect() as conn:
            yield conn

    def available_databases(self) -> list[str]:
        """Return names of databases that have URLs configured."""
        return list(self._config.db_urls.keys())

    def health_check(self) -> dict[str, bool]:
        """Run SELECT 1 on each configured database. Returns {db_name: reachable}."""
        results = {}
        for db_name in self._config.db_urls:
            try:
                engine = self._get_engine(db_name)
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                results[db_name] = True
            except Exception as exc:
                logger.warning("Health check failed for %s: %s", db_name, exc)
                results[db_name] = False
        return results

    def get_metrics(self) -> dict[str, PoolMetrics]:
        """Return pool utilisation metrics for each initialised engine."""
        metrics = {}
        for db_name, engine in self._engines.items():
            pool = engine.pool
            try:
                reachable = True
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            except Exception:
                reachable = False
            metrics[db_name] = PoolMetrics(
                pool_size=pool.size(),
                checked_out=pool.checkedout(),
                checked_in=pool.checkedin(),
                overflow=pool.overflow(),
                reachable=reachable,
            )
        return metrics

    def shutdown(self) -> None:
        """Dispose all engines and release connections."""
        for db_name, engine in self._engines.items():
            logger.info("Disposing engine for %s", db_name)
            engine.dispose()
        self._engines.clear()
