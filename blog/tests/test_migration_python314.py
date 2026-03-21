import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.images.python314_images import Python314Images


def _generator() -> Python314Images:
    return Python314Images("python314_upgrade")


def _assert_valid_svg(svg: str) -> None:
    assert svg.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<svg" in svg
    assert "</svg>" in svg


def test_create_version_timeline_svg_and_content() -> None:
    svg = _generator().create_version_timeline()
    _assert_valid_svg(svg)
    assert "Python 3.11" in svg
    assert "Python 3.12" in svg
    assert "Python 3.13" in svg
    assert "Python 3.14" in svg
    assert "타임라인" in svg


def test_create_performance_comparison_svg_and_content() -> None:
    svg = _generator().create_performance_comparison()
    _assert_valid_svg(svg)
    assert "3.13" in svg
    assert "3.14" in svg
    assert "성능" in svg


def test_create_thumbnail_svg_and_content() -> None:
    svg = _generator().create_thumbnail()
    _assert_valid_svg(svg)
    assert "Python 3.14" in svg
