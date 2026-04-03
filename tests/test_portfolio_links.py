from app.core.portfolio_links import build_position_detail_url


def test_build_position_detail_url_for_supported_markets() -> None:
    assert (
        build_position_detail_url("035720", "kr")
        == "https://mgh3326.duckdns.org/portfolio/positions/kr/035720"
    )
    assert (
        build_position_detail_url("NVDA", "us")
        == "https://mgh3326.duckdns.org/portfolio/positions/us/NVDA"
    )
    assert (
        build_position_detail_url("KRW-BTC", "crypto")
        == "https://mgh3326.duckdns.org/portfolio/positions/crypto/KRW-BTC"
    )


def test_build_position_detail_url_returns_none_for_unknown_market_or_blank_symbol() -> (
    None
):
    assert build_position_detail_url("", "kr") is None
    assert build_position_detail_url(None, "kr") is None
    assert build_position_detail_url("7203", "jp") is None
