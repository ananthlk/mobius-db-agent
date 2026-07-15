"""Org doc-store provisioner — the infrastructure leg of instant-RAG's
3-tier visibility model (docs/instant-rag-vault-proposal.md Appendix B,
db-agent acceptance 2026-07-10).

Physical model (v1, kind=shared_namespace): ONE database ``mobius_org_docs``
on the existing instance, one Postgres SCHEMA per org (``namespace_ref`` =
schema name). Disjoint from mobius_rag by construction: PUBLIC connect is
revoked on the database, so only explicitly granted roles reach it — the
global-corpus service role is never granted.

RAG defines what lives inside each namespace as VERSIONED migration files
(``org_docs_schema/vNNN_*.sql``); this module applies them per namespace and
tracks ``schema_version`` in the control table. Re-provisioning an existing
namespace applies pending versions — provision doubles as the fleet-wide
schema-upgrade mechanism. The directory ships EMPTY (schema_version 0) until
RAG delivers v1; hot-swap is just dropping files in.

``created`` in the provision result is authoritative here: true iff this
call physically created the namespace. Roster echoes it up to onboarding
unchanged ("was a new DB created" signal).
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

ORG_DOCS_DB = "mobius_org_docs"
V1_KIND = "shared_namespace"

# Accepts both customer slugs (hyphenated: david-lawrence-center) and payor
# lexicon keys (underscored: sunshine_health). Same grammar the org master
# enforces, plus underscore. 48-char cap keeps the derived schema name well
# under Postgres's 63-byte identifier limit with the org_ prefix.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_MIGRATION_RE = re.compile(r"^v(\d+)_[\w-]+\.sql$")

_CONTROL_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS org_docs_namespaces (
    org_slug        text PRIMARY KEY,
    namespace       text NOT NULL UNIQUE,
    kind            text NOT NULL DEFAULT 'shared_namespace',
    schema_version  int  NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
)
"""


class ProvisionError(Exception):
    """Provisioning failure with a structured-error code (app/errors.py taxonomy)."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def validate_slug(org_slug: str) -> str:
    slug = (org_slug or "").strip()
    if not _SLUG_RE.match(slug):
        raise ProvisionError(
            "invalid_input",
            "org_slug must match ^[a-z0-9][a-z0-9_-]{0,47}$ "
            "(lowercase; hyphenated customer slug or underscored payor key)",
            org_slug=org_slug,
        )
    return slug


def namespace_for(org_slug: str) -> str:
    """Schema name for an org. Hyphens fold to underscores (Postgres
    identifiers), so distinct slugs CAN collide (a-b vs a_b) — the control
    table's UNIQUE(namespace) makes that loud instead of silent."""
    return "org_" + org_slug.replace("-", "_")


def discover_migrations(schema_dir: Path) -> list[tuple[int, Path]]:
    """RAG-delivered migration files, ordered by version. Empty dir = v0 stub."""
    if not schema_dir.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for f in schema_dir.iterdir():
        m = _MIGRATION_RE.match(f.name)
        if m:
            found.append((int(m.group(1)), f))
    found.sort(key=lambda t: t[0])
    versions = [v for v, _ in found]
    if len(versions) != len(set(versions)):
        raise ProvisionError(
            "invalid_input",
            f"duplicate migration version in {schema_dir}: {versions}",
        )
    return found


@dataclass
class ProvisionResult:
    namespace_ref: str
    created: bool
    status: str
    schema_version: int

    def as_dict(self) -> dict:
        return {
            "namespace_ref": self.namespace_ref,
            "created": self.created,
            "status": self.status,
            "schema_version": self.schema_version,
        }


class OrgDocsProvisioner:
    """Idempotent create-if-absent of the org-docs DB, per-org namespaces,
    and RAG-schema application. Sync, low-traffic (onboarding path) — one
    small lazily-created engine per target, never per-org pools."""

    def __init__(
        self,
        admin_url: str,
        org_docs_url: str,
        schema_dir: Path,
    ) -> None:
        self._admin_url = admin_url          # postgres db — CREATE DATABASE needs autocommit
        self._org_docs_url = org_docs_url    # mobius_org_docs
        self._schema_dir = schema_dir
        self._admin_engine: Engine | None = None
        self._engine: Engine | None = None
        self._db_ensured = False
        self._lock = threading.Lock()        # serialise DDL; onboarding is not a hot path

    # -- engines --------------------------------------------------------

    def _admin(self) -> Engine:
        if self._admin_engine is None:
            if not self._admin_url:
                raise ProvisionError("invalid_input", "no admin DB URL configured (DB_AGENT_ADMIN_URL)")
            self._admin_engine = create_engine(
                self._admin_url, isolation_level="AUTOCOMMIT",
                pool_size=1, max_overflow=1, pool_pre_ping=True,
            )
        return self._admin_engine

    def _org_docs(self) -> Engine:
        if self._engine is None:
            if not self._org_docs_url:
                raise ProvisionError("invalid_input", "no org-docs DB URL configured (DB_AGENT_ORG_DOCS_URL)")
            self._engine = create_engine(
                self._org_docs_url, isolation_level="AUTOCOMMIT",
                pool_size=1, max_overflow=2, pool_pre_ping=True, pool_recycle=300,
            )
        return self._engine

    # -- database + control table --------------------------------------

    def _ensure_database(self) -> None:
        if self._db_ensured:
            return
        with self._admin().connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": ORG_DOCS_DB}
            ).scalar()
            if not exists:
                # CREATE DATABASE cannot be parameterised; name is a constant.
                conn.execute(text(f'CREATE DATABASE "{ORG_DOCS_DB}"'))
                logger.info("created database %s", ORG_DOCS_DB)
            # Disjoint-grants boundary: nobody connects by default. Service
            # roles are granted explicitly; the global-corpus role never is.
            conn.execute(text(f'REVOKE CONNECT ON DATABASE "{ORG_DOCS_DB}" FROM PUBLIC'))
        with self._org_docs().connect() as conn:
            conn.execute(text(_CONTROL_TABLE_DDL))
            # NOLOGIN umbrella role for org-docs readers/writers; prod service
            # users get membership. Idempotent.
            role_exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'mobius_org_docs_rw'")
            ).scalar()
            if not role_exists:
                conn.execute(text("CREATE ROLE mobius_org_docs_rw NOLOGIN"))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{ORG_DOCS_DB}" TO mobius_org_docs_rw'))
        self._db_ensured = True

    # -- schema migrations ----------------------------------------------

    def _apply_migrations(self, conn: Connection, namespace: str, from_version: int) -> int:
        """Apply RAG migrations newer than from_version inside the namespace.
        Returns the resulting schema_version."""
        current = from_version
        for version, path in discover_migrations(self._schema_dir):
            if version <= current:
                continue
            sql = path.read_text()
            # Namespace-scoped: RAG's files use unqualified names; search_path
            # pins them to this org's schema.
            conn.execute(text(f'SET search_path TO "{namespace}"'))
            try:
                conn.execute(text(sql))
            finally:
                conn.execute(text("SET search_path TO public"))
            current = version
            logger.info("applied org_docs schema v%d to %s (%s)", version, namespace, path.name)
        return current

    # -- public API -------------------------------------------------------

    def provision(self, org_slug: str, kind: str = V1_KIND) -> ProvisionResult:
        slug = validate_slug(org_slug)
        if kind != V1_KIND:
            raise ProvisionError(
                "invalid_input",
                f"kind '{kind}' not supported in v1 (only '{V1_KIND}'; "
                "dedicated_db is deferred to the HIPAA tier)",
                kind=kind,
            )
        namespace = namespace_for(slug)

        with self._lock:
            self._ensure_database()
            with self._org_docs().connect() as conn:
                row = conn.execute(
                    text("SELECT namespace, schema_version FROM org_docs_namespaces WHERE org_slug = :s"),
                    {"s": slug},
                ).fetchone()

                if row is not None and row[0] != namespace:
                    raise ProvisionError(
                        "integrity_violation",
                        f"org '{slug}' already mapped to namespace '{row[0]}' (≠ derived '{namespace}')",
                    )

                created = row is None
                if created:
                    # UNIQUE(namespace) turns a slug-fold collision
                    # (a-b vs a_b) into a loud integrity error.
                    conn.execute(
                        text(
                            "INSERT INTO org_docs_namespaces (org_slug, namespace, kind) "
                            "VALUES (:s, :n, :k)"
                        ),
                        {"s": slug, "n": namespace, "k": kind},
                    )
                    conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{namespace}"'))
                    conn.execute(text(f'GRANT USAGE ON SCHEMA "{namespace}" TO mobius_org_docs_rw'))
                    logger.info("provisioned namespace %s for org %s", namespace, slug)

                from_version = 0 if created else int(row[1])
                schema_version = self._apply_migrations(conn, namespace, from_version)
                if schema_version != from_version or created:
                    conn.execute(
                        text(
                            "UPDATE org_docs_namespaces SET schema_version = :v, updated_at = now() "
                            "WHERE org_slug = :s"
                        ),
                        {"v": schema_version, "s": slug},
                    )

        return ProvisionResult(
            namespace_ref=namespace, created=created,
            status="ready", schema_version=schema_version,
        )

    def get(self, org_slug: str) -> dict | None:
        """Current descriptor for an org, or None. Read-only debug/serve aid;
        roster's org_doc_store registry is the system of record."""
        slug = validate_slug(org_slug)
        self._ensure_database()
        with self._org_docs().connect() as conn:
            row = conn.execute(
                text(
                    "SELECT namespace, kind, schema_version, created_at, updated_at "
                    "FROM org_docs_namespaces WHERE org_slug = :s"
                ),
                {"s": slug},
            ).fetchone()
        if row is None:
            return None
        return {
            "org_slug": slug,
            "namespace_ref": row[0],
            "kind": row[1],
            "status": "ready",
            "schema_version": int(row[2]),
            "created_at": row[3].isoformat(),
            "updated_at": row[4].isoformat(),
        }
