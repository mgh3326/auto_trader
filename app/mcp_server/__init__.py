"""auto_trader MCP server (Market Data Tools)

This package exposes a small set of read-only tools over MCP (HTTP/SSE).
"""

from app.mcp_server.tools import register_tools

__all__ = ["register_tools"]

try:
    from app.mcp_server.tools import screen_stocks

    __all__.append("screen_stocks")
except ImportError:
    # screen_stocks may have dependencies that are not installed (e.g., dart_fss)
    pass
