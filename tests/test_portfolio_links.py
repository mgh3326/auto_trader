import pytest

from app.core.portfolio_links import build_position_detail_url


@pytest.mark.unit
def test_kr_url_points_to_invest_stocks():
    url = build_position_detail_url("005930", "kr")
    assert url is not None
    assert url.endswith("/invest/stocks/kr/005930")
    assert "/portfolio/positions/" not in url


@pytest.mark.unit
def test_crypto_symbol_encoded():
    url = build_position_detail_url("KRW-BTC", "crypto")
    assert url is not None
    assert url.endswith("/invest/stocks/crypto/KRW-BTC")


@pytest.mark.unit
def test_unknown_market_returns_none():
    assert build_position_detail_url("005930", "bogus") is None
    assert build_position_detail_url("", "kr") is None
