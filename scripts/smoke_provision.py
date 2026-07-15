#!/usr/bin/env python3
"""Live smoke for the org doc-store provisioner against the local proxy.

Proves, against real Postgres: database create-if-absent, control table,
namespace create + idempotency (created true → false), schema_version
tracking with a temp migration, slug-fold collision rejection, and cleanup.

Usage:  python scripts/smoke_provision.py
Env:    CHAT_RAG_DATABASE_URL (or DB_AGENT_ADMIN_URL/DB_AGENT_ORG_DOCS_URL)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import DbAgentConfig  # noqa: E402
from app.org_docs import OrgDocsProvisioner, ProvisionError  # noqa: E402

SLUG = "smoke-test-org"
SLUG_COLLIDER = "smoke_test-org"  # folds to the same namespace


def main() -> None:
    cfg = DbAgentConfig.from_env()
    assert cfg.admin_url and cfg.org_docs_url, "no DB URLs resolved from env"

    with tempfile.TemporaryDirectory() as td:
        schema_dir = Path(td)
        p = OrgDocsProvisioner(cfg.admin_url, cfg.org_docs_url, schema_dir)

        # 1. fresh provision (v0 stub — no migrations yet)
        r1 = p.provision(SLUG)
        assert r1.created is True and r1.schema_version == 0, r1
        print(f"provision (new): PASS {r1.as_dict()}")

        # 2. idempotent re-provision
        r2 = p.provision(SLUG)
        assert r2.created is False and r2.namespace_ref == r1.namespace_ref, r2
        print(f"re-provision (idempotent): PASS {r2.as_dict()}")

        # 3. hot-swap: drop a v1 migration in, re-provision → applied
        (schema_dir / "v001_smoke.sql").write_text(
            "CREATE TABLE smoke_probe (id int PRIMARY KEY, note text)"
        )
        r3 = p.provision(SLUG)
        assert r3.created is False and r3.schema_version == 1, r3
        with p._org_docs().connect() as conn:
            n = conn.execute(text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = :ns AND table_name = 'smoke_probe'"
            ), {"ns": r3.namespace_ref}).scalar()
        assert n == 1, "smoke_probe not created in namespace"
        print(f"schema hot-swap v0→v1: PASS {r3.as_dict()}")

        # 4. slug-fold collision rejected loudly
        try:
            p.provision(SLUG_COLLIDER)
            raise AssertionError("collision not caught")
        except ProvisionError as exc:
            assert exc.code in ("integrity_violation",), exc.code
        except Exception as exc:  # driver-level unique violation also acceptable
            assert "unique" in str(exc).lower() or "duplicate" in str(exc).lower(), exc
        print("slug-fold collision: PASS (rejected)")

        # 5. GET record
        rec = p.get(SLUG)
        assert rec and rec["schema_version"] == 1 and rec["status"] == "ready", rec
        print(f"get: PASS {rec}")

        # 6. cleanup smoke artifacts (namespace + control row); DB itself stays
        with p._org_docs().connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{r1.namespace_ref}" CASCADE'))
            conn.execute(text("DELETE FROM org_docs_namespaces WHERE org_slug = :s"), {"s": SLUG})
        print("cleanup: PASS")

    print("ALL SMOKE PASS — mobius_org_docs live, provisioner verified")


if __name__ == "__main__":
    main()
