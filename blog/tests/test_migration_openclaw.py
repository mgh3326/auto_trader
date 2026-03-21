# pyright: reportMissingImports=false,reportUnknownVariableType=false,reportUnknownMemberType=false,reportUnknownArgumentType=false,reportUnknownParameterType=false
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.images.openclaw_images import OpenClawImages


def _generator() -> OpenClawImages:
    return OpenClawImages("openclaw")


def _assert_valid_svg(svg: str) -> None:
    assert svg.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<svg" in svg
    assert "</svg>" in svg


def test_create_thumbnail_svg_and_content() -> None:
    svg = _generator().create_thumbnail()
    _assert_valid_svg(svg)
    assert "OpenClaw" in svg


def test_create_architecture_svg_and_content() -> None:
    svg = _generator().create_architecture()
    _assert_valid_svg(svg)
    assert "FastAPI" in svg
    assert "OpenClaw" in svg
    assert "파이프라인" in svg


def test_create_ssh_tunnel_svg_and_content() -> None:
    svg = _generator().create_ssh_tunnel()
    _assert_valid_svg(svg)
    assert "SSH" in svg
    assert "터널" in svg
    assert "Raspberry" in svg


def test_create_auth_flow_svg_and_content() -> None:
    svg = _generator().create_auth_flow()
    _assert_valid_svg(svg)
    assert "Callback" in svg
    assert "Auth" in svg
    assert "TOKEN" in svg
