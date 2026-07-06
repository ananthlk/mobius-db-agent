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
        headers={"Content-Type": "application/json"},
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


def _fallback_query(sql: str, db_name: str, params: dict, max_rows: int) -> dict:
    """Direct psycopg2 query when MCP agent is down."""
    import psycopg2
    import psycopg2.extras

    url = _get_fallback_url(db_name)
    if not url:
        raise RuntimeError(f"No fallback URL for database '{db_name}'")

    conn = psycopg2.connect(url, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or None)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows)
            return {
                "columns": columns,
                "rows": [list(r) for r in rows],
                "row_count": len(rows),
                "truncated": len(rows) == max_rows,
                "_fallback": True,
            }
    finally:
        conn.close()


def _fallback_execute(sql: str, db_name: str, params: dict) -> dict:
    """Direct psycopg2 execute when MCP agent is down."""
    import psycopg2

    url = _get_fallback_url(db_name)
    if not url:
        raise RuntimeError(f"No fallback URL for database '{db_name}'")

    conn = psycopg2.connect(url, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or None)
            rows_affected = cur.rowcount
        conn.commit()
        return {"rows_affected": rows_affected, "_fallback": True}
    except Exception:
        conn.rollback()
        raise
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
