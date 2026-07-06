"""Tests for app.access — manifest loading and permission checks."""
import tempfile
from pathlib import Path

import pytest

from app.access import AccessControl, ServiceLimits


@pytest.fixture
def manifests_dir(tmp_path):
    """Create a temporary manifests directory with test manifests."""
    m1 = tmp_path / "svc-a.yml"
    m1.write_text("""\
service: svc-a
permissions:
  chat:
    read: [table_x, table_y]
    write: [table_x]
  rag:
    read: ["*"]
    write: []
limits:
  max_rows: 2000
  timeout_seconds: 10
""")
    m2 = tmp_path / "svc-b.yml"
    m2.write_text("""\
service: svc-b
permissions:
  chat:
    read: ["*"]
    write: ["*"]
""")
    return tmp_path


class TestAccessControl:
    def test_loads_manifests(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        assert "svc-a" in ac.known_callers()
        assert "svc-b" in ac.known_callers()

    def test_read_allowed(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        assert ac.check_read("svc-a", "chat", "table_x") is True
        assert ac.check_read("svc-a", "chat", "table_y") is True

    def test_read_denied(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        assert ac.check_read("svc-a", "chat", "table_z") is False

    def test_write_allowed(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        assert ac.check_write("svc-a", "chat", "table_x") is True

    def test_write_denied(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        assert ac.check_write("svc-a", "chat", "table_y") is False

    def test_wildcard_read(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        assert ac.check_read("svc-a", "rag", "any_table") is True
        assert ac.check_read("svc-b", "chat", "any_table") is True

    def test_wildcard_write(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        assert ac.check_write("svc-b", "chat", "any_table") is True
        # svc-a has rag write: [] (empty), not wildcard
        assert ac.check_write("svc-a", "rag", "any_table") is False

    def test_unknown_caller_denied(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        assert ac.check_read("unknown", "chat", "table_x") is False
        assert ac.check_write("unknown", "chat", "table_x") is False

    def test_unknown_database_denied(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        assert ac.check_read("svc-a", "nonexistent_db", "table_x") is False

    def test_limits(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        limits = ac.get_limits("svc-a")
        assert limits.max_rows == 2000
        assert limits.timeout_seconds == 10

    def test_default_limits_for_unknown_caller(self, manifests_dir):
        ac = AccessControl(manifests_dir)
        limits = ac.get_limits("unknown")
        assert limits.max_rows == 5000  # default

    def test_admin_bypasses_access(self, manifests_dir):
        ac = AccessControl(manifests_dir, allow_admin=True)
        assert ac.check_read("_admin", "chat", "anything") is True
        assert ac.check_write("_admin", "rag", "anything") is True

    def test_admin_disabled_by_default(self, manifests_dir):
        ac = AccessControl(manifests_dir, allow_admin=False)
        assert ac.check_read("_admin", "chat", "anything") is False

    def test_empty_dir(self, tmp_path):
        ac = AccessControl(tmp_path)
        assert ac.known_callers() == []

    def test_nonexistent_dir(self, tmp_path):
        ac = AccessControl(tmp_path / "nonexistent")
        assert ac.known_callers() == []

    def test_invalid_yaml_skipped(self, tmp_path):
        bad = tmp_path / "bad.yml"
        bad.write_text("not: valid: yaml: [[[")
        ac = AccessControl(tmp_path)
        assert ac.known_callers() == []

    def test_manifest_without_service_key_skipped(self, tmp_path):
        m = tmp_path / "noservice.yml"
        m.write_text("permissions:\n  chat:\n    read: ['*']")
        ac = AccessControl(tmp_path)
        assert ac.known_callers() == []
