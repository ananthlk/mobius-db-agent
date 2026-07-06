"""Tests for app.errors — structured error model and SQLSTATE classification."""
from app.errors import classify_db_exception, make_error


class TestMakeError:
    def test_basic_shape(self):
        err = make_error("access_denied", "not allowed")
        assert err == {"error": {"code": "access_denied", "message": "not allowed"}}

    def test_extras_included(self):
        err = make_error("column_missing", "bad col", table="foo", column="x")
        assert err["error"]["code"] == "column_missing"
        assert err["error"]["table"] == "foo"
        assert err["error"]["column"] == "x"

    def test_none_extras_stripped(self):
        err = make_error("internal", "oops", table=None, sqlstate=None)
        assert "table" not in err["error"]
        assert "sqlstate" not in err["error"]

    def test_code_stable_for_switching(self):
        err = make_error("timeout", "cancelled")
        assert err["error"]["code"] == "timeout"


class _FakePsycopgError(Exception):
    """Stand-in for psycopg2 errors that carries pgcode and optional diag."""
    def __init__(self, message: str, pgcode: str | None = None, diag=None):
        super().__init__(message)
        self.pgcode = pgcode
        if diag is not None:
            self.diag = diag


class _Diag:
    def __init__(self, table_name=None, column_name=None):
        self.table_name = table_name
        self.column_name = column_name


class TestClassifyDbException:
    def test_unique_violation(self):
        exc = _FakePsycopgError("duplicate key value", pgcode="23505")
        code, extras = classify_db_exception(exc)
        assert code == "integrity_violation"
        assert extras["sqlstate"] == "23505"

    def test_undefined_table(self):
        exc = _FakePsycopgError("relation \"foo\" does not exist", pgcode="42P01",
                                diag=_Diag(table_name="foo"))
        code, extras = classify_db_exception(exc)
        assert code == "relation_missing"
        assert extras["table"] == "foo"

    def test_undefined_column(self):
        exc = _FakePsycopgError("column x does not exist", pgcode="42703",
                                diag=_Diag(column_name="x"))
        code, extras = classify_db_exception(exc)
        assert code == "column_missing"
        assert extras["column"] == "x"

    def test_statement_timeout(self):
        exc = _FakePsycopgError("canceling statement due to statement timeout",
                                pgcode="57014")
        code, _ = classify_db_exception(exc)
        assert code == "timeout"

    def test_connection_failure(self):
        exc = _FakePsycopgError("connection refused", pgcode="08006")
        code, _ = classify_db_exception(exc)
        assert code == "connection_error"

    def test_syntax_error(self):
        exc = _FakePsycopgError("syntax error at or near SELCT", pgcode="42601")
        code, _ = classify_db_exception(exc)
        assert code == "syntax_error"

    def test_foreign_key_violation(self):
        exc = _FakePsycopgError("FK fail", pgcode="23503")
        code, _ = classify_db_exception(exc)
        assert code == "integrity_violation"

    def test_deadlock_detected(self):
        exc = _FakePsycopgError("deadlock", pgcode="40P01")
        code, _ = classify_db_exception(exc)
        assert code == "integrity_violation"

    # Heuristic fallback when driver doesn't surface SQLSTATE
    def test_heuristic_relation_missing(self):
        exc = Exception('relation "foo" does not exist')
        code, _ = classify_db_exception(exc)
        assert code == "relation_missing"

    def test_heuristic_connection_refused(self):
        exc = Exception("could not connect to server: Connection refused")
        code, _ = classify_db_exception(exc)
        assert code == "connection_error"

    def test_unknown_falls_through_to_internal(self):
        exc = Exception("something weird happened")
        code, _ = classify_db_exception(exc)
        assert code == "internal"

    def test_sqlalchemy_wrapper(self):
        """SQLAlchemy exceptions wrap the driver exc in .orig — we should
        unwrap and read pgcode from there."""
        class _SAWrapper(Exception):
            pass
        wrapper = _SAWrapper("integrity error")
        wrapper.orig = _FakePsycopgError("duplicate", pgcode="23505",
                                         diag=_Diag(table_name="t"))
        code, extras = classify_db_exception(wrapper)
        assert code == "integrity_violation"
        assert extras["sqlstate"] == "23505"
        assert extras["table"] == "t"
