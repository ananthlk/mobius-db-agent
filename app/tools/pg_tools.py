"""PostgreSQL MCP tools: db_query, db_execute, db_get_schema."""
import json
import logging
import re

from sqlalchemy import text

from app.server import mcp, pool_manager, access_control, contract_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_WRITE_RE = re.compile(
    r"^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?(\w+)\"?",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    return _COMMENT_RE.sub("", sql).strip()


def _is_read_only(sql: str) -> bool:
    cleaned = _strip_comments(sql)
    first_word = cleaned.split()[0].upper() if cleaned else ""
    return first_word in ("SELECT", "WITH", "EXPLAIN")


def _extract_write_target(sql: str) -> tuple[str, str] | None:
    """Return (operation, table) from a write SQL statement, or None."""
    cleaned = _strip_comments(sql)
    m = _WRITE_RE.match(cleaned)
    if not m:
        return None
    op = m.group(1).split()[0].upper()  # INSERT, UPDATE, or DELETE
    table = m.group(2)
    return op, table


def _extract_read_tables(sql: str) -> list[str]:
    """Best-effort extraction of table names from a SELECT statement."""
    cleaned = _strip_comments(sql)
    # Extract CTE aliases so we can exclude them
    cte_aliases = set(re.findall(r"(\w+)\s+AS\s*\(", cleaned, re.IGNORECASE))
    # Match FROM <table> and JOIN <table>
    tables = re.findall(r"(?:FROM|JOIN)\s+\"?(\w+)\"?", cleaned, re.IGNORECASE)
    # Exclude CTE aliases and common keywords
    tables = [t for t in tables if t not in cte_aliases and t.upper() not in ("SELECT", "LATERAL")]
    return list(dict.fromkeys(tables))  # deduplicate, preserve order


def _json_result(data: dict) -> str:
    return json.dumps(data, default=str)


def _json_error(error: str) -> str:
    return json.dumps({"error": error})


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def db_query(
    sql: str,
    db_name: str,
    caller_id: str,
    params: str = "{}",
    max_rows: int = 1000,
) -> str:
    """Execute a read-only SQL query against a PostgreSQL database.

    Args:
        sql: SELECT/WITH/EXPLAIN SQL statement.
        db_name: Database name (chat, rag, user, qa).
        caller_id: Service identifier for access control.
        params: JSON object of query parameters (optional).
        max_rows: Maximum rows to return (default 1000).
    """
    if not sql or not sql.strip():
        return _json_error("sql is required")
    if not db_name:
        return _json_error("db_name is required")
    if not caller_id:
        return _json_error("caller_id is required")

    if db_name not in pool_manager.available_databases():
        return _json_error(f"Unknown database: {db_name}")

    if not _is_read_only(sql):
        return _json_error("db_query only accepts SELECT/WITH/EXPLAIN statements. Use db_execute for writes.")

    # Access control
    tables = _extract_read_tables(sql)
    for table in tables:
        if not access_control.check_read(caller_id, db_name, table):
            return _json_error(f"Access denied: {caller_id} cannot read {db_name}.{table}")

    # Enforce limits
    limits = access_control.get_limits(caller_id)
    effective_max = min(max_rows, limits.max_rows)

    try:
        parsed_params = json.loads(params) if isinstance(params, str) else params
    except json.JSONDecodeError:
        return _json_error("Invalid JSON in params")

    try:
        with pool_manager.get_connection(db_name) as conn:
            # Set statement timeout
            conn.execute(text(f"SET LOCAL statement_timeout = '{limits.timeout_seconds * 1000}'"))
            result = conn.execute(text(sql), parsed_params)
            columns = list(result.keys())
            rows = [list(row) for row in result.fetchmany(effective_max + 1)]
            truncated = len(rows) > effective_max
            if truncated:
                rows = rows[:effective_max]
            conn.rollback()  # ensure read-only
            return _json_result({
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
            })
    except Exception as exc:
        logger.exception("db_query failed for %s", db_name)
        return _json_error(f"Query failed: {exc}")


@mcp.tool()
def db_execute(
    sql: str,
    db_name: str,
    caller_id: str,
    params: str = "{}",
) -> str:
    """Execute a write SQL statement (INSERT/UPDATE/DELETE) against a PostgreSQL database.

    Args:
        sql: INSERT, UPDATE, or DELETE SQL statement.
        db_name: Database name (chat, rag, user, qa).
        caller_id: Service identifier for access control.
        params: JSON object of query parameters (optional).
    """
    if not sql or not sql.strip():
        return _json_error("sql is required")
    if not db_name:
        return _json_error("db_name is required")
    if not caller_id:
        return _json_error("caller_id is required")

    if db_name not in pool_manager.available_databases():
        return _json_error(f"Unknown database: {db_name}")

    target = _extract_write_target(sql)
    if not target:
        return _json_error(
            "db_execute only accepts INSERT INTO/UPDATE/DELETE FROM statements. "
            "Use db_query for reads. DDL (CREATE/ALTER/DROP) is not allowed."
        )

    operation, table = target

    # Access control
    if not access_control.check_write(caller_id, db_name, table):
        return _json_error(f"Access denied: {caller_id} cannot write to {db_name}.{table}")

    # Contract validation for INSERT/UPDATE
    if operation in ("INSERT", "UPDATE"):
        # Extract column names from SQL for validation
        columns = _extract_write_columns(sql, operation)
        if columns:
            errors = contract_validator.validate_write(db_name, table, columns, pool_manager)
            if errors:
                return _json_error(f"Contract validation failed: {'; '.join(errors)}")

    try:
        parsed_params = json.loads(params) if isinstance(params, str) else params
    except json.JSONDecodeError:
        return _json_error("Invalid JSON in params")

    try:
        with pool_manager.get_connection(db_name) as conn:
            result = conn.execute(text(sql), parsed_params)
            rows_affected = result.rowcount
            conn.commit()
            return _json_result({
                "operation": operation,
                "table": table,
                "rows_affected": rows_affected,
            })
    except Exception as exc:
        logger.exception("db_execute failed for %s.%s", db_name, table)
        return _json_error(f"Execute failed: {exc}")


@mcp.tool()
def db_get_schema(
    db_name: str,
    caller_id: str,
    table: str = "",
) -> str:
    """Get schema information for a database or specific table.

    Args:
        db_name: Database name (chat, rag, user, qa).
        caller_id: Service identifier for access control.
        table: Table name (optional). If empty, lists all readable tables.
    """
    if not db_name:
        return _json_error("db_name is required")
    if not caller_id:
        return _json_error("caller_id is required")

    if db_name not in pool_manager.available_databases():
        return _json_error(f"Unknown database: {db_name}")

    try:
        with pool_manager.get_connection(db_name) as conn:
            if not table:
                # List all tables, filtered by access control
                rows = conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                        "ORDER BY table_name"
                    )
                ).fetchall()
                all_tables = [row[0] for row in rows]
                readable = [
                    t for t in all_tables
                    if access_control.check_read(caller_id, db_name, t)
                ]
                conn.rollback()
                return _json_result({"database": db_name, "tables": readable})
            else:
                if not access_control.check_read(caller_id, db_name, table):
                    return _json_error(f"Access denied: {caller_id} cannot read {db_name}.{table}")
                rows = conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :table "
                        "ORDER BY ordinal_position"
                    ),
                    {"table": table},
                ).fetchall()
                columns = [
                    {
                        "name": r[0],
                        "type": r[1],
                        "nullable": r[2] == "YES",
                        "default": r[3],
                    }
                    for r in rows
                ]
                conn.rollback()
                if not columns:
                    return _json_error(f"Table '{table}' not found in {db_name}")
                return _json_result({"database": db_name, "table": table, "columns": columns})
    except Exception as exc:
        logger.exception("db_get_schema failed for %s", db_name)
        return _json_error(f"Schema query failed: {exc}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_write_columns(sql: str, operation: str) -> list[str]:
    """Best-effort extraction of column names from INSERT/UPDATE SQL."""
    cleaned = _strip_comments(sql)
    if operation == "INSERT":
        # INSERT INTO table (col1, col2, ...) VALUES ...
        m = re.search(r"\(\s*([^)]+)\s*\)\s*VALUES", cleaned, re.IGNORECASE)
        if m:
            return [c.strip().strip('"') for c in m.group(1).split(",")]
    elif operation == "UPDATE":
        # UPDATE table SET col1 = ..., col2 = ...
        m = re.search(r"SET\s+(.+?)(?:\s+WHERE|\s*$)", cleaned, re.IGNORECASE | re.DOTALL)
        if m:
            assignments = m.group(1).split(",")
            cols = []
            for a in assignments:
                eq = a.split("=", 1)
                if eq:
                    cols.append(eq[0].strip().strip('"'))
            return cols
    return []
