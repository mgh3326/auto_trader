from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.services.invest_view_model.investor_flow_service import (
    latest_items_for_symbols,
)


def _row(symbol: str, snapshot_date: dt.date) -> InvestorFlowSnapshot:
    return InvestorFlowSnapshot(
        market="kr",
        symbol=symbol,
        snapshot_date=snapshot_date,
        foreign_net=100,
        institution_net=-50,
        individual_net=-50,
        foreign_net_buy_rank=3,
        foreign_net_sell_rank=None,
        institution_net_buy_rank=None,
        institution_net_sell_rank=12,
        double_buy=False,
        double_sell=False,
        foreign_consecutive_buy_days=2,
        foreign_consecutive_sell_days=None,
        institution_consecutive_buy_days=None,
        institution_consecutive_sell_days=4,
        individual_consecutive_buy_days=None,
        individual_consecutive_sell_days=None,
        source="naver_finance",
        collected_at=dt.datetime(2026, 5, 12, tzinfo=dt.UTC),
    )


@pytest.mark.asyncio
async def test_latest_items_for_symbols_returns_dict_keyed_by_symbol(monkeypatch):
    today = dt.date(2026, 5, 13)
    fake_repo = SimpleNamespace(
        latest_by_symbols=AsyncMock(return_value=[_row("403550", dt.date(2026, 5, 12))])
    )

    monkeypatch.setattr(
        "app.services.invest_view_model.investor_flow_service.InvestorFlowSnapshotsRepository",
        lambda db: fake_repo,
    )

    result = await latest_items_for_symbols(
        db=SimpleNamespace(),
        symbols=["403550", " 003550 "],
        as_of=today,
        max_stale_days=1,
    )

    assert set(result.keys()) == {"403550"}
    assert result["403550"].dataState == "fresh"
    assert result["403550"].foreignNet == 100
    fake_repo.latest_by_symbols.assert_awaited_once()


@pytest.mark.asyncio
async def test_latest_items_for_symbols_marks_stale_when_age_exceeds_threshold(
    monkeypatch,
):
    today = dt.date(2026, 5, 13)
    fake_repo = SimpleNamespace(
        latest_by_symbols=AsyncMock(return_value=[_row("403550", dt.date(2026, 5, 10))])
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.investor_flow_service.InvestorFlowSnapshotsRepository",
        lambda db: fake_repo,
    )

    result = await latest_items_for_symbols(
        db=SimpleNamespace(),
        symbols=["403550"],
        as_of=today,
        max_stale_days=1,
    )

    assert result["403550"].dataState == "stale"


@pytest.mark.asyncio
async def test_latest_items_for_symbols_empty_input_returns_empty(monkeypatch):
    result = await latest_items_for_symbols(
        db=SimpleNamespace(),
        symbols=[],
        as_of=dt.date(2026, 5, 13),
    )
    assert result == {}


@pytest.mark.asyncio
async def test_latest_items_for_symbols_non_kr_raises(monkeypatch):
    with pytest.raises(ValueError, match="kr"):
        await latest_items_for_symbols(
            db=SimpleNamespace(),
            symbols=["AAPL"],
            market="us",
        )
