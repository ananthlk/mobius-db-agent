"""Live smoke test for mobius-db-agent.

Boots the agent in a subprocess, exercises every MCP tool against a real
PostgreSQL, validates the fallback path, measures p50/p95 latency, then
cleans up.

Exits 0 on all-green, non-zero on the first failure. Designed to be run
locally (chat PG on 127.0.0.1:5433) or in CI before a production cut.

Usage:
    cd mobius-db-agent
    .venv/bin/python scripts/smoke_test.py

Environment requirements:
    - The four mobius-* databases exist and the URLs point at them
      (defaults match the local dev .env). Override any of:
        CHAT_RAG_DATABASE_URL
        DATABASE_URL          (for rag)
        USER_DATABASE_URL
        QA_DATABASE_URL

    - Port 8008 free (or override DB_AGENT_PORT).

What this validates:
    1. Agent boots, listens on the configured port.
    2. db_health returns per-database reachability.
    3. db_query: SELECT 1 + a real table SELECT.
    4. db_execute: INSERT into a disposable smoke-test row, UPDATE, DELETE.
       All cleaned up before exit.
    5. db_transaction: multi-statement commit + rollback-on-error.
    6. db_get_schema: list tables + describe one.
    7. Structured errors: unknown-table, readonly-violation, DDL-forbidden.
    8. Fallback: kill the agent mid-run and confirm db_client.db_query
       still works via direct psycopg2 (returns _fallback=True).
    9. Latency: p50/p95 over 50 SELECT 1 calls, reported but non-fatal.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from statistics import median

# ── Config ────────────────────────────────────────────────────────────────

REPO = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("DB_AGENT_PORT", "8008"))
AGENT_URL = f"http://127.0.0.1:{PORT}/mcp"
READY_TIMEOUT_S = 15
SMOKE_CALLER = "smoke-test"
SMOKE_TABLE = "_db_agent_smoke"  # disposable table we create/drop
LATENCY_SAMPLES = 50

# Matches local dev defaults; override via env.
DEFAULTS = {
    "CHAT_RAG_DATABASE_URL": "postgresql://postgres:MobiusDev123%24@127.0.0.1:5433/mobius_chat",
    "DATABASE_URL":          "postgresql://postgres:MobiusDev123%24@127.0.0.1:5433/mobius_rag",
    "USER_DATABASE_URL":     "postgresql://postgres:MobiusDev123%24@127.0.0.1:5433/mobius_user",
    "QA_DATABASE_URL":       "postgresql://postgres:MobiusDev123%24@127.0.0.1:5433/mobius_qa",
}

# ── Tiny colour helpers ──────────────────────────────────────────────────

def _g(s): return f"\033[32m{s}\033[0m"
def _r(s): return f"\033[31m{s}\033[0m"
def _y(s): return f"\033[33m{s}\033[0m"
def _b(s): return f"\033[1m{s}\033[0m"


def log(label: str, msg: str, ok: bool | None = None) -> None:
    marker = _g("✓") if ok is True else (_r("✗") if ok is False else _y("•"))
    print(f"  {marker} {label}: {msg}")


# ── MCP client ──────────────────────────────────────────────────────────


def mcp_call(tool: str, args: dict) -> dict:
    """Call an MCP tool via JSON-RPC over HTTP."""
    import json
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }).encode()
    req = urllib.request.Request(
        AGENT_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    # 60s timeout: the first call (typically db_health) creates all 4 pools
    # with pre_ping validation, which can take 10-20s on cold start.
    # Subsequent calls reuse pools and complete in single-digit ms.
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
    result = body.get("result") or {}
    content = result.get("content") or []
    if content and "text" in content[0]:
        return json.loads(content[0]["text"])
    return body


# ── Agent lifecycle ─────────────────────────────────────────────────────


def wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def start_agent(env: dict) -> subprocess.Popen:
    """Boot the agent as a subprocess. Returns the Popen handle."""
    venv_python = REPO / ".venv" / "bin" / "python"
    if not venv_python.exists():
        print(_r(f"Missing {venv_python} — run `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` first"))
        sys.exit(1)
    return subprocess.Popen(
        [str(venv_python), "-m", "app"],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def stop_agent(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)
    except Exception:
        pass


# ── Checks ──────────────────────────────────────────────────────────────


class SmokeFailure(AssertionError):
    pass


def require(cond: bool, label: str, msg: str) -> None:
    log(label, msg, ok=cond)
    if not cond:
        raise SmokeFailure(f"{label}: {msg}")


def check_health() -> None:
    print(_b("\n[1] db_health"))
    r = mcp_call("db_health", {})
    reachable = r.get("databases") or r  # tool returns either shape
    require(isinstance(reachable, dict), "response", f"got {type(reachable).__name__}")
    for db_name, data in reachable.items():
        if isinstance(data, dict):
            ok = bool(data.get("reachable", False))
        else:
            ok = bool(data)
        require(ok, db_name, "reachable" if ok else f"unreachable ({data!r})")


def check_query() -> None:
    print(_b("\n[2] db_query"))
    r = mcp_call("db_query", {"sql": "SELECT 1 AS one", "db_name": "chat",
                               "caller_id": SMOKE_CALLER})
    require("columns" in r, "SELECT 1", f"columns present; row_count={r.get('row_count')}")
    require(r["rows"][0][0] == 1, "SELECT 1 value", f"got {r['rows'][0][0]}")

    r2 = mcp_call("db_query", {
        "sql": "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = 'public'",
        "db_name": "chat",
        "caller_id": SMOKE_CALLER,
    })
    require("columns" in r2, "tables count", f"returned {r2['rows'][0][0]} tables in public")


def _bootstrap_smoke_table() -> None:
    """Create a disposable table via direct psycopg2 — we need DDL which the
    agent (correctly) forbids. This gets dropped in cleanup."""
    import psycopg2
    url = os.environ["CHAT_RAG_DATABASE_URL"].replace("%24", "$")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {SMOKE_TABLE}")
        cur.execute(
            f"CREATE TABLE {SMOKE_TABLE} (id text PRIMARY KEY, value int, note text)"
        )
    conn.close()


def _drop_smoke_table() -> None:
    import psycopg2
    url = os.environ["CHAT_RAG_DATABASE_URL"].replace("%24", "$")
    try:
        conn = psycopg2.connect(url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {SMOKE_TABLE}")
        conn.close()
    except Exception as e:
        print(_y(f"  • cleanup note: DROP {SMOKE_TABLE} failed: {e}"))


def _write_manifest_allowing_smoke_table(tmp_manifests_dir: Path) -> None:
    """Write a manifest that lets smoke-test read/write the smoke table."""
    (tmp_manifests_dir / "smoke-test.yml").write_text(
        f"""\
service: {SMOKE_CALLER}
permissions:
  chat:
    read: ["*"]
    write: ["*"]
limits:
  max_rows: 1000
  timeout_seconds: 10
"""
    )


def check_execute() -> None:
    print(_b("\n[3] db_execute"))
    r = mcp_call("db_execute", {
        "sql": f"INSERT INTO {SMOKE_TABLE} (id, value, note) VALUES (:id, :v, :n)",
        "db_name": "chat",
        "caller_id": SMOKE_CALLER,
        "params": '{"id": "s-1", "v": 42, "n": "hello"}',
    })
    require(r.get("rows_affected") == 1, "INSERT", f"rows_affected={r.get('rows_affected')}")

    r2 = mcp_call("db_execute", {
        "sql": f"UPDATE {SMOKE_TABLE} SET value = :v WHERE id = :id",
        "db_name": "chat",
        "caller_id": SMOKE_CALLER,
        "params": '{"id": "s-1", "v": 43}',
    })
    require(r2.get("rows_affected") == 1, "UPDATE", f"rows_affected={r2.get('rows_affected')}")

    r3 = mcp_call("db_execute", {
        "sql": f"DELETE FROM {SMOKE_TABLE} WHERE id = :id",
        "db_name": "chat",
        "caller_id": SMOKE_CALLER,
        "params": '{"id": "s-1"}',
    })
    require(r3.get("rows_affected") == 1, "DELETE", f"rows_affected={r3.get('rows_affected')}")


def check_transaction() -> None:
    import json
    print(_b("\n[4] db_transaction"))

    # 4a. Commit path
    stmts_ok = [
        {"sql": f"INSERT INTO {SMOKE_TABLE} (id, value) VALUES (:id, :v)",
         "params": {"id": "tx-1", "v": 1}},
        {"sql": f"INSERT INTO {SMOKE_TABLE} (id, value) VALUES (:id, :v)",
         "params": {"id": "tx-2", "v": 2}},
        {"sql": f"UPDATE {SMOKE_TABLE} SET value = :v WHERE id = :id",
         "params": {"id": "tx-1", "v": 10}},
    ]
    r = mcp_call("db_transaction", {
        "statements": json.dumps(stmts_ok),
        "db_name": "chat",
        "caller_id": SMOKE_CALLER,
    })
    require(r.get("statements_executed") == 3, "commit path", f"executed {r.get('statements_executed')}/3")

    # Verify row state: tx-1.value should be 10.
    r2 = mcp_call("db_query", {
        "sql": f"SELECT value FROM {SMOKE_TABLE} WHERE id = :id",
        "db_name": "chat", "caller_id": SMOKE_CALLER,
        "params": '{"id": "tx-1"}',
    })
    require(r2["rows"][0][0] == 10, "commit value", f"tx-1.value = {r2['rows'][0][0]}")

    # 4b. Rollback path — second statement violates PK (tx-1 already exists).
    stmts_fail = [
        {"sql": f"INSERT INTO {SMOKE_TABLE} (id, value) VALUES (:id, :v)",
         "params": {"id": "tx-3", "v": 30}},
        {"sql": f"INSERT INTO {SMOKE_TABLE} (id, value) VALUES (:id, :v)",
         "params": {"id": "tx-1", "v": 999}},  # duplicate key
    ]
    r3 = mcp_call("db_transaction", {
        "statements": json.dumps(stmts_fail),
        "db_name": "chat",
        "caller_id": SMOKE_CALLER,
    })
    require("error" in r3, "rollback path", f"got error code={r3.get('error', {}).get('code')}")
    require(r3["error"]["code"] == "integrity_violation", "error code",
            f"expected integrity_violation, got {r3['error']['code']}")
    require(r3["error"]["statement_index"] == 1, "statement_index",
            f"expected 1, got {r3['error'].get('statement_index')}")

    # Verify rollback actually happened: tx-3 must NOT exist.
    r4 = mcp_call("db_query", {
        "sql": f"SELECT COUNT(*) FROM {SMOKE_TABLE} WHERE id = :id",
        "db_name": "chat", "caller_id": SMOKE_CALLER,
        "params": '{"id": "tx-3"}',
    })
    require(r4["rows"][0][0] == 0, "rollback verified", "tx-3 absent (0 rows)")

    # Cleanup
    mcp_call("db_execute", {
        "sql": f"DELETE FROM {SMOKE_TABLE} WHERE id IN ('tx-1', 'tx-2')",
        "db_name": "chat", "caller_id": SMOKE_CALLER,
    })


def check_schema() -> None:
    print(_b("\n[5] db_get_schema"))
    r = mcp_call("db_get_schema", {"db_name": "chat", "caller_id": SMOKE_CALLER})
    tables = r.get("tables") or []
    require(SMOKE_TABLE in tables, "list tables", f"{SMOKE_TABLE} present ({len(tables)} tables total)")

    r2 = mcp_call("db_get_schema", {"db_name": "chat", "caller_id": SMOKE_CALLER,
                                      "table": SMOKE_TABLE})
    cols = [c["name"] for c in (r2.get("columns") or [])]
    require(cols == ["id", "value", "note"], "describe table", f"columns={cols}")


def check_error_codes() -> None:
    print(_b("\n[6] structured errors"))
    r = mcp_call("db_query", {
        "sql": "SELECT * FROM does_not_exist",
        "db_name": "chat", "caller_id": SMOKE_CALLER,
    })
    # We explicitly allow "*" for smoke-test so access passes — then the
    # query hits the DB and comes back as relation_missing.
    err = r.get("error") or {}
    require(err.get("code") == "relation_missing", "missing table", f"code={err.get('code')}")

    r2 = mcp_call("db_query", {
        "sql": "DELETE FROM chat_feedback", "db_name": "chat", "caller_id": SMOKE_CALLER,
    })
    require(r2["error"]["code"] == "readonly_violation", "readonly_violation",
            f"code={r2['error']['code']}")

    r3 = mcp_call("db_execute", {
        "sql": "DROP TABLE chat_feedback", "db_name": "chat", "caller_id": SMOKE_CALLER,
    })
    require(r3["error"]["code"] == "ddl_forbidden", "ddl_forbidden",
            f"code={r3['error']['code']}")


def check_latency() -> None:
    print(_b("\n[7] latency (p50/p95 over %d SELECT 1 calls)" % LATENCY_SAMPLES))
    times_ms: list[float] = []
    for _ in range(LATENCY_SAMPLES):
        t0 = time.perf_counter()
        mcp_call("db_query", {"sql": "SELECT 1", "db_name": "chat",
                               "caller_id": SMOKE_CALLER})
        times_ms.append((time.perf_counter() - t0) * 1000)
    times_ms.sort()
    p50 = median(times_ms)
    p95 = times_ms[int(0.95 * len(times_ms))]
    log("p50", f"{p50:.2f} ms")
    log("p95", f"{p95:.2f} ms")
    # Non-fatal: just report. Interpretation depends on the environment:
    #   * Unix-socket Postgres: expect single-digit ms. p95 > 50ms = agent bug.
    #   * Cloud SQL Proxy to a remote Cloud SQL instance (typical dev setup
    #     on this box: port 5433 → us-central1): expect 200-500ms per call
    #     because every query crosses the proxy + internet. That's the
    #     proxy, not the agent.
    # Check the listener with `lsof -iTCP:5433 -sTCP:LISTEN` before
    # drawing conclusions from an unexpectedly high p50.
    if p95 > 2000:
        print(_y(f"  ! p95 is {p95:.0f}ms — unusually high even for a remote Cloud SQL proxy"))


def check_fallback(agent_proc: subprocess.Popen) -> None:
    print(_b("\n[8] fallback (agent down → direct psycopg2)"))
    # Kill agent so the HTTP call fails.
    stop_agent(agent_proc)
    time.sleep(0.5)

    # Spawn a fresh interpreter that imports db_client and calls db_query.
    # Has to be subprocess because db_client caches the module-level
    # _MCP_URL from env at import time.
    script = (
        "import os, sys, json; "
        "os.environ['DB_AGENT_MCP_URL'] = 'http://127.0.0.1:8008/mcp'; "
        f"os.environ['DB_AGENT_CALLER_ID'] = {SMOKE_CALLER!r}; "
        "sys.path.insert(0, '.'); "
        "from db_client import db_query; "
        "r = db_query('SELECT 1 AS one', 'chat'); "
        "print(json.dumps(r))"
    )
    result = subprocess.run(
        [str(REPO / ".venv/bin/python"), "-c", script],
        cwd=str(REPO),
        env={**os.environ},
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        print(result.stderr)
        raise SmokeFailure(f"fallback subprocess failed: {result.stderr[:200]}")
    import json
    payload = json.loads(result.stdout.strip())
    require(payload.get("_fallback") is True, "fallback flag",
            f"_fallback={payload.get('_fallback')}")
    require(payload.get("rows", [[None]])[0][0] == 1, "fallback value",
            f"got {payload.get('rows')}")


# ── Orchestration ───────────────────────────────────────────────────────


def main() -> int:
    # Resolve effective env (default → env overrides). Apply to both
    # os.environ (so in-process helpers like _bootstrap_smoke_table see
    # the URLs) and the subprocess env (for the agent).
    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)
    env = os.environ.copy()

    # Write temporary manifest allowing smoke-test access.
    manifests_dir = REPO / "manifests"
    smoke_manifest = manifests_dir / "smoke-test.yml"
    _write_manifest_allowing_smoke_table(manifests_dir)

    # Bootstrap the disposable table (requires DDL → direct psycopg2).
    print(_b("[0] bootstrap"))
    try:
        _bootstrap_smoke_table()
        log("create table", f"{SMOKE_TABLE} created", ok=True)
    except Exception as e:
        log("create table", f"FAILED: {e}", ok=False)
        return 2

    # Boot the agent.
    print(_b("\nstarting db-agent on port %d..." % PORT))
    agent_proc = start_agent(env)
    try:
        if not wait_for_port("127.0.0.1", PORT, READY_TIMEOUT_S):
            agent_proc.kill()
            out = agent_proc.stdout.read() if agent_proc.stdout else ""
            print(_r(f"Agent failed to bind to port {PORT}:\n{out[-1000:]}"))
            return 3
        print(_g(f"agent ready at {AGENT_URL}"))

        check_health()
        check_query()
        check_execute()
        check_transaction()
        check_schema()
        check_error_codes()
        check_latency()
        check_fallback(agent_proc)  # kills agent

        print(_g(_b("\n✓ ALL SMOKE CHECKS PASSED")))
        return 0
    except SmokeFailure as e:
        print(_r(_b(f"\n✗ SMOKE TEST FAILED: {e}")))
        return 1
    except Exception as e:
        print(_r(_b(f"\n✗ SMOKE TEST CRASHED: {type(e).__name__}: {e}")))
        import traceback; traceback.print_exc()
        return 4
    finally:
        stop_agent(agent_proc)
        _drop_smoke_table()
        if smoke_manifest.exists():
            smoke_manifest.unlink()
        print(_y("\ncleaned up smoke table + manifest"))


if __name__ == "__main__":
    sys.exit(main())
