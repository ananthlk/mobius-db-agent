"""Health and metrics MCP tool."""
import json

from app.server import mcp, pool_manager


@mcp.tool()
def db_health(caller_id: str = "_admin") -> str:
    """Get health status and pool metrics for all configured databases.

    Args:
        caller_id: Service identifier (default: _admin).
    """
    metrics = pool_manager.get_metrics()
    health = pool_manager.health_check()

    result = {}
    for db_name in pool_manager.available_databases():
        m = metrics.get(db_name)
        result[db_name] = {
            "reachable": health.get(db_name, False),
            "pool_size": m.pool_size if m else 0,
            "checked_out": m.checked_out if m else 0,
            "checked_in": m.checked_in if m else 0,
            "overflow": m.overflow if m else 0,
        }

    return json.dumps({"databases": result})
