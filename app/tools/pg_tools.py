"""PostgreSQL MCP tools: db_query, db_execute, db_get_schema.

All errors returned by these tools follow the structured shape:
    {"error": {"code": "<code>", "message": "<text>", ...extras}}

See app/errors.py and docs/db-agent-contract.md § "Error Model".
"""
import json
import logging
import re

from sqlalchemy import text

from app.errors import classify_db_exception, make_error
from app.server import access_control, contract_validator, mcp, pool_manager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_WRITE_RE = re.compile(
    r"^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?(\w+)\"?",
    re.IGNORECASE,
)
_DDL_RE = re.compile(
    r"^\s*(CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    return _COMMENT_RE.sub("", sql).strip()


def _is_read_only(sql: str) -> bool:
    cleaned = _strip_comments(sql)
    first_word = cleaned.split()[0].upper() if cleaned else ""
    return first_word in ("SELECT", "WITH", "EXPLAIN")


def _is_ddl(sql: str) -> bool:
    return bool(_DDL_RE.match(_strip_comments(sql)))


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
    cte_aliases = set(re.findall(r"(\w+)\s+AS\s*\(", cleaned, re.IGNORECASE))
    tables = re.findall(r"(?:FROM|JOIN)\s+\"?(\w+)\"?", cleaned, re.IGNORECASE)
    tables = [t for t in tables if t not in cte_aliases and t.upper() not in ("SELECT", "LATERAL")]
    return list(dict.fromkeys(tables))


def _json(data: dict) -> str:
    return json.dumps(data, default=str)


def _err(code: str, message: str, **extra) -> str:
    return _json(make_error(code, message, **extra))


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
        return _err("invalid_input", "sql is required")
    if not db_name:
        return _err("invalid_input", "db_name is required")
    if not caller_id:
        return _err("invalid_input", "caller_id is required")

    if db_name not in pool_manager.available_databases():
        return _err("invalid_input", f"Unknown database: {db_name}")

    if _is_ddl(sql):
        return _err(
            "ddl_forbidden",
            "DDL (CREATE/ALTER/DROP/TRUNCATE/GRANT/REVOKE) is not allowed",
        )
    if not _is_read_only(sql):
        return _err(
            "readonly_violation",
            "db_query only accepts SELECT/WITH/EXPLAIN. Use db_execute for writes.",
        )

    tables = _extract_read_tables(sql)
    for table in tables:
        if not access_control.check_read(caller_id, db_name, table):
            return _err(
                "access_denied",
                f"{caller_id} cannot read {db_name}.{table}",
                table=table,
                database=db_name,
            )

    limits = access_control.get_limits(caller_id)
    effective_max = min(max_rows, limits.max_rows)

    try:
        parsed_params = json.loads(params) if isinstance(params, str) else params
    except json.JSONDecodeError:
        return _err("invalid_input", "params is not valid JSON")

    try:
        with pool_manager.get_connection(db_name) as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = '{limits.timeout_seconds * 1000}'"))
            result = conn.execute(text(sql), parsed_params)
            columns = list(result.keys())
            rows = [list(row) for row in result.fetchmany(effective_max + 1)]
            truncated = len(rows) > effective_max
            if truncated:
                rows = rows[:effective_max]
            conn.rollback()
            payload = {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
            }
            return _json(payload)
    except Exception as exc:
        code, extras = classify_db_exception(exc)
        logger.warning("db_query failed for %s: code=%s msg=%s", db_name, code, exc)
        return _err(code, str(exc), **extras)


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
        return _err("invalid_input", "sql is required")
    if not db_name:
        return _err("invalid_input", "db_name is required")
    if not caller_id:
        return _err("invalid_input", "caller_id is required")

    if db_name not in pool_manager.available_databases():
        return _err("invalid_input", f"Unknown database: {db_name}")

    if _is_ddl(sql):
        return _err(
            "ddl_forbidden",
            "DDL (CREATE/ALTER/DROP/TRUNCATE/GRANT/REVOKE) is not allowed",
        )

    target = _extract_write_target(sql)
    if not target:
        # Reject SELECT here with readonly_violation, everything else as invalid.
        if _is_read_only(sql):
            return _err(
                "readonly_violation",
                "db_execute does not accept SELECT. Use db_query for reads.",
            )
        return _err(
            "invalid_input",
            "db_execute only accepts INSERT INTO / UPDATE / DELETE FROM statements.",
        )

    operation, table = target

    if not access_control.check_write(caller_id, db_name, table):
        return _err(
            "access_denied",
            f"{caller_id} cannot write to {db_name}.{table}",
            table=table,
            database=db_name,
        )

    if operation in ("INSERT", "UPDATE"):
        columns = _extract_write_columns(sql, operation)
        if columns:
            errors = contract_validator.validate_write(db_name, table, columns, pool_manager)
            if errors:
                return _err(
                    "column_missing",
                    "; ".join(errors),
                    table=table,
                    database=db_name,
                )

    try:
        parsed_params = json.loads(params) if isinstance(params, str) else params
    except json.JSONDecodeError:
        return _err("invalid_input", "params is not valid JSON")

    try:
        with pool_manager.get_connection(db_name) as conn:
            result = conn.execute(text(sql), parsed_params)
            rows_affected = result.rowcount
            conn.commit()
            return _json({
                "operation": operation,
                "table": table,
                "rows_affected": rows_affected,
            })
    except Exception as exc:
        code, extras = classify_db_exception(exc)
        extras.setdefault("table", table)
        extras.setdefault("database", db_name)
        logger.warning("db_execute failed for %s.%s: code=%s msg=%s", db_name, table, code, exc)
        return _err(code, str(exc), **extras)


@mcp.tool()
def db_transaction(
    statements: str,
    db_name: str,
    caller_id: str,
) -> str:
    """Execute multiple write statements atomically in a single transaction.

    Use this when you need multiple INSERT/UPDATE/DELETE statements to
    succeed or fail together (e.g. writing a parent row + child rows where
    orphan children are worse than failing the whole set).

    Args:
        statements: JSON array of ``{"sql": "...", "params": {...}}`` objects.
                    Each sql must be INSERT/UPDATE/DELETE (no SELECT, no DDL).
        db_name: Database name (chat, rag, user, qa).
        caller_id: Service identifier for access control.

    Returns JSON of one of two shapes:
      success: ``{"statements_executed": N, "rows_affected_total": M,
                  "per_statement": [{"operation": "INSERT", "table": "t",
                                      "rows_affected": K}, ...]}``
      error:   ``{"error": {"code": ..., "message": ..., "statement_index": N}}``
               On any failure all statements roll back — no partial writes.
    """
    if not db_name:
        return _err("invalid_input", "db_name is required")
    if not caller_id:
        return _err("invalid_input", "caller_id is required")
    if db_name not in pool_manager.available_databases():
        return _err("invalid_input", f"Unknown database: {db_name}")

    try:
        parsed = json.loads(statements) if isinstance(statements, str) else statements
    except json.JSONDecodeError:
        return _err("invalid_input", "statements is not valid JSON")
    if not isinstance(parsed, list) or not parsed:
        return _err("invalid_input", "statements must be a non-empty JSON array")

    # Pre-flight: validate every statement before opening a transaction.
    # Gates: ddl_forbidden, readonly_violation (SELECT), invalid_input,
    # access_denied, column_missing. If any fails, return immediately —
    # no connection acquired.
    validated: list[tuple[str, str, str, dict]] = []  # (operation, table, sql, params)
    for idx, stmt in enumerate(parsed):
        if not isinstance(stmt, dict):
            return _err("invalid_input", f"statement {idx} is not an object",
                        statement_index=idx)
        sql = stmt.get("sql") or ""
        if not sql.strip():
            return _err("invalid_input", f"statement {idx} has empty sql",
                        statement_index=idx)
        raw_params = stmt.get("params") or {}
        if isinstance(raw_params, str):
            try:
                raw_params = json.loads(raw_params)
            except json.JSONDecodeError:
                return _err("invalid_input",
                            f"statement {idx}: params is not valid JSON",
                            statement_index=idx)
        if not isinstance(raw_params, dict):
            return _err("invalid_input",
                        f"statement {idx}: params must be an object",
                        statement_index=idx)

        if _is_ddl(sql):
            return _err("ddl_forbidden",
                        f"statement {idx}: DDL not allowed in transactions",
                        statement_index=idx)

        target = _extract_write_target(sql)
        if not target:
            if _is_read_only(sql):
                return _err("readonly_violation",
                            f"statement {idx}: SELECT not allowed; use db_query",
                            statement_index=idx)
            return _err("invalid_input",
                        f"statement {idx}: only INSERT/UPDATE/DELETE allowed",
                        statement_index=idx)

        operation, table = target

        if not access_control.check_write(caller_id, db_name, table):
            return _err("access_denied",
                        f"{caller_id} cannot write to {db_name}.{table}",
                        statement_index=idx, table=table, database=db_name)

        if operation in ("INSERT", "UPDATE"):
            columns = _extract_write_columns(sql, operation)
            if columns:
                errors = contract_validator.validate_write(db_name, table, columns, pool_manager)
                if errors:
                    return _err("column_missing", "; ".join(errors),
                                statement_index=idx, table=table, database=db_name)

        validated.append((operation, table, sql, raw_params))

    # Execute atomically. SQLAlchemy's engine.connect() opens an implicit
    # transaction; commit() at the end finalizes, any exception before
    # that triggers the pool's pool_reset_on_return="rollback".
    per_statement: list[dict] = []
    rows_total = 0
    try:
        with pool_manager.get_connection(db_name) as conn:
            try:
                for operation, table, sql, raw_params in validated:
                    res = conn.execute(text(sql), raw_params)
                    ra = res.rowcount
                    per_statement.append({
                        "operation": operation,
                        "table": table,
                        "rows_affected": ra,
                    })
                    rows_total += ra if ra and ra > 0 else 0
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception as exc:
        code, extras = classify_db_exception(exc)
        # Best-effort: point at the statement that broke (length of
        # per_statement is the index that failed, since we append only
        # after a successful execute).
        extras["statement_index"] = len(per_statement)
        extras.setdefault("database", db_name)
        logger.warning(
            "db_transaction failed for %s at stmt %d: code=%s msg=%s",
            db_name, extras["statement_index"], code, exc,
        )
        return _err(code, str(exc), **extras)

    return _json({
        "statements_executed": len(per_statement),
        "rows_affected_total": rows_total,
        "per_statement": per_statement,
    })


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
        return _err("invalid_input", "db_name is required")
    if not caller_id:
        return _err("invalid_input", "caller_id is required")

    if db_name not in pool_manager.available_databases():
        return _err("invalid_input", f"Unknown database: {db_name}")

    try:
        with pool_manager.get_connection(db_name) as conn:
            if not table:
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
                return _json({"database": db_name, "tables": readable})
            else:
                if not access_control.check_read(caller_id, db_name, table):
                    return _err(
                        "access_denied",
                        f"{caller_id} cannot read {db_name}.{table}",
                        table=table,
                        database=db_name,
                    )
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
                    return _err(
                        "relation_missing",
                        f"Table '{table}' not found in {db_name}",
                        table=table,
                        database=db_name,
                    )
                return _json({"database": db_name, "table": table, "columns": columns})
    except Exception as exc:
        code, extras = classify_db_exception(exc)
        logger.warning("db_get_schema failed for %s: code=%s msg=%s", db_name, code, exc)
        return _err(code, str(exc), **extras)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_write_columns(sql: str, operation: str) -> list[str]:
    """Best-effort extraction of column names from INSERT/UPDATE SQL."""
    cleaned = _strip_comments(sql)
    if operation == "INSERT":
        m = re.search(r"\(\s*([^)]+)\s*\)\s*VALUES", cleaned, re.IGNORECASE)
        if m:
            return [c.strip().strip('"') for c in m.group(1).split(",")]
    elif operation == "UPDATE":
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
