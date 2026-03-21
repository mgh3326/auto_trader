import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.images.mcp_server_images import MCPServerImages


def _generator() -> MCPServerImages:
    return MCPServerImages("mcp_server")


def _assert_valid_svg(svg: str) -> None:
    assert svg.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<svg" in svg
    assert "</svg>" in svg


def test_create_architecture_svg_and_content() -> None:
    svg = _generator().create_architecture()
    _assert_valid_svg(svg)
    assert "MCP" in svg
    assert "Claude" in svg
    assert "FastMCP" in svg
    assert "Server" in svg


def test_create_routing_svg_and_content() -> None:
    svg = _generator().create_routing()
    _assert_valid_svg(svg)
    assert "라우팅" in svg
    assert "심볼" in svg
    assert "_resolve_market_type" in svg
    assert "market" in svg


def test_create_thumbnail_svg_and_content() -> None:
    svg = _generator().create_thumbnail()
    _assert_valid_svg(svg)
    assert "MCP" in svg
