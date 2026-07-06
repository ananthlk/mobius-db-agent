# mobius-db-agent Integration Spec

**For any module or agent being built or refactored to use centralised database access.**

---

## Quick Start (3 steps)

### 1. Copy the client

Copy `db_client.py` from `mobius-db-agent/` into your module:

```bash
cp mobius-db-agent/db_client.py mobius-skills/your-skill/app/db_client.py
```

### 2. Set env vars

Add to your `.env` or let `mstart` provide them:

```bash
DB_AGENT_MCP_URL=http://localhost:8008/mcp   # set by mstart automatically
DB_AGENT_CALLER_ID=your-service-name          # MUST match your manifest
```

### 3. Use the client

```python
from app.db_client import db_query, db_execute, db_get_schema

# Read
result = db_query("SELECT * FROM mobius_task WHERE status = :s", "chat", params={"s": "open"})
for row in result["rows"]:
    print(dict(zip(result["columns"], row)))

# Write
db_execute(
    "INSERT INTO mobius_task (task_id, type, status) VALUES (:id, :t, :s)",
    "chat",
    params={"id": "abc-123", "t": "review", "s": "open"},
)

# Schema discovery
tables = db_get_schema("chat")                  # {"tables": ["mobius_task", ...]}
cols = db_get_schema("chat", "mobius_task")      # {"columns": [{name, type, nullable}, ...]}
```

---

## What You Must Provide

### A. Access Manifest

Create `mobius-db-agent/manifests/your-service.yml`:

```yaml
service: your-service-name          # must match DB_AGENT_CALLER_ID
permissions:
  chat:                              # database name: chat | rag | user | qa
    read: [table_a, table_b]         # tables you read from
    write: [table_a]                 # tables you write to
  rag:
    read: [documents]
    write: []
limits:
  max_rows: 5000                     # max rows per query (default: 5000)
  timeout_seconds: 15                # statement timeout (default: 15)
```

Use `"*"` for wildcard access to all tables in a database (use sparingly).

### B. DB_AGENT_CALLER_ID

Set in your `.env`:
```
DB_AGENT_CALLER_ID=your-service-name
```

This string MUST exactly match the `service:` field in your manifest YAML.

---

## What You Must NOT Do

| Old Pattern (remove) | New Pattern (use instead) |
|---|---|
| `import psycopg2; conn = psycopg2.connect(url)` | `from app.db_client import db_query, db_execute` |
| `os.environ.get("CHAT_RAG_DATABASE_URL")` | Client resolves this internally for fallback only |
| `create_engine(url); Session()` | `db_query(sql, db_name)` / `db_execute(sql, db_name)` |
| `with get_conn() as conn: conn.execute(...)` | `db_execute(sql, db_name, params={...})` |

Do NOT:
- Open your own connection pools
- Import psycopg2/asyncpg/sqlalchemy for database access
- Hardcode database URLs
- Run DDL (CREATE/ALTER/DROP) — migrations are handled separately
- Access tables not declared in your manifest

---

## API Reference

### `db_query(sql, db_name, params=None, max_rows=1000) -> dict`

Read-only queries (SELECT, WITH, EXPLAIN).

**Parameters:**
- `sql` — SQL string with `:param` placeholders
- `db_name` — `"chat"`, `"rag"`, `"user"`, or `"qa"`
- `params` — dict of parameter values (optional)
- `max_rows` — max rows returned (capped by manifest limits)

**Returns:**
```python
{
    "columns": ["id", "name", "status"],
    "rows": [[1, "alice", "active"], [2, "bob", "pending"]],
    "row_count": 2,
    "truncated": False
}
```

**Errors:** Returns `{"error": "message"}` — check for `"error"` key.

### `db_execute(sql, db_name, params=None) -> dict`

Write operations (INSERT, UPDATE, DELETE). No DDL.

**Returns:**
```python
{
    "operation": "INSERT",
    "table": "mobius_task",
    "rows_affected": 1
}
```

### `db_get_schema(db_name, table="") -> dict`

Schema discovery. No table = list all readable tables. With table = column details.

---

## Databases Available

| Name | Contains | Typical Users |
|------|----------|---------------|
| `chat` | Clinical tables, chat turns, tasks, credentialing, resolution plans | mobius-os, mobius-chat, skills |
| `rag` | Documents, chunks, embeddings, jobs | mobius-rag |
| `user` | Users, sessions, auth providers, roles | mobius-user, mobius-os |
| `qa` | Lexicon, test data, retrieval eval | mobius-qa |

---

## Fallback Behaviour

If the MCP agent is unreachable (connection refused, timeout):
- `db_query` and `db_execute` automatically fall back to direct psycopg2
- Uses `CHAT_RAG_DATABASE_URL` / `DATABASE_URL` env vars
- A `_fallback: true` flag is set in the response
- `db_get_schema` does NOT fall back (requires the agent)

This means your service keeps working even if the db-agent is down.

---

## Migration Checklist

When refactoring an existing module:

- [ ] Copy `db_client.py` into your module
- [ ] Create a manifest YAML in `mobius-db-agent/manifests/`
- [ ] Set `DB_AGENT_CALLER_ID` in your `.env`
- [ ] Replace all `psycopg2.connect()` / `get_conn()` / `create_engine()` calls with `db_query` / `db_execute`
- [ ] Remove database URL env vars from your module's config (keep them for fallback only)
- [ ] Remove connection pool setup code
- [ ] Remove `psycopg2` / `asyncpg` / `sqlalchemy` from your module's `requirements.txt` (they stay in the shared venv)
- [ ] Test with agent running: `mstart` and verify your module works
- [ ] Test fallback: stop the agent (`kill $(lsof -ti:8008)`) and verify your module still works
- [ ] PR: include your manifest YAML alongside your module changes

---

## Example: Migrating task-manager

**Before** (`mobius-skills/task-manager/app/storage/tasks_pg.py`):
```python
from app.db import get_conn

def list_tasks(org_name=None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM mobius_task WHERE org_name = %s", (org_name,))
        rows = cur.fetchall()
        ...
```

**After:**
```python
from app.db_client import db_query

def list_tasks(org_name=None):
    result = db_query(
        "SELECT * FROM mobius_task WHERE org_name = :org",
        "chat",
        params={"org": org_name},
    )
    if "error" in result:
        raise RuntimeError(result["error"])
    return [dict(zip(result["columns"], row)) for row in result["rows"]]
```

---

## Testing Your Integration

```bash
# 1. Start everything
./mstart

# 2. Verify db-agent is running
curl -s http://localhost:8008/mcp | head -c 200

# 3. Test via your module's endpoints
curl http://localhost:YOUR_PORT/your-endpoint

# 4. Check agent logs
tail -f .mobius_logs/mobius-db-agent.log

# 5. Test fallback (stop agent, verify module still works)
kill $(lsof -ti:8008)
curl http://localhost:YOUR_PORT/your-endpoint  # should still work via fallback
```
