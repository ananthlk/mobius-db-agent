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

The directory ships EMPTY (schema_version 0) — the deliberate stub that lets
the provisioner's DB/namespace/grants leg build and verify ahead of RAG's
schema v1 (Org Agent go-ahead, 2026-07-15). RAG: drop v001 in and every
namespace picks it up on next provision.
