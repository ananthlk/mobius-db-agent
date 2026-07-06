"""Tests for app.tools.pg_tools — MCP tool functions."""
import json
import os
from unittest.mock import MagicMock, patch

import pytest


# We need to mock the server singletons before importing pg_tools.
# pg_tools imports from app.server at module level.

@pytest.fixture(autouse=True)
def _mock_server(monkeypatch, tmp_path):
    """Patch server singletons so pg_tools can be imported without real DB."""
    # Create a minimal manifest for testing
    manifest = tmp_path / "test-svc.yml"
    manifest.write_text("""\
service: test-svc
permissions:
  testdb:
    read: [allowed_table, users]
    write: [allowed_table]
limits:
  max_rows: 100
  timeout_seconds: 5
""")
    wildcard = tmp_path / "admin-svc.yml"
    wildcard.write_text("""\
service: admin-svc
permissions:
  testdb:
    read: ["*"]
    write: ["*"]
""")

    from app.access import AccessControl
    from app.contracts import ContractValidator

    ac = AccessControl(tmp_path, allow_admin=True)
    cv = ContractValidator()

    # Mock pool manager
    mock_pm = MagicMock()
    mock_pm.available_databases.return_value = ["testdb"]

    # Default mock connection behavior
    mock_conn = MagicMock()
    mock_result = MagicMock()
    mock_result.keys.return_value = ["id", "name"]
    mock_result.fetchmany.return_value = [
        (1, "alice"),
        (2, "bob"),
    ]
    mock_result.rowcount = 1
    mock_conn.execute.return_value = mock_result
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_pm.get_connection.return_value = mock_conn

    monkeypatch.setattr("app.tools.pg_tools.pool_manager", mock_pm)
    monkeypatch.setattr("app.tools.pg_tools.access_control", ac)
    monkeypatch.setattr("app.tools.pg_tools.contract_validator", cv)


class TestDbQuery:
    def test_basic_select(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query("SELECT * FROM allowed_table", "testdb", "test-svc"))
        assert "columns" in result
        assert result["row_count"] == 2
        assert result["truncated"] is False

    def test_rejects_write_sql(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query("DELETE FROM allowed_table", "testdb", "test-svc"))
        assert "error" in result
        assert "db_execute" in result["error"]

    def test_rejects_empty_sql(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query("", "testdb", "test-svc"))
        assert "error" in result

    def test_rejects_unknown_database(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query("SELECT 1", "nonexistent", "test-svc"))
        assert "error" in result
        assert "Unknown database" in result["error"]

    def test_access_denied_for_table(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query("SELECT * FROM forbidden_table", "testdb", "test-svc"))
        assert "error" in result
        assert "Access denied" in result["error"]

    def test_access_denied_for_unknown_caller(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query("SELECT * FROM allowed_table", "testdb", "nobody"))
        assert "error" in result
        assert "Access denied" in result["error"]

    def test_admin_can_read_anything(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query("SELECT * FROM any_table", "testdb", "_admin"))
        assert "columns" in result

    def test_wildcard_service_can_read_anything(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query("SELECT * FROM any_table", "testdb", "admin-svc"))
        assert "columns" in result

    def test_with_statement_allowed(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query(
            "WITH cte AS (SELECT * FROM allowed_table) SELECT * FROM cte",
            "testdb", "test-svc"
        ))
        assert "columns" in result

    def test_explain_allowed(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query("EXPLAIN SELECT * FROM allowed_table", "testdb", "test-svc"))
        assert "columns" in result

    def test_sql_with_comments_stripped(self):
        from app.tools.pg_tools import db_query
        result = json.loads(db_query(
            "-- this is a comment\nSELECT * FROM allowed_table",
            "testdb", "test-svc"
        ))
        assert "columns" in result


class TestDbExecute:
    def test_insert(self):
        from app.tools.pg_tools import db_execute
        result = json.loads(db_execute(
            "INSERT INTO allowed_table (id, name) VALUES (:id, :name)",
            "testdb", "test-svc",
            '{"id": 1, "name": "test"}'
        ))
        assert result["operation"] == "INSERT"
        assert result["table"] == "allowed_table"

    def test_update(self):
        from app.tools.pg_tools import db_execute
        result = json.loads(db_execute(
            "UPDATE allowed_table SET name = :name WHERE id = :id",
            "testdb", "test-svc",
            '{"id": 1, "name": "updated"}'
        ))
        assert result["operation"] == "UPDATE"

    def test_delete(self):
        from app.tools.pg_tools import db_execute
        result = json.loads(db_execute(
            "DELETE FROM allowed_table WHERE id = :id",
            "testdb", "test-svc",
            '{"id": 1}'
        ))
        assert result["operation"] == "DELETE"

    def test_rejects_select(self):
        from app.tools.pg_tools import db_execute
        result = json.loads(db_execute("SELECT * FROM allowed_table", "testdb", "test-svc"))
        assert "error" in result
        assert "db_query" in result["error"]

    def test_rejects_ddl(self):
        from app.tools.pg_tools import db_execute
        result = json.loads(db_execute("DROP TABLE allowed_table", "testdb", "test-svc"))
        assert "error" in result

    def test_write_access_denied(self):
        from app.tools.pg_tools import db_execute
        # test-svc can read 'users' but not write
        result = json.loads(db_execute(
            "INSERT INTO users (name) VALUES ('x')",
            "testdb", "test-svc"
        ))
        assert "error" in result
        assert "Access denied" in result["error"]

    def test_rejects_empty_sql(self):
        from app.tools.pg_tools import db_execute
        result = json.loads(db_execute("", "testdb", "test-svc"))
        assert "error" in result


class TestDbGetSchema:
    def test_list_tables(self):
        from app.tools.pg_tools import db_get_schema
        # Mock the connection to return table list
        import app.tools.pg_tools as mod
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            ("allowed_table",),
            ("users",),
            ("forbidden_table",),
        ]
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mod.pool_manager.get_connection.return_value = mock_conn

        result = json.loads(db_get_schema("testdb", "test-svc"))
        assert "tables" in result
        # test-svc can only read allowed_table and users
        assert "allowed_table" in result["tables"]
        assert "users" in result["tables"]
        assert "forbidden_table" not in result["tables"]

    def test_get_table_columns(self):
        from app.tools.pg_tools import db_get_schema
        import app.tools.pg_tools as mod
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            ("id", "uuid", "NO", None),
            ("name", "text", "YES", None),
        ]
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mod.pool_manager.get_connection.return_value = mock_conn

        result = json.loads(db_get_schema("testdb", "test-svc", "allowed_table"))
        assert result["table"] == "allowed_table"
        assert len(result["columns"]) == 2
        assert result["columns"][0]["name"] == "id"

    def test_access_denied_for_table(self):
        from app.tools.pg_tools import db_get_schema
        result = json.loads(db_get_schema("testdb", "test-svc", "forbidden_table"))
        assert "error" in result
        assert "Access denied" in result["error"]


class TestSqlHelpers:
    def test_is_read_only(self):
        from app.tools.pg_tools import _is_read_only
        assert _is_read_only("SELECT * FROM t") is True
        assert _is_read_only("WITH cte AS (SELECT 1) SELECT * FROM cte") is True
        assert _is_read_only("EXPLAIN SELECT * FROM t") is True
        assert _is_read_only("INSERT INTO t VALUES (1)") is False
        assert _is_read_only("UPDATE t SET x = 1") is False
        assert _is_read_only("DELETE FROM t") is False
        assert _is_read_only("DROP TABLE t") is False

    def test_extract_write_target(self):
        from app.tools.pg_tools import _extract_write_target
        assert _extract_write_target("INSERT INTO tasks (id) VALUES (1)") == ("INSERT", "tasks")
        assert _extract_write_target("UPDATE tasks SET x = 1") == ("UPDATE", "tasks")
        assert _extract_write_target("DELETE FROM tasks WHERE id = 1") == ("DELETE", "tasks")
        assert _extract_write_target("SELECT * FROM tasks") is None
        assert _extract_write_target("DROP TABLE tasks") is None

    def test_extract_read_tables(self):
        from app.tools.pg_tools import _extract_read_tables
        tables = _extract_read_tables("SELECT * FROM users JOIN orders ON users.id = orders.user_id")
        assert "users" in tables
        assert "orders" in tables

    def test_extract_write_columns_insert(self):
        from app.tools.pg_tools import _extract_write_columns
        cols = _extract_write_columns("INSERT INTO t (a, b, c) VALUES (1, 2, 3)", "INSERT")
        assert cols == ["a", "b", "c"]

    def test_extract_write_columns_update(self):
        from app.tools.pg_tools import _extract_write_columns
        cols = _extract_write_columns("UPDATE t SET x = 1, y = 2 WHERE id = 3", "UPDATE")
        assert cols == ["x", "y"]
