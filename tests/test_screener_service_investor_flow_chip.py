from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.investor_flow import InvestorFlowItem
from app.services.invest_view_model.screener_service import (
    _hydrate_investor_flow_chips,
    _investor_flow_chip_for_item,
)


def _item(**overrides) -> InvestorFlowItem:
    defaults = {
        "symbol": "403550",
        "market": "kr",
        "dataState": "fresh",
        "snapshotDate": dt.date(2026, 5, 12),
        "doubleBuy": False,
        "doubleSell": False,
        "foreignConsecutiveBuyDays": None,
        "foreignConsecutiveSellDays": None,
        "institutionConsecutiveBuyDays": None,
        "institutionConsecutiveSellDays": None,
        "individualConsecutiveBuyDays": None,
        "individualConsecutiveSellDays": None,
    }
    defaults.update(overrides)
    return InvestorFlowItem(**defaults)


def test_double_buy_takes_precedence():
    chip = _investor_flow_chip_for_item(
        _item(
            doubleBuy=True,
            foreignConsecutiveBuyDays=4,
            institutionConsecutiveBuyDays=2,
        )
    )
    assert chip is not None
    assert chip.tone == "double_buy"
    assert "쌍끌이 매수" in chip.label


def test_foreign_consecutive_buy_only():
    chip = _investor_flow_chip_for_item(_item(foreignConsecutiveBuyDays=5))
    assert chip is not None
    assert chip.tone == "foreign_buy"
    assert chip.label == "외국인 5일 순매수"


def test_no_chip_when_signals_below_threshold():
    chip = _investor_flow_chip_for_item(_item(foreignConsecutiveBuyDays=2))
    assert chip is None


def test_missing_state_yields_no_chip():
    chip = _investor_flow_chip_for_item(_item(dataState="missing"))
    assert chip is None


def test_stale_state_annotates_label():
    chip = _investor_flow_chip_for_item(
        _item(dataState="stale", foreignConsecutiveBuyDays=4)
    )
    assert chip is not None
    assert chip.dataState == "stale"
    assert "1일 지연" in chip.label


def test_double_sell_chip():
    chip = _investor_flow_chip_for_item(
        _item(
            doubleSell=True,
            foreignConsecutiveSellDays=3,
            institutionConsecutiveSellDays=2,
        )
    )
    assert chip is not None
    assert chip.tone == "double_sell"
    assert "쌍끌이 매도" in chip.label


def test_institution_consecutive_buy():
    chip = _investor_flow_chip_for_item(_item(institutionConsecutiveBuyDays=4))
    assert chip is not None
    assert chip.tone == "institution_buy"
    assert "기관 4일 순매수" in chip.label


def test_institution_consecutive_sell():
    chip = _investor_flow_chip_for_item(_item(institutionConsecutiveSellDays=3))
    assert chip is not None
    assert chip.tone == "institution_sell"
    assert "기관 3일 순매도" in chip.label


@pytest.mark.asyncio
async def test_hydrate_skips_non_kr_market():
    rows = [{"market": "us", "symbol": "AAPL"}]
    chips = await _hydrate_investor_flow_chips(
        db=SimpleNamespace(), market="us", rows=rows
    )
    assert chips == {}


@pytest.mark.asyncio
async def test_hydrate_swallows_provider_failures(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._latest_investor_flow_items",
        boom,
    )

    chips = await _hydrate_investor_flow_chips(
        db=SimpleNamespace(),
        market="kr",
        rows=[{"market": "kr", "symbol": "403550"}],
    )
    assert chips == {}


@pytest.mark.asyncio
async def test_hydrate_returns_chip_for_matching_snapshot(monkeypatch):
    fake_item = _item(foreignConsecutiveBuyDays=4)
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._latest_investor_flow_items",
        AsyncMock(return_value={"403550": fake_item}),
    )

    chips = await _hydrate_investor_flow_chips(
        db=SimpleNamespace(),
        market="kr",
        rows=[{"market": "kr", "symbol": "403550"}],
    )
    assert "403550" in chips
    assert chips["403550"].tone == "foreign_buy"
