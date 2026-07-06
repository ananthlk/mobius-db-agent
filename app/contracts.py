"""Schema contract validation — introspects PG information_schema before writes."""
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    is_nullable: bool
    column_default: str | None = None


@dataclass
class TableSchema:
    columns: dict[str, ColumnInfo] = field(default_factory=dict)
    fetched_at: float = 0.0

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.fetched_at) > CACHE_TTL_SECONDS


class ContractValidator:
    """Validates write operations against actual PG table schemas."""

    def __init__(self, dbt_project_root: Path | None = None) -> None:
        self._cache: dict[str, TableSchema] = {}  # key: "db_name.table"
        self._dbt_root = dbt_project_root

    def _cache_key(self, db_name: str, table: str) -> str:
        return f"{db_name}.{table}"

    def _fetch_pg_schema(self, db_name: str, table: str, pool_manager) -> TableSchema:
        """Introspect information_schema.columns for the given table."""
        from app.pools import PoolManager
        pm: PoolManager = pool_manager

        schema = TableSchema()
        try:
            with pm.get_connection(db_name) as conn:
                rows = conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :table "
                        "ORDER BY ordinal_position"
                    ),
                    {"table": table},
                ).fetchall()
                for row in rows:
                    col = ColumnInfo(
                        name=row[0],
                        data_type=row[1],
                        is_nullable=row[2] == "YES",
                        column_default=row[3],
                    )
                    schema.columns[col.name] = col
                schema.fetched_at = time.time()
        except Exception as exc:
            logger.warning("Failed to fetch schema for %s.%s: %s", db_name, table, exc)
        return schema

    def get_table_schema(self, db_name: str, table: str, pool_manager) -> TableSchema:
        """Get cached or fresh table schema."""
        key = self._cache_key(db_name, table)
        cached = self._cache.get(key)
        if cached and not cached.is_stale:
            return cached
        schema = self._fetch_pg_schema(db_name, table, pool_manager)
        if schema.columns:
            self._cache[key] = schema
        return schema

    def validate_write(
        self, db_name: str, table: str, columns: list[str], pool_manager
    ) -> list[str]:
        """Validate that all columns exist in the target table.

        Returns a list of error strings (empty means valid).
        """
        schema = self.get_table_schema(db_name, table, pool_manager)
        if not schema.columns:
            # Could not fetch schema — allow the write (fail-open in Phase 1)
            logger.warning(
                "No schema cached for %s.%s — skipping contract validation", db_name, table
            )
            return []

        errors = []
        for col in columns:
            if col not in schema.columns:
                errors.append(f"Column '{col}' does not exist in {db_name}.{table}")
        return errors

    def invalidate(self, db_name: str, table: str) -> None:
        """Remove cached schema for a table."""
        key = self._cache_key(db_name, table)
        self._cache.pop(key, None)
