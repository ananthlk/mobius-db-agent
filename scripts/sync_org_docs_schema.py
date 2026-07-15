#!/usr/bin/env python3
"""Vendor RAG's org_docs schema into this repo (the schema-delivery mechanism).

RAG's canonical source: mobius-rag/schemas/org_docs/v{N}/schema.sql
Vendored target here:   org_docs_schema/v{NNN}_schema.sql (+ provenance header)

Why vendoring (ratified 2026-07-15): the provisioner must be able to apply
DDL with NO runtime dependency on RAG's repo or service (Cloud Run has no
sibling checkout; onboarding must work even if RAG is down). Drift is caught
at the contract layer instead: RAG's deploy-time provision call asserts its
expected schema_version, and a provisioner still shipping an older vendored
copy fails loudly with 409 schema_unavailable.

Run this whenever RAG publishes a new version, then commit + redeploy:
    python scripts/sync_org_docs_schema.py
"""
import re
import subprocess
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
RAG_SCHEMAS = MODULE_DIR.parent / "mobius-rag" / "schemas" / "org_docs"
TARGET_DIR = MODULE_DIR / "org_docs_schema"


def main() -> None:
    if not RAG_SCHEMAS.is_dir():
        sys.exit(f"RAG schema source not found: {RAG_SCHEMAS} (need a mobius-rag sibling checkout)")

    try:
        sha = subprocess.run(
            ["git", "-C", str(RAG_SCHEMAS), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        sha = "unknown"

    synced = []
    for vdir in sorted(RAG_SCHEMAS.iterdir()):
        m = re.match(r"^v(\d+)$", vdir.name)
        src = vdir / "schema.sql"
        if not (m and src.is_file()):
            continue
        version = int(m.group(1))
        target = TARGET_DIR / f"v{version:03d}_schema.sql"
        header = (
            f"-- VENDORED from mobius-rag/schemas/org_docs/{vdir.name}/schema.sql "
            f"(mobius-rag @ {sha})\n"
            f"-- Do NOT edit here — RAG owns this file; re-run scripts/sync_org_docs_schema.py.\n"
        )
        body = src.read_text()
        content = header + body
        if target.is_file() and target.read_text().split("\n", 2)[2:] == content.split("\n", 2)[2:]:
            print(f"v{version}: unchanged")
            continue
        if target.is_file():
            print(f"v{version}: UPDATED — RAG changed a shipped version; verify this is intentional!")
        target.write_text(content)
        synced.append(target.name)

    print(f"synced: {synced or 'nothing new'} (source mobius-rag @ {sha})")


if __name__ == "__main__":
    main()
