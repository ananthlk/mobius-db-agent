"""Structured error model for mobius-db-agent.

Every error returned by the MCP tools follows the shape:

    {"error": {"code": "<code>", "message": "<human-readable>", ...optional fields}}

Callers switch on ``error["code"]`` (stable) not on ``error["message"]``
(may change). See docs/db-agent-contract.md § "Error Model".

Taxonomy:
    access_denied         Table not in caller's manifest
    relation_missing      Table/view doesn't exist
    column_missing        Column in INSERT/UPDATE doesn't exist
    readonly_violation    Write statement passed to db_query
    ddl_forbidden         DDL statement attempted
    row_limit_exceeded    SELECT exceeded max_rows (data still returned truncated)
    timeout               Query exceeded timeout_seconds
    syntax_error          Bad SQL
    integrity_violation   Unique / FK / check constraint failed
    connection_error      Pool unavailable / DB unreachable
    invalid_input         Bad args (empty sql, unknown db, bad json params)
    internal              Anything else — message has details
"""
from __future__ import annotations

from typing import Any

# PostgreSQL SQLSTATE class and code prefixes.
# https://www.postgresql.org/docs/current/errcodes-appendix.html
_SQLSTATE_MAP = {
    # Class 42 — Syntax Error or Access Rule Violation
    "42601": "syntax_error",            # syntax_error
    "42P01": "relation_missing",        # undefined_table
    "42703": "column_missing",          # undefined_column
    "42P07": "integrity_violation",     # duplicate_table (rare but structural)
    # Class 23 — Integrity Constraint Violation
    "23000": "integrity_violation",     # integrity_constraint_violation
    "23001": "integrity_violation",     # restrict_violation
    "23502": "integrity_violation",     # not_null_violation
    "23503": "integrity_violation",     # foreign_key_violation
    "23505": "integrity_violation",     # unique_violation
    "23514": "integrity_violation",     # check_violation
    # Class 40 — Transaction Rollback
    "40001": "integrity_violation",     # serialization_failure
    "40P01": "integrity_violation",     # deadlock_detected
    # Class 57 — Operator Intervention
    "57014": "timeout",                 # query_canceled (statement_timeout)
    "57P01": "connection_error",        # admin_shutdown
    "57P02": "connection_error",        # crash_shutdown
    "57P03": "connection_error",        # cannot_connect_now
    # Class 08 — Connection Exception
    "08000": "connection_error",
    "08003": "connection_error",        # connection_does_not_exist
    "08006": "connection_error",        # connection_failure
    "08001": "connection_error",        # sqlclient_unable_to_establish
    "08004": "connection_error",        # sqlserver_rejected_establishment
}


def make_error(code: str, message: str, **extra: Any) -> dict:
    """Return the canonical error payload."""
    err: dict[str, Any] = {"code": code, "message": message}
    for k, v in extra.items():
        if v is not None:
            err[k] = v
    return {"error": err}


def classify_db_exception(exc: BaseException) -> tuple[str, dict[str, Any]]:
    """Map a DB driver exception to (code, extras).

    Inspects ``exc.orig.pgcode`` / ``exc.pgcode`` (SQLAlchemy / psycopg2) to
    route by SQLSTATE. Falls back to ``internal`` for anything unclassified.
    Extras include ``sqlstate`` and ``table``/``column`` where the driver
    surfaces them via ``diag``.
    """
    sqlstate: str | None = None
    table: str | None = None
    column: str | None = None

    # SQLAlchemy wraps driver exceptions in .orig
    orig = getattr(exc, "orig", None) or exc
    sqlstate = getattr(orig, "pgcode", None) or getattr(exc, "pgcode", None)

    diag = getattr(orig, "diag", None)
    if diag is not None:
        table = getattr(diag, "table_name", None)
        column = getattr(diag, "column_name", None)

    code = _SQLSTATE_MAP.get(sqlstate or "", None)
    if code is None:
        # Heuristic fallback for drivers that don't surface SQLSTATE
        # (e.g. when connecting fails before a session is established).
        msg_lower = str(exc).lower()
        if "does not exist" in msg_lower and "relation" in msg_lower:
            code = "relation_missing"
        elif "does not exist" in msg_lower and "column" in msg_lower:
            code = "column_missing"
        elif "timeout" in msg_lower or "canceling statement" in msg_lower:
            code = "timeout"
        elif "could not connect" in msg_lower or "connection refused" in msg_lower:
            code = "connection_error"
        elif "syntax error" in msg_lower:
            code = "syntax_error"
        else:
            code = "internal"

    extras: dict[str, Any] = {}
    if sqlstate:
        extras["sqlstate"] = sqlstate
    if table:
        extras["table"] = table
    if column:
        extras["column"] = column
    return code, extras
