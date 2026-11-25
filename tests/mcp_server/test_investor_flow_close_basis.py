"""hk 6317: investor-flow ``close`` is a provider value and must be labelled.

Fixture ``005930_trend_2026-11-24_provider_close.json`` is the real Naver trend
payload captured 2026-11-25 (first four rows). For 2026-11-24 the payload says
``closePrice`` 182,900, while the KRX settled regular-session close is 189,400
(public.kr_candles_1d; implied by the next session's Naver compare field:
171,100 + 18,300). Every close the flow tools emit must therefore carry
``close_source``/``close_basis`` and never appear as the KRX close unlabelled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.mcp_server.tooling.fundamentals import _investor_flow_common as ifc
from app.mcp_server.tooling.fundamentals import _valuation as valuation_mod
from app.mcp_server.tooling.fundamentals._investor_flow_common import (
    CLOSE_BASIS_PROVIDER_UNVERIFIED,
    CLOSE_SOURCE_NAVER_TREND,
    label_provider_close,
)
from app.services.naver_finance.investor import _parse_trend_payload

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "investor_flow"
    / "005930_trend_2026-11-24_provider_close.json"
)
PROVIDER_CLOSE_2026_11_24 = 182900  # Naver closePrice (provider value)
KRX_SETTLED_CLOSE_2026_11_24 = 189400  # not carried by the payload


def _provider_rows(days: int = 4) -> list[dict]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows, skipped = _parse_trend_payload(payload, days=days)
    assert skipped == {}
    assert len(rows) == days
    return rows


def _assert_labelled(row: dict) -> None:
    assert row["close_source"] == CLOSE_SOURCE_NAVER_TREND
    assert row["close_basis"] == CLOSE_BASIS_PROVIDER_UNVERIFIED


@pytest.mark.asyncio
async def test_day_row_close_is_labelled_as_provider_value(monkeypatch):
    rows = _provider_rows()

    async def fake_fetch(symbol, days):
        return {"symbol": symbol, "days": days, "data": rows, "skipped": {}}

    monkeypatch.setattr(valuation_mod, "_fetch_investor_trends_naver", fake_fetch)

    result = await valuation_mod.handle_get_investor_trends("005930", 4, "day")

    row_1124 = next(r for r in result["data"] if r["date"] == "2026-11-24")
    assert row_1124["close"] == PROVIDER_CLOSE_2026_11_24
    assert row_1124["close"] != KRX_SETTLED_CLOSE_2026_11_24
    for row in result["data"]:
        _assert_labelled(row)


@pytest.mark.asyncio
async def test_week_bucket_close_keeps_the_provider_label(monkeypatch):
    rows = _provider_rows()

    async def fake_fetch(symbol, days):
        return {"symbol": symbol, "days": days, "data": rows, "skipped": {}}

    monkeypatch.setattr(valuation_mod, "_fetch_investor_trends_naver", fake_fetch)

    result = await valuation_mod.handle_get_investor_trends("005930", 4, "week")

    assert result["data"], "week aggregation must produce buckets"
    for bucket in result["data"]:
        assert bucket["close"] is not None
        _assert_labelled(bucket)


@pytest.mark.asyncio
async def test_confirmed_block_history_close_is_labelled(monkeypatch):
    rows = _provider_rows()

    async def fake_fetch(symbol, days):
        return {"source": "naver", "data": rows}

    monkeypatch.setattr(ifc, "_fetch_investor_trends_naver", fake_fetch)

    block, last_confirmed = await ifc.build_confirmed_block("005930", days=4)

    assert last_confirmed == "2026-11-24"
    history_1124 = block["history"][0]
    assert history_1124["date"] == "2026-11-24"
    assert history_1124["close"] == PROVIDER_CLOSE_2026_11_24
    for item in block["history"]:
        _assert_labelled(item)


def test_row_without_close_gets_null_labels_and_zero_is_a_value():
    missing = label_provider_close({"date": "2026-11-24", "close": None})
    assert missing["close_source"] is None
    assert missing["close_basis"] is None

    zero = label_provider_close({"date": "2026-11-24", "close": 0})
    _assert_labelled(zero)
    assert zero["close"] == 0
