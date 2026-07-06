"""Tests for db_transaction — atomic multi-statement writes."""
import json
from unittest.mock import MagicMock

import pytest


# Shared fixture — matches test_pg_tools setup so we can exercise the
# new tool against the same mock pool manager / access control / contract.

@pytest.fixture(autouse=True)
def _mock_server(monkeypatch, tmp_path):
    manifest = tmp_path / "test-svc.yml"
    manifest.write_text("""\
service: test-svc
permissions:
  testdb:
    read: [allowed_table, users]
    write: [allowed_table, orders]
limits:
  max_rows: 100
  timeout_seconds: 5
""")

    from app.access import AccessControl

    ac = AccessControl(tmp_path, allow_admin=True)

    # Stub the contract validator. The real one issues information_schema
    # SELECTs through the pool — we'd have to fake those rows and they'd
    # count in our execute() recorder, obscuring the thing we're actually
    # testing (statement ordering and commit/rollback).
    cv = MagicMock()
    cv.validate_write.return_value = []

    mock_pm = MagicMock()
    mock_pm.available_databases.return_value = ["testdb"]

    # Connection that records every execute() call and supports commit/rollback.
    class MockConn:
        def __init__(self):
            self.executed: list[tuple[str, dict]] = []
            self.committed = False
            self.rolled_back = False
            self._fail_on_index: int | None = None
            self._fail_exc: Exception | None = None

        def execute(self, sql, params=None):
            idx = len(self.executed)
            self.executed.append((str(sql), params))
            if self._fail_on_index is not None and idx == self._fail_on_index:
                raise self._fail_exc or RuntimeError("boom")
            mock_result = MagicMock()
            mock_result.rowcount = 1
            return mock_result

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    shared_conn = MockConn()
    mock_pm.get_connection.return_value = shared_conn
    mock_pm._mock_conn = shared_conn  # expose for tests

    monkeypatch.setattr("app.tools.pg_tools.pool_manager", mock_pm)
    monkeypatch.setattr("app.tools.pg_tools.access_control", ac)
    monkeypatch.setattr("app.tools.pg_tools.contract_validator", cv)
    return mock_pm


class TestCommit:
    def test_single_statement_commits(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        stmts = [{"sql": "INSERT INTO allowed_table (id) VALUES (:id)",
                  "params": {"id": 1}}]
        result = json.loads(db_transaction(json.dumps(stmts), "testdb", "test-svc"))
        assert result["statements_executed"] == 1
        assert result["rows_affected_total"] == 1
        assert result["per_statement"][0]["operation"] == "INSERT"
        assert result["per_statement"][0]["table"] == "allowed_table"
        assert _mock_server._mock_conn.committed is True
        assert _mock_server._mock_conn.rolled_back is False

    def test_three_statements_one_transaction(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        stmts = [
            {"sql": "INSERT INTO allowed_table (id) VALUES (:a)", "params": {"a": 1}},
            {"sql": "INSERT INTO allowed_table (id) VALUES (:b)", "params": {"b": 2}},
            {"sql": "UPDATE allowed_table SET id=:c WHERE id=:d",
             "params": {"c": 3, "d": 1}},
        ]
        result = json.loads(db_transaction(json.dumps(stmts), "testdb", "test-svc"))
        assert result["statements_executed"] == 3
        assert result["rows_affected_total"] == 3
        assert len(_mock_server._mock_conn.executed) == 3
        assert _mock_server._mock_conn.committed is True


class TestRollback:
    def test_mid_stream_failure_rolls_all_back(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        # Arrange: second statement will raise
        _mock_server._mock_conn._fail_on_index = 1
        _mock_server._mock_conn._fail_exc = RuntimeError("duplicate key")

        stmts = [
            {"sql": "INSERT INTO allowed_table (id) VALUES (:a)", "params": {"a": 1}},
            {"sql": "INSERT INTO allowed_table (id) VALUES (:b)", "params": {"b": 2}},
            {"sql": "INSERT INTO allowed_table (id) VALUES (:c)", "params": {"c": 3}},
        ]
        result = json.loads(db_transaction(json.dumps(stmts), "testdb", "test-svc"))

        assert "error" in result
        assert result["error"]["statement_index"] == 1
        assert _mock_server._mock_conn.committed is False
        assert _mock_server._mock_conn.rolled_back is True


class TestValidationPreflight:
    """All validation must fire BEFORE any statement runs."""

    def test_ddl_in_middle_rejects_whole_batch(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        stmts = [
            {"sql": "INSERT INTO allowed_table (id) VALUES (:a)", "params": {"a": 1}},
            {"sql": "DROP TABLE allowed_table", "params": {}},
        ]
        result = json.loads(db_transaction(json.dumps(stmts), "testdb", "test-svc"))
        assert result["error"]["code"] == "ddl_forbidden"
        assert result["error"]["statement_index"] == 1
        # No execute calls — we gate before opening a connection
        assert _mock_server._mock_conn.executed == []

    def test_select_rejected_as_readonly_violation(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        stmts = [
            {"sql": "INSERT INTO allowed_table (id) VALUES (:a)", "params": {"a": 1}},
            {"sql": "SELECT * FROM allowed_table", "params": {}},
        ]
        result = json.loads(db_transaction(json.dumps(stmts), "testdb", "test-svc"))
        assert result["error"]["code"] == "readonly_violation"
        assert result["error"]["statement_index"] == 1
        assert _mock_server._mock_conn.executed == []

    def test_write_to_unauthorized_table_denied(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        # test-svc can READ 'users' but not WRITE to it.
        stmts = [
            {"sql": "INSERT INTO allowed_table (id) VALUES (:a)", "params": {"a": 1}},
            {"sql": "INSERT INTO users (name) VALUES (:n)", "params": {"n": "x"}},
        ]
        result = json.loads(db_transaction(json.dumps(stmts), "testdb", "test-svc"))
        assert result["error"]["code"] == "access_denied"
        assert result["error"]["statement_index"] == 1
        assert result["error"]["table"] == "users"
        assert _mock_server._mock_conn.executed == []

    def test_empty_statements_rejected(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        result = json.loads(db_transaction("[]", "testdb", "test-svc"))
        assert result["error"]["code"] == "invalid_input"
        assert "non-empty" in result["error"]["message"]

    def test_bad_json_rejected(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        result = json.loads(db_transaction("{not json", "testdb", "test-svc"))
        assert result["error"]["code"] == "invalid_input"

    def test_missing_sql_in_statement_rejected(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        result = json.loads(
            db_transaction(json.dumps([{"params": {}}]), "testdb", "test-svc")
        )
        assert result["error"]["code"] == "invalid_input"
        assert result["error"]["statement_index"] == 0

    def test_unknown_database_rejected(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        stmts = [{"sql": "INSERT INTO allowed_table (id) VALUES (:a)",
                  "params": {"a": 1}}]
        result = json.loads(db_transaction(json.dumps(stmts), "nope", "test-svc"))
        assert result["error"]["code"] == "invalid_input"


class TestStructuredErrors:
    def test_first_statement_fail_reports_index_zero(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        _mock_server._mock_conn._fail_on_index = 0
        _mock_server._mock_conn._fail_exc = RuntimeError("bad")

        stmts = [{"sql": "INSERT INTO allowed_table (id) VALUES (:a)",
                  "params": {"a": 1}}]
        result = json.loads(db_transaction(json.dumps(stmts), "testdb", "test-svc"))
        assert result["error"]["statement_index"] == 0
        assert _mock_server._mock_conn.rolled_back is True

    def test_every_error_has_code_and_message(self, _mock_server):
        from app.tools.pg_tools import db_transaction

        cases = [
            db_transaction("{not json", "testdb", "test-svc"),
            db_transaction("[]", "testdb", "test-svc"),
            db_transaction(json.dumps([{"sql": "DROP TABLE x", "params": {}}]),
                           "testdb", "test-svc"),
            db_transaction(json.dumps([{"sql": "SELECT 1", "params": {}}]),
                           "testdb", "test-svc"),
            db_transaction(json.dumps([{"sql": "INSERT INTO forbidden (x) VALUES (:a)",
                                        "params": {"a": 1}}]),
                           "testdb", "test-svc"),
        ]
        for raw in cases:
            r = json.loads(raw)
            assert isinstance(r.get("error"), dict), raw
            assert "code" in r["error"] and "message" in r["error"], raw
