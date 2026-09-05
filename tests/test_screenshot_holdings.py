"""
Screenshot Holdings Service Tests

Tests for screenshot-based holdings update service and MCP tool.
"""

from app.mcp_server.tooling.registry import register_all_tools


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


class DummySessionManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return None


def build_tools() -> dict[str, object]:
    mcp = DummyMCP()
    register_all_tools(mcp)
    return mcp.tools
