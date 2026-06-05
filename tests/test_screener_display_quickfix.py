"""ROB-435 A (ETF exclusion) + ROB-436 C-1 (absurd market_cap ceiling) quick fixes.

These are pure-function display-fidelity fixes in screener_service:
- _is_kr_toss_common_stock must exclude the KIWOOM / 1Q ETF brands that leaked
  into 연속상승세 (live: KIWOOM 미국S&P500모멘텀, 1Q 미국배당TOP30).
- the KR single-stock plausibility ceiling must hide absurd market caps
  (live: SG&G rendered 3,468.8조원 from a bad tvscreener row).
"""

from __future__ import annotations

import pytest

from app.services.invest_view_model.screener_service import (
    _KR_ABSURD_MARKET_CAP_KRW,
    _format_market_cap,
    _is_kr_toss_common_stock,
)

# --- ROB-435 A: ETF/ETN exclusion ---------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "symbol,name",
    [
        ("0137V0", "KIWOOM 미국S&P500모멘텀"),
        ("0137W0", "KIWOOM 미국S&P500&GOLD"),
        ("0004G0", "1Q 미국배당TOP30"),
        ("069500", "KODEX 200"),  # existing prefix still excluded
        ("005935", "삼성전자우"),  # preferred suffix still excluded
    ],
)
def test_excludes_etf_and_preferred(symbol: str, name: str) -> None:
    assert _is_kr_toss_common_stock(symbol, name) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "symbol,name",
    [
        ("040350", "크레오에스지"),
        ("069960", "현대백화점"),
        ("002350", "넥센타이어"),
    ],
)
def test_keeps_ordinary_common_stocks(symbol: str, name: str) -> None:
    assert _is_kr_toss_common_stock(symbol, name) is True


# --- ROB-436 C-1: absurd market_cap ceiling -----------------------------------


@pytest.mark.unit
def test_absurd_ceiling_is_single_stock_plausible() -> None:
    # ~2,000조 — generous over 삼성전자 (~500조), far below the bad 3,468조 row.
    assert _KR_ABSURD_MARKET_CAP_KRW == 2_000_000_000_000_000


@pytest.mark.unit
def test_hides_absurd_high_market_cap() -> None:
    # 3,468.8조 KRW (the live SG&G bug) → hidden, not rendered as "3,468.8조원".
    label, warnings = _format_market_cap(
        {"market_cap_krw": 3_468_800_000_000_000}, "kr"
    )
    assert label == "-"
    assert warnings  # "시가총액 데이터 확인 필요"
    # same when the value arrives via the generic market_cap field (KRW).
    label2, warnings2 = _format_market_cap({"market_cap": 3_468_800_000_000_000}, "kr")
    assert label2 == "-"
    assert warnings2


@pytest.mark.unit
def test_keeps_plausible_market_cap() -> None:
    # 삼성전자-scale ~500조 stays visible (under the ceiling).
    label, warnings = _format_market_cap({"market_cap_krw": 500_000_000_000_000}, "kr")
    assert label == "500.0조원"
    assert warnings == []
    # a normal mid-cap ~3,000억 renders cleanly (억 uses 0-decimal formatting).
    label2, _ = _format_market_cap({"market_cap_krw": 300_000_000_000}, "kr")
    assert label2 == "3,000억원"
