"""hk 6554: the KR target selection is byte-identical to the pre-6554 golden.

The US ordering change must not move a single KR symbol, tier count, cut or
report key. This golden pins ``build_targets`` + ``targets_report`` for one KR
scenario that uses every tier, a protected mover and a budget cut. It is written
on the unchanged code first; a later edit that changes it is a KR regression.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.research_candles import session_minutes_collector as sc

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)
GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "session_minutes" / (
    "kr_targets_golden.json"
)


class _Result:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _Session:
    def __init__(self, pick_symbols, mover_rows, have_today):
        self._pick = list(pick_symbols)
        self._movers = list(mover_rows)
        self._have = list(have_today)

    async def execute(self, statement, params=None):
        text = str(statement)
        if "invest_momentum_event_snapshots" in text:
            return _Result(self._movers)
        if "kr_candles_1m" in text:
            return _Result(self._have)
        return _Result(self._pick)

    async def rollback(self):  # pragma: no cover - no failing read in this scenario
        return None


class _Kis:
    async def fetch_my_stocks(self):
        return [{"pdno": "000001"}, {"pdno": "000002"}]

    async def fetch_my_us_stocks(self):  # pragma: no cover
        return []


def _mover(symbol: str, rank: int):
    return SimpleNamespace(
        symbol=symbol,
        name=None,
        market="kr",
        surface="domestic_market_stock_default",
        snapshot_at=NOW,
        trade_type="KRX",
        market_type="ALL",
        order_type="up",
        rank=rank,
        change_rate=None,
        trade_value=None,
    )


class _Snapshot:
    async def __call__(self, **kwargs):
        rows = [
            {"symbol": symbol, "rank": index}
            for index, symbol in enumerate(["000800", "000801", "000700"], start=1)
        ]
        return {"results": rows[: kwargs["limit"]]}


def _preset(preset_id: str):
    return SimpleNamespace(id=preset_id, availability="active")


async def kr_golden_snapshot(monkeypatch) -> dict:
    import app.services.manual_holdings_service as manual_module

    class _Manual:
        def __init__(self, session):
            pass

        async def get_holdings_by_user(self, user_id, market_type=None):
            return []

    monkeypatch.setattr(manual_module, "ManualHoldingsService", _Manual)
    session = _Session(
        pick_symbols=["000500", "000501", "000800"],
        mover_rows=[_mover("000700", 3), _mover("000701", 1), _mover("000702", 2)],
        have_today=["000701"],
    )
    targets = await sc.build_targets(
        market="kr",
        session=session,  # type: ignore[arg-type]
        kis=_Kis(),  # type: ignore[arg-type]
        user_id=1,
        fixed_symbols_raw="000200,000201",
        now=NOW,
        screener_top_n=3,
        max_symbols=8,
        snapshot_fn=_Snapshot(),
        presets_fn=lambda market: [_preset("alpha")],
        intraday_budget=2,
        screener_budget=2,
    )
    return {
        "symbols": targets.symbols,
        "backfill": targets.backfill,
        "errors": targets.errors,
        "report": sc.targets_report(targets),
    }


@pytest.mark.asyncio
async def test_kr_target_selection_matches_the_golden(monkeypatch) -> None:
    snapshot = await kr_golden_snapshot(monkeypatch)
    rendered = json.dumps(snapshot, sort_keys=True, indent=2, ensure_ascii=False)
    assert rendered + "\n" == GOLDEN.read_text(encoding="utf-8")
