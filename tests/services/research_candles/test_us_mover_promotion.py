"""hk 6554: US mover promotion order, asset filter and readable decisions.

The 11-25 case (design evidence): 90 up and 90 down KIS rows per 5-minute run
fill the 40-slot mover budget with whoever was captured earliest, many of them
thin names. URBN (rank 14, high dollar volume, captured 21:25 KST) was cut.
The fixture below reproduces that with fake reads; the production path is
``build_targets(market='us')`` and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.research_candles import session_minutes_collector as sc

pytestmark = pytest.mark.unit

# 2026-11-25 07:30 ET (pre-market): the ET trading day is 2026-11-25. The
# captures below are 21:20 / 21:25 / 21:35 KST = 12:20 / 12:25 / 12:35 UTC.
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)
T_1320 = datetime(2026, 11, 25, 12, 20, tzinfo=UTC)
T_1325 = datetime(2026, 11, 25, 12, 25, tzinfo=UTC)
T_1335 = datetime(2026, 11, 25, 12, 35, tzinfo=UTC)
FLOOR = Decimal("100000")


def _row(
    symbol: str,
    *,
    at: datetime,
    rank: int,
    trade_value: str | None,
    asset_type: str | None = "common_stock",
):
    """A ``invest_momentum_event_snapshots`` US row (hk 6510 fields)."""
    return SimpleNamespace(
        symbol=symbol,
        name=None,
        market="us",
        source="kis_overseas_ranking",
        surface="us:kis_overseas_ranking:us_pre:down",
        trade_type="US",
        market_type="NAS",
        order_type="down",
        snapshot_at=at,
        trading_date=at.date(),
        rank=rank,
        change_rate=None,
        trade_value=None if trade_value is None else Decimal(trade_value),
        asset_type=asset_type,
    )


def _thin_movers() -> list:
    """40 thin names captured first (21:20 KST) with ranks better than URBN's 14."""
    return [
        _row(f"THN{i:02d}", at=T_1320, rank=1 + (i % 13), trade_value="20000")
        for i in range(40)
    ]


def _the_11_25_rows() -> list:
    return [
        *_thin_movers(),
        _row("DELL", at=T_1320, rank=7, trade_value="50000000"),
        _row(
            "WRNT",
            at=T_1320,
            rank=2,
            trade_value="5000000",
            asset_type="right_or_warrant",
        ),
        # URBN: captured 21:25 KST at rank 14, then a later run at rank 9.
        _row("URBN", at=T_1325, rank=14, trade_value="2000000"),
        _row("URBN", at=T_1335, rank=9, trade_value="2100000"),
    ]


class _Result:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _UsSession:
    """Dispatch by table name: mover rows, earliest minute rows, pick log."""

    def __init__(self, mover_rows, earliest_today=(), pick_symbols=()):
        self._movers = list(mover_rows)
        self._earliest = list(earliest_today)
        self._pick = list(pick_symbols)
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        text = str(statement)
        self.statements.append(text)
        if "invest_momentum_event_snapshots" in text:
            values = statement.compile().params.values()
            return _Result(
                row
                for row in self._movers
                if not (
                    (row.source == "benzinga_pro" and "benzinga_pro" in values)
                    or (row.order_type == "excluded" and "excluded" in values)
                )
            )
        if "us_candles_1m" in text:
            return _Result(self._earliest)
        return _Result(self._pick)

    async def rollback(self):  # pragma: no cover - no failing read in this fixture
        return None


class _NoHoldingsUs:
    async def fetch_my_us_stocks(self):
        return []

    async def fetch_my_stocks(self):  # pragma: no cover
        return []


async def _snapshot(**kwargs):
    return {"results": []}


def _presets(market):
    return []


async def _us_targets(monkeypatch, session, **kwargs):
    import app.services.manual_holdings_service as manual_module

    class _Manual:
        def __init__(self, session):
            pass

        async def get_holdings_by_user(self, user_id, market_type=None):
            return []

    monkeypatch.setattr(manual_module, "ManualHoldingsService", _Manual)
    params = {
        "market": "us",
        "session": session,
        "kis": _NoHoldingsUs(),
        "user_id": 1,
        "fixed_symbols_raw": "SPY",
        "now": NOW,
        "snapshot_fn": _snapshot,
        "presets_fn": _presets,
        "us_mover_dollar_volume_floor": FLOOR,
        # SPY is the only fixed symbol; a fixed budget of 1 leaves no spill, so the
        # intraday budget (40) binds exactly as it did on 11-25.
        "fixed_budget": 1,
    }
    params.update(kwargs)
    return await sc.build_targets(**params)


@pytest.mark.asyncio
async def test_the_11_25_case_urbn_is_selected_under_the_same_budget(
    monkeypatch,
) -> None:
    session = _UsSession(_the_11_25_rows())
    targets = await _us_targets(monkeypatch, session)

    # URBN was cut by first-capture order before this change; it must now be
    # selected under the same 40-slot intraday budget.
    assert "URBN" in targets.symbols
    assert "DELL" in targets.symbols
    # the warrant is excluded from the movers tier, with its reason
    assert "WRNT" not in targets.symbols
    assert {
        "symbol": "WRNT",
        "tier": "intraday_movers",
        "reason": "not_common_or_etf",
    } in (targets.cuts)


@pytest.mark.asyncio
async def test_a_thin_name_is_cut_by_budget_with_its_score_inputs_shown(
    monkeypatch,
) -> None:
    session = _UsSession(_the_11_25_rows())
    targets = await _us_targets(monkeypatch, session)

    thin_cuts = [c for c in targets.cuts if c["symbol"].startswith("THN")]
    assert thin_cuts, (
        "the 40 thin names cannot all fit the 40-slot budget with URBN and DELL"
    )
    assert all(
        c["reason"] == "budget_cut" and c["tier"] == "intraday_movers"
        for c in thin_cuts
    )

    decisions = targets.decisions["decisions"]
    cut_thin = next(d for d in decisions if d["symbol"] == thin_cuts[0]["symbol"])
    assert cut_thin["decision"] == "cut"
    assert cut_thin["reason"] == "budget_cut"
    assert cut_thin["score"]["dollar_volume"] == "20000"
    assert cut_thin["score"]["liquidity"] == "below_floor"
    assert cut_thin["score"]["asset_type"] == "common_stock"
    assert cut_thin["score"]["best_rank"] in range(1, 14)


@pytest.mark.asyncio
async def test_a_mover_whose_later_run_has_the_best_rank_scores_with_that_rank(
    monkeypatch,
) -> None:
    session = _UsSession(_the_11_25_rows())
    targets = await _us_targets(monkeypatch, session)
    urbn = next(d for d in targets.decisions["decisions"] if d["symbol"] == "URBN")
    assert urbn["decision"] == "selected"
    assert urbn["tier"] == "intraday_movers"
    assert (
        urbn["score"]["best_rank"] == 9
    )  # 21:35 KST run, not the 21:25 capture rank 14
    assert urbn["score"]["dollar_volume"] == "2100000"
    assert urbn["score"]["liquidity"] == "above_floor"


@pytest.mark.asyncio
async def test_the_excluded_warrant_is_reported_with_its_reason(monkeypatch) -> None:
    session = _UsSession(_the_11_25_rows())
    targets = await _us_targets(monkeypatch, session)
    wrnt = next(d for d in targets.decisions["decisions"] if d["symbol"] == "WRNT")
    assert wrnt == {
        "symbol": "WRNT",
        "tier": "intraday_movers",
        "decision": "cut",
        "reason": "not_common_or_etf",
        "score": {
            "best_rank": 2,
            "dollar_volume": "5000000",
            "asset_type": "right_or_warrant",
            "liquidity": "above_floor",
            "first_captured_at": T_1320.isoformat(),
            "protected": False,
            "position": None,
        },
    }


@pytest.mark.asyncio
async def test_benzinga_pro_promotion_is_opt_in_and_excludes_sentinels(monkeypatch):
    kis_rows = _the_11_25_rows()
    alpaca = _row("ALPA", at=T_1320, rank=3, trade_value="5000000")
    alpaca.source = "alpaca_screener"
    existing = [*kis_rows, alpaca]
    benzinga = _row("BZUP", at=T_1320, rank=1, trade_value="50000000")
    benzinga.source = "benzinga_pro"
    excluded = _row("BZNO", at=T_1320, rank=1, trade_value="50000000")
    excluded.source, excluded.order_type = "benzinga_pro", "excluded"
    all_rows = [*existing, benzinga, excluded]

    monkeypatch.setattr(settings, "SESSION_MINUTES_BENZINGA_PRO_MOVERS_ENABLED", False)
    baseline = await _us_targets(monkeypatch, _UsSession(existing))
    disabled = await _us_targets(monkeypatch, _UsSession(all_rows))
    assert baseline.symbols == disabled.symbols
    assert sc.targets_report(baseline) == sc.targets_report(disabled)

    monkeypatch.setattr(settings, "SESSION_MINUTES_BENZINGA_PRO_MOVERS_ENABLED", True)
    enabled = await _us_targets(monkeypatch, _UsSession(all_rows))
    assert "BZUP" in enabled.symbols
    assert "BZNO" not in enabled.symbols
