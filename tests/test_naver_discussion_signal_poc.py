from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.invest_stock_detail import StockDetailDiscussionSignal
from app.services.invest_view_model.naver_discussion_signal_poc import (
    build_naver_discussion_signal_poc,
)
from app.services.invest_view_model.stock_detail_service import (
    StockDetailProviders,
    build_stock_detail,
)
from app.services.invest_view_model.stock_detail_symbol_resolver import ResolvedSymbol


@pytest.mark.asyncio
async def test_kr_returns_aggregate_signal_fixture():
    result = await build_naver_discussion_signal_poc("kr", "005930", None)
    assert isinstance(result, StockDetailDiscussionSignal)
    assert result.liveFetchEnabled is False
    assert result.market == "kr"
    assert result.naverCode == "005930"
    assert result.activityRank is not None
    assert result.momentum in {"rising", "flat", "falling", "unknown"}
    assert all(
        "post_text" not in m.label and "title" not in m.label for m in result.metrics
    )


@pytest.mark.asyncio
async def test_us_returns_no_go_pending_review():
    result = await build_naver_discussion_signal_poc("us", "AAPL", None)
    assert isinstance(result, StockDetailDiscussionSignal)
    assert result.status == "no_go_pending_review"
    assert result.liveFetchEnabled is False
    assert result.activityRank is None


@pytest.mark.asyncio
async def test_crypto_returns_none():
    result = await build_naver_discussion_signal_poc("crypto", "KRW-BTC", None)
    assert result is None


@pytest.mark.asyncio
async def test_build_stock_detail_wires_discussion_signal_for_kr():
    async def resolve_kr(market, raw_symbol, db):
        return ResolvedSymbol(
            symbol_db="005930",
            display_name="삼성전자",
            exchange="KOSPI",
            instrument_type="equity_kr",
            asset_type="equity",
            asset_category="kr_stock",
            currency="KRW",
        )

    response = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="005930",
        db=SimpleNamespace(),
        providers=StockDetailProviders(resolver=resolve_kr),
    )

    assert response.discussionSignal is not None
    assert response.discussionSignal.liveFetchEnabled is False
    assert response.discussionSignal.market == "kr"


@pytest.mark.asyncio
async def test_build_stock_detail_omits_discussion_signal_for_crypto():
    async def resolve_crypto(market, raw_symbol, db):
        return ResolvedSymbol(
            symbol_db="KRW-BTC",
            display_name="비트코인",
            exchange="Upbit",
            instrument_type="crypto",
            asset_type="crypto",
            asset_category="crypto",
            currency="KRW",
        )

    response = await build_stock_detail(
        user_id=1,
        market="crypto",
        symbol="KRW-BTC",
        db=SimpleNamespace(),
        providers=StockDetailProviders(resolver=resolve_crypto),
    )

    assert response.discussionSignal is None
