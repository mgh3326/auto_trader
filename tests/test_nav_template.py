from __future__ import annotations

from pathlib import Path


def test_nav_contains_portfolio_and_screener_links() -> None:
    nav_html = Path("app/templates/nav.html").read_text(encoding="utf-8")

    assert 'href="/portfolio/"' in nav_html
    assert 'href="/screener"' in nav_html


def test_nav_removes_deprecated_legacy_links() -> None:
    nav_html = Path("app/templates/nav.html").read_text(encoding="utf-8")

    assert 'href="/manual-holdings/"' not in nav_html
    assert 'href="/kis-domestic-trading/"' not in nav_html
    assert 'href="/kis-overseas-trading/"' not in nav_html
    assert 'href="/upbit-trading/"' not in nav_html
    assert 'href="/analysis-json/"' not in nav_html
    assert 'href="/stock-latest/"' not in nav_html
    assert 'href="/orderbook/"' not in nav_html
