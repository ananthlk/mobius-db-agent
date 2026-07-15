# Org-docs chunk schema — RAG-owned, db-agent-applied

This directory is the handshake point between RAG (schema OWNER) and the
db-agent provisioner (schema APPLIER). See docs/instant-rag-vault-proposal.md
Appendix B in the superproject.

**Contract:**
- RAG delivers the org-docs chunk/embedding schema as versioned migration
  files here: `vNNN_short_name.sql` (e.g. `v001_chunks.sql`). Plain SQL,
  unqualified table names — the provisioner pins `search_path` to the target
  org's namespace when applying, so one file serves every org.
- Files are applied in version order. Applied versions are tracked per
  namespace in `mobius_org_docs.public.org_docs_namespaces.schema_version`.
- Re-provisioning an org applies any versions newer than its recorded one —
  `POST /doc-store/provision` doubles as the fleet-wide schema upgrade.
- Never edit a shipped version; add a new `vNNN+1` file.
- No out-of-band DDL against org namespaces. Everything goes through here.

**Delivery mechanism (ratified 2026-07-15): VENDORING.** RAG's canonical
source is `mobius-rag/schemas/org_docs/v{N}/schema.sql`; files here are
vendored copies produced by `scripts/sync_org_docs_schema.py` (provenance
header carries the source sha). Never edit vendored files by hand. Rationale:
the provisioner needs DDL with zero runtime dependency on RAG's repo/service
(no sibling checkout on Cloud Run; onboarding must survive RAG being down).
Drift is caught at the contract layer: RAG's deploy-time provision call
asserts its expected `schema_version`; a provisioner shipping an older copy
returns 409 `schema_unavailable` instead of silently serving stale DDL.

**v1 is vendored** (org_documents + org_chunks + HNSW cosine + BM25 GIN +
sentinel table). Two provisioner-side guarantees RAG files rely on:
`CREATE EXTENSION vector` is handled at DB level in `public` (a per-file
IF NOT EXISTS no-ops), and files are applied with
`search_path = "<namespace>", public` so extension types resolve while new
objects land in the namespace.
