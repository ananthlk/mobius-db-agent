"""Lightweight client for mobius-db-agent MCP server.

Drop this file into any service that needs database access via the MCP agent.
Falls back to direct psycopg2 when the agent is unavailable.

Usage:
    from db_client import db_query, db_execute, db_get_schema

    # Read
    result = db_query("SELECT * FROM mobius_task WHERE status = :s", "chat", params={"s": "open"})
    for row in result["rows"]:
        print(row)

    # Write
    result = db_execute(
        "INSERT INTO mobius_task (task_id, type, status) VALUES (:id, :t, :s)",
        "chat",
        params={"id": "abc", "t": "review", "s": "open"},
    )
    print(result["rows_affected"])

    # Schema
    tables = db_get_schema("chat")          # list tables
    cols = db_get_schema("chat", "mobius_task")  # table columns

Environment:
    DB_AGENT_MCP_URL  - MCP server URL (default: http://localhost:8008/mcp)
    DB_AGENT_CALLER_ID - Service identity for access control (REQUIRED)
    CHAT_RAG_DATABASE_URL - Fallback direct DB URL (used when agent unavailable)
"""
import json
import logging
import os
import re
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

_MCP_URL = os.environ.get("DB_AGENT_MCP_URL", "http://localhost:8008/mcp")
_CALLER_ID = os.environ.get("DB_AGENT_CALLER_ID", "")
_TIMEOUT = 15  # seconds


def _call_mcp_tool(tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool via streamable-http and return the parsed result."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }).encode()

    req = urllib.request.Request(
        _MCP_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            # FastMCP's streamable-http transport requires text/event-stream in
            # Accept even for json_response=True. Without it the server hangs
            # negotiating the stream instead of responding with JSON.
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )

    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    body = json.loads(resp.read())

    # MCP returns result in body["result"]["content"][0]["text"]
    if "result" in body:
        content = body["result"].get("content", [])
        if content and "text" in content[0]:
            return json.loads(content[0]["text"])
    if "error" in body:
        raise RuntimeError(f"MCP error: {body['error']}")
    return body


def _get_caller_id() -> str:
    if not _CALLER_ID:
        raise ValueError(
            "DB_AGENT_CALLER_ID env var is required. "
            "Set it to your service name (must match a manifest in mobius-db-agent/manifests/)."
        )
    return _CALLER_ID


# ---------------------------------------------------------------------------
# Direct fallback (when MCP agent is unavailable)
# ---------------------------------------------------------------------------

def _to_psycopg2_sql(sql: str) -> str:
    """Convert SQLAlchemy :param style to psycopg2 %(param)s style."""
    return re.sub(r":([a-zA-Z_][a-zA-Z0-9_]*)", r"%(\1)s", sql)


def _get_fallback_url(db_name: str) -> str:
    """Resolve a direct database URL from env vars."""
    url_map = {
        "chat": os.environ.get("CHAT_RAG_DATABASE_URL", ""),
        "rag": os.environ.get("DATABASE_URL", ""),
        "user": os.environ.get("USER_DATABASE_URL", ""),
        "qa": os.environ.get("QA_DATABASE_URL", ""),
    }
    url = url_map.get(db_name, "")
    # Strip async driver
    return re.sub(r"postgresql\+\w+://", "postgresql://", url)


def _fallback_error(exc: BaseException) -> dict:
    """Map a psycopg2 exception to the structured error shape used by the agent.

    Keeps the error model consistent whether the caller is hitting the MCP
    server or falling back to direct DB. Callers switch on error["code"].
    """
    sqlstate = getattr(exc, "pgcode", None)
    diag = getattr(exc, "diag", None)
    table = getattr(diag, "table_name", None) if diag else None
    column = getattr(diag, "column_name", None) if diag else None

    # Minimal SQLSTATE map duplicated from app/errors.py so db_client.py has
    # no dependency on the server-side package. Keep in sync.
    sqlstate_map = {
        "42601": "syntax_error", "42P01": "relation_missing", "42703": "column_missing",
        "23000": "integrity_violation", "23001": "integrity_violation",
        "23502": "integrity_violation", "23503": "integrity_violation",
        "23505": "integrity_violation", "23514": "integrity_violation",
        "40001": "integrity_violation", "40P01": "integrity_violation",
        "57014": "timeout", "57P01": "connection_error", "57P02": "connection_error",
        "57P03": "connection_error",
        "08000": "connection_error", "08001": "connection_error",
        "08003": "connection_error", "08004": "connection_error", "08006": "connection_error",
    }
    code = sqlstate_map.get(sqlstate or "")
    if code is None:
        msg_lower = str(exc).lower()
        if "does not exist" in msg_lower and "relation" in msg_lower:
            code = "relation_missing"
        elif "does not exist" in msg_lower and "column" in msg_lower:
            code = "column_missing"
        elif "could not connect" in msg_lower or "connection refused" in msg_lower:
            code = "connection_error"
        elif "syntax error" in msg_lower:
            code = "syntax_error"
        else:
            code = "internal"

    err: dict = {"code": code, "message": str(exc)}
    if sqlstate:
        err["sqlstate"] = sqlstate
    if table:
        err["table"] = table
    if column:
        err["column"] = column
    return {"error": err, "_fallback": True}


def _fallback_query(sql: str, db_name: str, params: dict, max_rows: int) -> dict:
    """Direct psycopg2 query when MCP agent is down."""
    import psycopg2
    import psycopg2.extras  # noqa: F401

    url = _get_fallback_url(db_name)
    if not url:
        return {
            "error": {"code": "connection_error",
                      "message": f"No fallback URL for database '{db_name}'"},
            "_fallback": True,
        }

    try:
        conn = psycopg2.connect(url, connect_timeout=10)
    except Exception as exc:
        return _fallback_error(exc)

    try:
        with conn.cursor() as cur:
            cur.execute(_to_psycopg2_sql(sql), params or None)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows)
            return {
                "columns": columns,
                "rows": [list(r) for r in rows],
                "row_count": len(rows),
                "truncated": len(rows) == max_rows,
                "_fallback": True,
            }
    except Exception as exc:
        return _fallback_error(exc)
    finally:
        conn.close()


def _fallback_execute(sql: str, db_name: str, params: dict) -> dict:
    """Direct psycopg2 execute when MCP agent is down."""
    import psycopg2

    url = _get_fallback_url(db_name)
    if not url:
        return {
            "error": {"code": "connection_error",
                      "message": f"No fallback URL for database '{db_name}'"},
            "_fallback": True,
        }

    try:
        conn = psycopg2.connect(url, connect_timeout=10)
    except Exception as exc:
        return _fallback_error(exc)

    try:
        with conn.cursor() as cur:
            cur.execute(_to_psycopg2_sql(sql), params or None)
            rows_affected = cur.rowcount
        conn.commit()
        return {"rows_affected": rows_affected, "_fallback": True}
    except Exception as exc:
        conn.rollback()
        return _fallback_error(exc)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def db_query(
    sql: str,
    db_name: str,
    params: dict | None = None,
    max_rows: int = 1000,
) -> dict:
    """Execute a read-only SQL query. Falls back to direct DB if agent is down."""
    try:
        return _call_mcp_tool("db_query", {
            "sql": sql,
            "db_name": db_name,
            "caller_id": _get_caller_id(),
            "params": json.dumps(params or {}),
            "max_rows": max_rows,
        })
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        logger.warning("db-agent unavailable (%s), falling back to direct DB", exc)
        return _fallback_query(sql, db_name, params or {}, max_rows)


def db_execute(
    sql: str,
    db_name: str,
    params: dict | None = None,
) -> dict:
    """Execute a write SQL statement. Falls back to direct DB if agent is down."""
    try:
        return _call_mcp_tool("db_execute", {
            "sql": sql,
            "db_name": db_name,
            "caller_id": _get_caller_id(),
            "params": json.dumps(params or {}),
        })
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        logger.warning("db-agent unavailable (%s), falling back to direct DB", exc)
        return _fallback_execute(sql, db_name, params or {})


def db_get_schema(
    db_name: str,
    table: str = "",
) -> dict:
    """Get schema info. No fallback — requires the agent."""
    return _call_mcp_tool("db_get_schema", {
        "db_name": db_name,
        "caller_id": _get_caller_id(),
        "table": table,
    })


def db_transaction(
    statements: list[dict],
    db_name: str,
) -> dict:
    """Execute multiple INSERT/UPDATE/DELETE statements as one atomic transaction.

    Args:
        statements: list of ``{"sql": "<:param style SQL>", "params": {...}}``
                    — all statements must be writes (no SELECT, no DDL).
        db_name: target database name (chat / rag / user / qa).

    Success returns:
        {
            "statements_executed": N,
            "rows_affected_total": M,
            "per_statement": [{"operation": "INSERT", "table": "t",
                                "rows_affected": K}, ...],
        }

    Failure returns structured ``{"error": {"code": ..., "message": ...,
    "statement_index": N}}`` — any failure rolls back the entire transaction,
    so callers never see partial writes.

    No direct-DB fallback: transactions are the whole point. If the agent
    is down, the caller's best option is to degrade (skip persistence or
    split into individual db_execute calls, accepting loss of atomicity).
    """
    return _call_mcp_tool("db_transaction", {
        "statements": json.dumps(statements),
        "db_name": db_name,
        "caller_id": _get_caller_id(),
    })
