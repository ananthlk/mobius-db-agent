"""Run the MCP server: python -m app"""
from app.server import mcp

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
