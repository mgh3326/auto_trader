"""Unit tests for signals_service."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.invest_view_model.relation_resolver import RelationResolver


def _fake_pair(
    *,
    ana_id: int,
    info_id: int,
    symbol: str,
    name: str,
    itype: str = "domestic",
):
    info = MagicMock()
    info.id = info_id
    info.symbol = symbol
    info.name = name
    info.instrument_type = itype
    ana = MagicMock()
    ana.id = ana_id
    ana.stock_info_id = info_id
    ana.decision = "buy"
    ana.confidence = 80
    ana.detailed_text = "summary"
    ana.reasons = ["r1"]
    ana.created_at = datetime(2026, 5, 1, tzinfo=UTC)
    return ana, info


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signals_mine_filters_to_held() -> None:
    from app.services.invest_view_model.signals_service import build_signals

    db = MagicMock()
    pair_held = _fake_pair(
        ana_id=1, info_id=10, symbol="005930", name="삼성전자", itype="domestic"
    )
    pair_other = _fake_pair(
        ana_id=2, info_id=11, symbol="000660", name="SK하이닉스", itype="domestic"
    )
    result = MagicMock()
    result.all.return_value = [pair_held, pair_other]
    db.execute = AsyncMock(return_value=result)

    resolver = RelationResolver(held={("kr", "005930")})
    resp = await build_signals(db=db, resolver=resolver, tab="mine", limit=20)
    assert len(resp.items) == 1
    assert resp.items[0].relation == "held"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signals_kr_tab_filters_market() -> None:
    from app.services.invest_view_model.signals_service import build_signals

    db = MagicMock()
    kr_pair = _fake_pair(
        ana_id=1, info_id=10, symbol="005930", name="삼성", itype="domestic"
    )
    us_pair = _fake_pair(
        ana_id=2, info_id=11, symbol="AAPL", name="Apple", itype="overseas"
    )
    result = MagicMock()
    result.all.return_value = [kr_pair, us_pair]
    db.execute = AsyncMock(return_value=result)

    resolver = RelationResolver()
    resp = await build_signals(db=db, resolver=resolver, tab="kr", limit=20)
    assert all(i.market == "kr" for i in resp.items)
