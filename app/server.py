"""MCP server for centralised database access — Mobius DB Agent."""
import logging
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.config import DbAgentConfig
from app.pools import PoolManager
from app.access import AccessControl
from app.contracts import ContractValidator

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_PORT = int(os.environ.get("DB_AGENT_PORT", os.environ.get("PORT", "8008")))
_HOST = os.environ.get("HOST", "0.0.0.0")

mcp = FastMCP(
    "Mobius DB Agent",
    json_response=True,
    stateless_http=True,
    host=_HOST,
    port=_PORT,
)

# ---------------------------------------------------------------------------
# Singletons (initialised at import time, before tools register)
# ---------------------------------------------------------------------------

config = DbAgentConfig.from_env()
pool_manager = PoolManager(config)
access_control = AccessControl(config.manifests_dir, allow_admin=config.allow_admin)
contract_validator = ContractValidator(config.dbt_project_root)

logger.info(
    "Mobius DB Agent initialised — databases: %s, callers: %s",
    pool_manager.available_databases(),
    access_control.known_callers(),
)

# Import tools to trigger @mcp.tool() registration
import app.tools.pg_tools  # noqa: E402, F401
import app.metrics  # noqa: E402, F401
