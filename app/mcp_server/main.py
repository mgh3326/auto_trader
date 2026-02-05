import logging

from fastmcp import FastMCP

from app.mcp_server.env_utils import _env, _env_int
from app.mcp_server.tools import register_tools


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    mcp_type = _env("MCP_TYPE", "streamable-http")  # stdio | sse | streamable-http
    mcp_host = _env("MCP_HOST", "0.0.0.0")
    mcp_port = _env_int("MCP_PORT", 8765)
    mcp_path = _env("MCP_PATH", "/mcp")

    server = FastMCP(
        name="auto_trader-mcp",
        instructions="Read-only market data tools for auto_trader (symbol search, quotes, OHLCV).",
        version="0.1.0",
        stateless_http=False,
    )

    register_tools(server)

    logging.info(
        f"Starting MCP server: type={mcp_type} host={mcp_host} port={mcp_port} path={mcp_path}"
    )

    if mcp_type == "stdio":
        server.run(transport="stdio")
    elif mcp_type == "sse":
        server.run(transport="sse", host=mcp_host, port=mcp_port, path=mcp_path)
    elif mcp_type == "streamable-http":
        server.run(
            transport="streamable-http", host=mcp_host, port=mcp_port, path=mcp_path
        )
    else:
        raise ValueError(f"Unsupported MCP_TYPE: {mcp_type}")


if __name__ == "__main__":
    main()
