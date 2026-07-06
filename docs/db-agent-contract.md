# Database Access Contract — mobius-db-agent

**Purpose:** hand this to any agent or developer refactoring a Mobius module
so all database access flows through the `mobius-db-agent` MCP server instead
of scattered `psycopg2.connect()` calls. Shorter and more directive than
`INTEGRATION_SPEC.md` — drop it into a system prompt or task brief.

---

You are working on a Mobius module that accesses PostgreSQL databases.
ALL database access MUST go through the mobius-db-agent MCP server.
Do NOT use psycopg2, asyncpg, or SQLAlchemy directly.

## Scoping Rule (read first)

**Refactor at the lowest layer that owns the SQL.** In Mobius this is usually
`app/storage/*.py` or `app/persistence/*.py` — not the router / handler / skill
on top of it.

If the file you were pointed at is a FastAPI router, a worker entrypoint, or
a skill, and it has no `psycopg2` import: **trace down** to the helper it
imports from and refactor that helper instead. Do NOT inline SQL up into the
caller — that duplicates queries that are shared across routers, workers,
and background jobs.

Quick test: `grep -l "psycopg2\|sqlalchemy" <file>`. If empty, that file is
not the right refactor target — follow its imports down.

## Setup (do these once)

1. Copy `mobius-db-agent/db_client.py` into your module's `app/` directory.
2. Create a manifest at `mobius-db-agent/manifests/<your-service>.yml`:

   ```yaml
   service: <your-service-name>       # e.g. "claims-processor"
   permissions:
     chat:                             # database: chat | rag | user | qa
       read: [<table1>, <table2>]     # tables you SELECT from
       write: [<table1>]              # tables you INSERT/UPDATE/DELETE
   limits:
     max_rows: 5000
     timeout_seconds: 15
   ```

   A table appears under both `read` and `write` if you do both.

3. Set env var in your deployment config (not in code):
   `DB_AGENT_CALLER_ID=<your-service-name>`. `db_client.py` reads it once per
   process.

## How to Read

```python
from app.db_client import db_query

result = db_query(
    "SELECT id, status, created_at FROM claims WHERE patient_id = :pid",
    "chat",                          # database name
    params={"pid": "abc-123"},       # named params with :param syntax
)

# result shape:
# {
#     "columns": ["id", "status", "created_at"],
#     "rows": [["claim-1", "pending", "2026-03-01T..."], ...],
#     "row_count": 3,
#     "truncated": False,
# }

if "error" in result:
    raise RuntimeError(result["error"]["message"])

rows = [dict(zip(result["columns"], r)) for r in result["rows"]]
```

## How to Write

```python
from app.db_client import db_execute

result = db_execute(
    "INSERT INTO claims (id, patient_id, status, amount) "
    "VALUES (:id, :pid, :status, :amt)",
    "chat",
    params={"id": "claim-99", "pid": "abc-123", "status": "submitted", "amt": 150.00},
)

# result shape:
# {"operation": "INSERT", "table": "claims", "rows_affected": 1}

if "error" in result:
    raise RuntimeError(result["error"]["message"])
```

### UPSERT (INSERT … ON CONFLICT)

Allowed and common.

```python
db_execute(
    """
    INSERT INTO chat_feedback (correlation_id, rating, comment, created_at)
    VALUES (:cid, :rating, :comment, now())
    ON CONFLICT (correlation_id) DO UPDATE SET
        rating = EXCLUDED.rating,
        comment = EXCLUDED.comment,
        created_at = now()
    """,
    "chat",
    params={"cid": "abc", "rating": "up", "comment": None},
)
```

### JSONB writes

Serialize the dict yourself with `json.dumps(...)` and cast in SQL using
`CAST(:param AS jsonb)`. The `::jsonb` suffix style is NOT reliable through
the param binder — use `CAST(... AS jsonb)` instead.

```python
import json

db_execute(
    "UPDATE chat_turns "
    "SET qc_audit = COALESCE(qc_audit, '{}'::jsonb) || CAST(:patch AS jsonb) "
    "WHERE correlation_id = :cid",
    "chat",
    params={"patch": json.dumps({"passed": True, "score": 0.92}), "cid": "abc"},
)
```

## Atomic Multi-Statement Writes (Transactions)

When several INSERT/UPDATE/DELETE statements must succeed or fail together
(parent + child rows, stamp + related log, etc.), use `db_transaction`.
Single statements should still use `db_execute`.

```python
from app.db_client import db_transaction

result = db_transaction(
    [
        {
            "sql": "INSERT INTO chat_turns (correlation_id, question) "
                   "VALUES (:cid, :q)",
            "params": {"cid": "abc", "q": "hello"},
        },
        {
            "sql": "INSERT INTO chat_turn_messages (turn_id, thread_id, role, content) "
                   "VALUES (:cid, :tid, 'user', :content)",
            "params": {"cid": "abc", "tid": "t-1", "content": "hello"},
        },
        {
            "sql": "INSERT INTO chat_turn_messages (turn_id, thread_id, role, content) "
                   "VALUES (:cid, :tid, 'assistant', :content)",
            "params": {"cid": "abc", "tid": "t-1", "content": "hi!"},
        },
    ],
    "chat",
)

if "error" in result:
    raise RuntimeError(result["error"]["message"])

# Success shape:
# {
#   "statements_executed": 3,
#   "rows_affected_total": 3,
#   "per_statement": [
#     {"operation": "INSERT", "table": "chat_turns",         "rows_affected": 1},
#     {"operation": "INSERT", "table": "chat_turn_messages", "rows_affected": 1},
#     {"operation": "INSERT", "table": "chat_turn_messages", "rows_affected": 1},
#   ],
# }
```

### Rules

- Only `INSERT` / `UPDATE` / `DELETE` statements are accepted. SELECT returns
  `readonly_violation`; DDL returns `ddl_forbidden`.
- Validation (access control, column-existence) runs on **every** statement
  **before** any statement executes. A manifest or column violation in
  statement N prevents statements 0..N-1 from running.
- On any runtime failure (constraint violation, deadlock, etc.), the entire
  transaction rolls back. The response is
  `{"error": {"code": ..., "message": ..., "statement_index": N}}` so you
  know which statement tripped it.
- No fallback. If the agent is down, the caller must decide whether to
  skip the write or split into individual `db_execute` calls (accepting
  loss of atomicity).

### When not to use it

- Single-statement writes — use `db_execute`, it's lighter.
- Large batch inserts (hundreds of rows) — consider `COPY` via an ops
  script, not the agent.
- Long-running work that spans external HTTP calls — the transaction
  holds a connection; don't block on I/O inside.

## How to Discover Schema

```python
from app.db_client import db_get_schema

tables = db_get_schema("chat")
# {"tables": ["claims", "payments", ...]}

schema = db_get_schema("chat", "claims")
# {"columns": [{"name": "id", "type": "uuid", "nullable": False}, ...]}
```

## Rules

- Use `:param` syntax for all parameters — NEVER f-strings or `%` formatting.
- `db_query`: only `SELECT` / `WITH` / `EXPLAIN`. Writes rejected.
- `db_execute`: `INSERT` (including `ON CONFLICT`), `UPDATE`, `DELETE`, and
  built-in functions like `now()`, `gen_random_uuid()`, `COALESCE` are fine.
  No DDL (`CREATE` / `ALTER` / `DROP` / `TRUNCATE`).
- Each `db_execute` call is **one autocommitted statement**. Multi-statement
  transactions are not currently supported — if you need them, ask before
  proceeding.
- Only access tables declared in your manifest — others return Access Denied.
- Check for `"error"` key in every response before using the data.
- Do NOT import `psycopg2`, `asyncpg`, `sqlalchemy`, or `create_engine`.
- Do NOT read `DATABASE_URL`, `CHAT_RAG_DATABASE_URL`, or any DB connection
  env vars.
- Database names: `chat` (clinical + tasks + chat), `rag` (documents +
  embeddings), `user` (auth + sessions), `qa` (test data).

## Return Value Types

Values in `result["rows"][i][j]` come back as native Python types where the
driver supports it:

| Postgres type      | Python type in `rows`            |
|--------------------|----------------------------------|
| `text`, `varchar`  | `str`                            |
| `integer`, `bigint`| `int`                            |
| `numeric`, `float` | `float` (or `decimal.Decimal` for high-precision) |
| `boolean`          | `bool`                           |
| `uuid`             | `str` (stringified)              |
| `timestamp`, `timestamptz` | ISO-8601 `str`           |
| `date`             | ISO-8601 `str` (`YYYY-MM-DD`)    |
| `jsonb`, `json`    | `dict` or `list` (already decoded) — but defensively handle `str` too, see below |
| `bytea`            | base64-encoded `str`             |
| `NULL`             | `None`                           |

**JSONB defensive branch:** in rare cases (fallback path, older drivers)
`jsonb` comes back as a `str` containing JSON. Safe pattern:

```python
raw = row[0]
if isinstance(raw, str):
    raw = json.loads(raw)
# raw is now a dict/list
```

## Error Model

Errors are structured:

```python
{"error": {"code": "relation_missing", "table": "foo", "message": "relation \"foo\" does not exist"}}
```

Error codes you may encounter:

| `code`                  | Meaning                                             |
|-------------------------|-----------------------------------------------------|
| `access_denied`         | Table not in your manifest                          |
| `relation_missing`      | Table / view doesn't exist                          |
| `column_missing`        | Column in INSERT/UPDATE doesn't exist               |
| `readonly_violation`    | Write statement passed to `db_query`                |
| `ddl_forbidden`         | DDL statement attempted                             |
| `row_limit_exceeded`    | SELECT returned more than `max_rows` (data truncated, `truncated: true`) |
| `timeout`               | Query exceeded `timeout_seconds`                    |
| `syntax_error`          | Bad SQL                                             |
| `integrity_violation`   | Unique / FK / check constraint failed               |
| `connection_error`      | Pool unavailable / DB unreachable                   |
| `internal`              | Anything else — `message` has details               |

Switch on `error["code"]`, not on substrings in `message`. Message strings
are for humans and may change.

## What the Agent Validates

Before your query runs, the db-agent checks:

1. Is your SQL shape allowed? (`SELECT` for query, `INSERT/UPDATE/DELETE` for execute)
2. Does your manifest permit this table?
3. Do the columns you're writing to actually exist?
4. Is your query within `max_rows` / `timeout_seconds` limits?

On failure you get `{"error": {"code": ..., "message": ...}}`.

## Fallback

If the db-agent is down, `db_query` and `db_execute` automatically fall back
to direct psycopg2 using standard env vars. Callers don't need to handle
this; a `"_fallback": true` flag appears in the response for ops alerting.
Ignore it in application code.

---

## Full worked example — before / after

Existing psycopg2 code in `app/storage/feedback.py`:

```python
import psycopg2
from app.chat_config import get_chat_config

def insert_feedback(correlation_id: str, rating: str, comment: str | None) -> None:
    conn = psycopg2.connect(get_chat_config().rag.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_feedback (correlation_id, rating, comment, created_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (correlation_id) DO UPDATE SET
                    rating = EXCLUDED.rating,
                    comment = EXCLUDED.comment,
                    created_at = now()
                """,
                (correlation_id, rating, (comment or "").strip() or None),
            )
        conn.commit()
    finally:
        conn.close()
```

Refactored:

```python
from app.db_client import db_execute

def insert_feedback(correlation_id: str, rating: str, comment: str | None) -> None:
    result = db_execute(
        """
        INSERT INTO chat_feedback (correlation_id, rating, comment, created_at)
        VALUES (:cid, :rating, :comment, now())
        ON CONFLICT (correlation_id) DO UPDATE SET
            rating = EXCLUDED.rating,
            comment = EXCLUDED.comment,
            created_at = now()
        """,
        "chat",
        params={
            "cid": correlation_id,
            "rating": rating,
            "comment": (comment or "").strip() or None,
        },
    )
    if "error" in result:
        raise RuntimeError(result["error"]["message"])
```

Changes: `%s` → `:name`, tuple → dict, `psycopg2.connect / cursor / commit`
gone, explicit error check added. No connection lifecycle code anywhere.
