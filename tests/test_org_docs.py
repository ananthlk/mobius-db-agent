"""Unit tests for the org doc-store provisioner (pure logic — no DB)."""
import pytest

from app.org_docs import (
    ProvisionError,
    discover_migrations,
    namespace_for,
    validate_slug,
)


class TestValidateSlug:
    def test_customer_hyphenated(self):
        assert validate_slug("david-lawrence-center") == "david-lawrence-center"

    def test_payor_underscored(self):
        assert validate_slug("sunshine_health") == "sunshine_health"

    def test_strips_whitespace(self):
        assert validate_slug("  aetna ") == "aetna"

    @pytest.mark.parametrize("bad", [
        "", "  ", "UPPER", "Sunshine Health", "-leading-hyphen",
        "_leading_underscore", "has.dot", "has/slash", "a" * 49,
        "nonexistent org", "O;DROP TABLE x",
    ])
    def test_rejects(self, bad):
        with pytest.raises(ProvisionError) as ei:
            validate_slug(bad)
        assert ei.value.code == "invalid_input"


class TestNamespaceFor:
    def test_hyphens_fold_to_underscores(self):
        assert namespace_for("david-lawrence-center") == "org_david_lawrence_center"

    def test_underscores_pass_through(self):
        assert namespace_for("sunshine_health") == "org_sunshine_health"

    def test_fold_collision_is_possible_and_must_be_caught_by_unique(self):
        # Documented hazard: distinct slugs, same namespace. The DB-side
        # UNIQUE(namespace) is the guard; this test pins the collision fact.
        assert namespace_for("a-b") == namespace_for("a_b")


class TestDiscoverMigrations:
    def test_empty_or_missing_dir_is_v0_stub(self, tmp_path):
        assert discover_migrations(tmp_path) == []
        assert discover_migrations(tmp_path / "nope") == []

    def test_orders_by_version_not_name(self, tmp_path):
        (tmp_path / "v010_later.sql").write_text("SELECT 10")
        (tmp_path / "v002_first.sql").write_text("SELECT 2")
        (tmp_path / "v001_zeroth.sql").write_text("SELECT 1")
        got = discover_migrations(tmp_path)
        assert [v for v, _ in got] == [1, 2, 10]

    def test_ignores_non_matching_files(self, tmp_path):
        (tmp_path / "README.md").write_text("x")
        (tmp_path / "helper.sql").write_text("x")
        (tmp_path / "v001_real.sql").write_text("SELECT 1")
        got = discover_migrations(tmp_path)
        assert len(got) == 1 and got[0][0] == 1

    def test_duplicate_version_raises(self, tmp_path):
        (tmp_path / "v001_a.sql").write_text("SELECT 1")
        (tmp_path / "v001_b.sql").write_text("SELECT 1")
        with pytest.raises(ProvisionError) as ei:
            discover_migrations(tmp_path)
        assert ei.value.code == "invalid_input"


class TestKindGate:
    def test_dedicated_db_rejected_before_any_db_work(self):
        from app.org_docs import OrgDocsProvisioner
        p = OrgDocsProvisioner(admin_url="", org_docs_url="", schema_dir=None)
        with pytest.raises(ProvisionError) as ei:
            p.provision("aetna", kind="dedicated_db")
        assert ei.value.code == "invalid_input"
        assert "dedicated_db" in str(ei.value)


class TestSchemaVersionAssertion:
    def _p(self, tmp_path):
        from app.org_docs import OrgDocsProvisioner
        return OrgDocsProvisioner(admin_url="", org_docs_url="", schema_dir=tmp_path)

    def test_asserting_newer_than_vendored_fails_before_db_work(self, tmp_path):
        (tmp_path / "v001_schema.sql").write_text("SELECT 1")
        with pytest.raises(ProvisionError) as ei:
            self._p(tmp_path).provision("aetna", schema_version=2)
        assert ei.value.code == "schema_unavailable"
        assert ei.value.extra == {"requested": 2, "available": 1}

    def test_asserting_newer_than_empty_stub_fails(self, tmp_path):
        with pytest.raises(ProvisionError) as ei:
            self._p(tmp_path).provision("aetna", schema_version=1)
        assert ei.value.code == "schema_unavailable"
        assert ei.value.extra["available"] == 0

    def test_kind_gate_still_wins_over_version_check(self, tmp_path):
        with pytest.raises(ProvisionError) as ei:
            self._p(tmp_path).provision("aetna", kind="dedicated_db", schema_version=99)
        assert ei.value.code == "invalid_input"
