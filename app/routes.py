"""REST routes on the db-agent HTTP app (alongside /mcp).

Service-to-service auth: X-Internal-Key header checked against
DB_AGENT_INTERNAL_KEY (Secret Manager in deployed envs, same pattern as
mobius-user). If the env is UNSET the check is skipped with a warning —
local-dev convenience only; deployed environments must set it.
"""
from __future__ import annotations

import hmac
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.errors import classify_db_exception, make_error
from app.org_docs import OrgDocsProvisioner, ProvisionError, V1_KIND
from app.server import config, mcp

logger = logging.getLogger(__name__)

provisioner = OrgDocsProvisioner(
    admin_url=config.admin_url,
    org_docs_url=config.org_docs_url,
    schema_dir=config.org_docs_schema_dir,
)

_warned_no_key = False

_HTTP_BY_CODE = {
    "invalid_input": 400,
    "access_denied": 403,
    "integrity_violation": 409,
    "connection_error": 503,
    "timeout": 503,
}


def _auth_ok(request: Request) -> bool:
    global _warned_no_key
    if not config.internal_key:
        if not _warned_no_key:
            logger.warning(
                "DB_AGENT_INTERNAL_KEY unset — REST routes UNAUTHENTICATED (dev only)"
            )
            _warned_no_key = True
        return True
    supplied = request.headers.get("x-internal-key", "")
    return hmac.compare_digest(supplied, config.internal_key)


def _error_response(code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(make_error(code, message, **extra), status_code=_HTTP_BY_CODE.get(code, 500))


@mcp.custom_route("/doc-store/provision", methods=["POST"])
async def provision_doc_store(request: Request) -> JSONResponse:
    """Idempotent create-if-absent of an org's doc-store namespace.

    Body:    {"org_slug": "...", "kind": "shared_namespace"}
    Returns: {"namespace_ref", "created", "status", "schema_version"}
    `created` is authoritative here and echoes up through roster to
    onboarding as the "was a new store created" signal.
    """
    if not _auth_ok(request):
        return _error_response("access_denied", "missing or wrong X-Internal-Key")
    try:
        body = await request.json()
    except Exception:
        return _error_response("invalid_input", "body must be JSON: {org_slug, kind?}")
    org_slug = (body.get("org_slug") or "").strip()
    kind = (body.get("kind") or V1_KIND).strip()

    try:
        result = provisioner.provision(org_slug, kind)
        return JSONResponse(result.as_dict())
    except ProvisionError as exc:
        return _error_response(exc.code, str(exc), **exc.extra)
    except Exception as exc:  # driver/infra failures → structured taxonomy
        code, extras = classify_db_exception(exc)
        logger.exception("provision failed for org %s", org_slug)
        return _error_response(code, f"provision failed: {exc}", **extras)


@mcp.custom_route("/doc-store/{org_slug}", methods=["GET"])
async def get_doc_store(request: Request) -> JSONResponse:
    """Debug/read aid — roster's org_doc_store registry is the system of record."""
    if not _auth_ok(request):
        return _error_response("access_denied", "missing or wrong X-Internal-Key")
    org_slug = request.path_params["org_slug"]
    try:
        record = provisioner.get(org_slug)
    except ProvisionError as exc:
        return _error_response(exc.code, str(exc), **exc.extra)
    except Exception as exc:
        code, extras = classify_db_exception(exc)
        return _error_response(code, f"lookup failed: {exc}", **extras)
    if record is None:
        return JSONResponse(
            make_error("relation_missing", f"no doc-store provisioned for org '{org_slug}'"),
            status_code=404,
        )
    return JSONResponse(record)
