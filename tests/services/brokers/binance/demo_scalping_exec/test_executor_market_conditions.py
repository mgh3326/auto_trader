"""ROB-841 — executor preflight fail-closes without a server market snapshot.

The prior behavior synthesized ``spread_bps=0``/``data_age_seconds=0`` when
``market`` was omitted, silently disarming the SPREAD_TOO_WIDE / STALE_DATA
gates. That 0/0 fallback is removed: a missing snapshot now returns a
``blocked`` result with the ``market_conditions_unavailable`` reason code
BEFORE any ledger read — so an unavailable snapshot touches neither broker
nor ledger (ACs 1 & 4). A valid server-derived snapshot still flows into the
existing risk gates; stale / wide-spread snapshots reuse the existing reason
codes (ACs 2 & 3).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
    ReasonCode,
    ScalpingRiskLimits,
)
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent
from app.services.brokers.binance.demo_scalping_exec import executor as executor_mod
from app.services.brokers.binance.demo_scalping_exec.executor import (
    DemoScalpingExecutor,
)
from app.services.brokers.binance.demo_scalping_exec.reference import SymbolReference

_NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.UTC)
_REF = SymbolReference(
    price=Decimal("1.36"),
    step_size=Decimal("0.1"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.0001"),
)


def _limits_for(symbol: str) -> ScalpingRiskLimits:
    return ScalpingRiskLimits(
        allowlist=frozenset({symbol}),
        excluded=frozenset(),
        global_open_lifecycle_cap=10_000,
        daily_order_count_cap=10_000,
        daily_loss_budget_usdt=Decimal("1000000"),
    )


def _intent(symbol: str, side: str = "BUY") -> OrderIntent:
    return OrderIntent(
        product="usdm_futures",
        symbol=symbol,
        side=side,
        order_type="MARKET",
        target_notional_usdt=Decimal("10"),
        entry_reference_price=Decimal("1.36"),
        tp_price=None,
        sl_price=None,
        confidence=Decimal("0.5"),
        reason_codes=("enter_long_breakout",),
        source_candle_close_time_ms=1_779_000_000_000,
        evaluated_at_ms=1_779_000_001_000,
    )


class _FakeReference:
    async def fetch(self, product, symbol):
        return _REF

    async def aclose(self):  # pragma: no cover - parity with real
        return None


class _ExplodingClient:
    """Any broker call is a test failure — the fail-close must never reach it."""

    def __getattr__(self, name):
        async def _boom(*args, **kwargs):
            raise AssertionError(f"broker call {name!r} must not happen on fail-close")

        return _boom


async def _ledger_rowcount(db_session) -> int:
    return await db_session.scalar(select(func.count(BinanceDemoOrderLedger.id)))


@pytest.mark.asyncio
async def test_missing_market_fails_closed_no_ledger_no_broker(
    db_session, monkeypatch
) -> None:
    # Spy: the fail-close must return BEFORE any ledger read.
    def _no_ledger_read(*args, **kwargs):
        raise AssertionError("ledger must not be read when market is unavailable")

    monkeypatch.setattr(executor_mod, "load_ledger_snapshot", _no_ledger_read)

    before = await _ledger_rowcount(db_session)
    executor = DemoScalpingExecutor(
        product="usdm_futures",
        client=_ExplodingClient(),
        session=db_session,
        reference=_FakeReference(),
        now=_NOW,
        limits=_limits_for("MKTUNAVAILUSDT"),
    )
    # market omitted → must NOT synthesize 0/0; must fail closed.
    result = await executor.execute(_intent("MKTUNAVAILUSDT"), confirm=True)
    assert result.status == "blocked"
    assert result.reason_codes == (ReasonCode.MARKET_CONDITIONS_UNAVAILABLE,)
    after = await _ledger_rowcount(db_session)
    assert after == before  # zero ledger writes


@pytest.mark.asyncio
async def test_stale_market_blocked_with_existing_stale_data(db_session) -> None:
    executor = DemoScalpingExecutor(
        product="usdm_futures",
        client=_ExplodingClient(),
        session=db_session,
        reference=_FakeReference(),
        now=_NOW,
        limits=_limits_for("MKTSTALEUSDT"),
    )
    stale = MarketConditions(
        spread_bps=Decimal("1"),
        data_age_seconds=999.0,  # ≫ 120 cap
        spot_free_base_qty=Decimal("0"),
    )
    result = await executor.execute(_intent("MKTSTALEUSDT"), confirm=True, market=stale)
    assert result.status == "blocked"
    assert ReasonCode.STALE_DATA in result.reason_codes


@pytest.mark.asyncio
async def test_wide_spread_market_blocked_with_existing_spread_too_wide(
    db_session,
) -> None:
    executor = DemoScalpingExecutor(
        product="usdm_futures",
        client=_ExplodingClient(),
        session=db_session,
        reference=_FakeReference(),
        now=_NOW,
        limits=_limits_for("MKTWIDEUSDT"),
    )
    wide = MarketConditions(
        spread_bps=Decimal("500"),  # ≫ 20 cap
        data_age_seconds=1.0,
        spot_free_base_qty=Decimal("0"),
    )
    result = await executor.execute(_intent("MKTWIDEUSDT"), confirm=True, market=wide)
    assert result.status == "blocked"
    assert ReasonCode.SPREAD_TOO_WIDE in result.reason_codes


@pytest.mark.asyncio
async def test_valid_market_dry_run_reads_ledger_but_writes_nothing(
    db_session,
) -> None:
    # A valid server snapshot + confirm=False → dry_run judgment, no order,
    # no ledger insert (AC6 at the executor layer).
    executor = DemoScalpingExecutor(
        product="usdm_futures",
        client=_ExplodingClient(),
        session=db_session,
        reference=_FakeReference(),
        now=_NOW,
        limits=_limits_for("MKTVALIDUSDT"),
    )
    fresh = MarketConditions(
        spread_bps=Decimal("2"),
        data_age_seconds=5.0,
        spot_free_base_qty=Decimal("0"),
    )
    before = await _ledger_rowcount(db_session)
    result = await executor.execute(
        _intent("MKTVALIDUSDT"), confirm=False, market=fresh
    )
    assert result.status == "dry_run"
    after = await _ledger_rowcount(db_session)
    assert after == before  # dry-run inserts no ledger rows
