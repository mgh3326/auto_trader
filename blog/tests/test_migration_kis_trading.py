import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.images.kis_trading_images import KISTradingImages


def _generator() -> KISTradingImages:
    return KISTradingImages("kis_trading")


def _assert_valid_svg(svg: str) -> None:
    assert svg.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "</svg>" in svg


def test_create_thumbnail_svg_and_content() -> None:
    svg = _generator().create_thumbnail()
    _assert_valid_svg(svg)
    assert "KIS" in svg
    assert 'width="1200"' in svg
    assert 'height="630"' in svg


def test_create_architecture_svg_and_content() -> None:
    svg = _generator().create_architecture()
    _assert_valid_svg(svg)
    assert "아키텍처" in svg
    assert "FastAPI" in svg
    assert "Celery" in svg
    assert "Telegram" in svg


def test_create_buy_flow_svg_and_content() -> None:
    svg = _generator().create_buy_flow()
    _assert_valid_svg(svg)
    assert "분할 매수" in svg
    assert "1% 조건" in svg
    assert "조건 필터링" in svg


def test_create_erd_svg_and_content() -> None:
    svg = _generator().create_erd()
    _assert_valid_svg(svg)
    assert "ERD" in svg
    assert "users" in svg
    assert "symbol_trade_settings" in svg
    assert "UNIQUE(user_id, symbol)" in svg


def test_create_dashboard_svg_and_content() -> None:
    svg = _generator().create_dashboard()
    _assert_valid_svg(svg)
    assert "Auto Trader" in svg
    assert "KIS 국내주식 자동 매매" in svg
    assert "보유 종목" in svg


def test_create_progress_svg_and_content() -> None:
    svg = _generator().create_progress()
    _assert_valid_svg(svg)
    assert "실시간 진행 상황" in svg
    assert "실행 로그" in svg
    assert "70%" in svg


def test_create_flower_svg_and_content() -> None:
    svg = _generator().create_flower()
    _assert_valid_svg(svg)
    assert "Flower" in svg
    assert "Celery monitoring" in svg
    assert "Recent Tasks" in svg
