# Live smoke test

`scripts/smoke_test.py` boots the agent against real Postgres, exercises
every MCP tool end-to-end, and validates the fallback path. Run this
before a production cut, after any pool/protocol change, or when the unit
tests pass but you suspect a live-integration bug.

```bash
cd mobius-db-agent
.venv/bin/python scripts/smoke_test.py
```

## What it checks

| # | Check                  | What it validates |
|---|------------------------|---|
| 0 | Bootstrap              | Creates a disposable `_db_agent_smoke` table via direct psycopg2 (the agent — correctly — refuses DDL). |
| 1 | `db_health`            | Every configured database is reachable via its pool. |
| 2 | `db_query`             | `SELECT 1` + a real `information_schema.tables` query. |
| 3 | `db_execute`           | INSERT / UPDATE / DELETE lifecycle against the smoke table. |
| 4 | `db_transaction`       | Commit path (3 statements, one transaction, verify last write) **and** rollback path (PK violation on statement 1 → all reverted). |
| 5 | `db_get_schema`        | List tables + describe the smoke table's columns. |
| 6 | Structured errors      | `relation_missing`, `readonly_violation`, `ddl_forbidden` all surface with correct codes. |
| 7 | Latency                | p50/p95 over 50 `SELECT 1` calls. **Non-fatal** — interpret with the environment in mind (see below). |
| 8 | Fallback               | Kill the agent, call `db_client.db_query` from a fresh interpreter, confirm `_fallback: true` response from direct psycopg2. |

The script writes a temporary `manifests/smoke-test.yml` granting wildcard
access, and cleans it up on exit along with the bootstrap table — whether
the run passes or fails.

## Interpreting latency

**Unix-socket or same-host TCP Postgres:** single-digit ms per call.
p95 > 50 ms indicates a real agent-side regression (pool churn,
connection thrashing, per-call re-auth).

**Cloud SQL Proxy tunnel (common in dev on this box — check with
`lsof -iTCP:5433 -sTCP:LISTEN`):** expect 200–500 ms per call. Every
query crosses the proxy to us-central1. That latency is the proxy, not
the agent. Don't chase it here.

A concrete historical data point: against `mobius-os-dev` Cloud SQL via
the proxy on a developer laptop, the smoke test reports ~1.2 s p50. The
same code against an in-process SQLite or a sibling-pod PG reports
< 5 ms p50. The agent's own overhead on top of the pool is negligible.

## Bugs this caught

### Accept header (fixed 2026-04-20)

Before this smoke test existed, `db_client.py` sent `Accept: application/json`
to the agent's streamable-http endpoint. FastMCP's streamable-http transport
requires `Accept: application/json, text/event-stream` even when
`json_response=True` is set — otherwise the server hangs in stream
negotiation instead of returning the JSON payload.

The unit tests (mocked MCP) didn't catch this because they never
crossed the wire. The smoke test caught it on the first live boot:
every call timed out at 15 s. Fix: add `text/event-stream` to the
client's Accept header. See the commit that added this file.

## When to run

- Before a deployment cut — confirms the agent works against the target
  database stack and manifests.
- After any change to `app/pools.py`, `app/server.py`, `db_client.py`,
  or the streamable-http wire format.
- After adding a new MCP tool — extend the script to cover it.
- As part of an on-call runbook when a caller reports "agent up but
  every query hangs" — this script isolates protocol, pool, and
  fallback layers individually.

## Extending

Each check is its own `check_*` function. Add a new step by:

1. Writing a `check_foo()` that calls `require(cond, label, msg)` for
   every assertion — a `False` raises `SmokeFailure` and cleanup runs.
2. Calling it from `main()` in the block between `check_error_codes()`
   and `check_latency()` (or wherever ordering matters).
3. If the check needs pre-state, add it to the bootstrap phase and the
   cleanup hook so the script stays re-runnable.
