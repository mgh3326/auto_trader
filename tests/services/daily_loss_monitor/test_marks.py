"""hk 7119 — session-aware marks, policy cost estimate, FX snapshot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.daily_loss_monitor import marks, monitor
from app.services.daily_loss_monitor.accounts import ACCOUNTS
from app.services.daily_loss_monitor.marks import FxSnapshot
from app.services.daily_loss_monitor.reader import AccountFills
from app.services.daily_loss_monitor.rules import Fill, Mark

# 2026-11-27 (Fri) 11:00 KST = 02:00 UTC
OPEN = datetime(2026, 11, 27, 2, 0, tzinfo=UTC)
CLOSED = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)  # 21:00 KST


def _bounds(market, day):
    return (
        datetime(2026, 11, 27, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 27, 6, 30, tzinfo=UTC),
    )


async def _fresh(session, scope, symbols, now):
    return {s: Mark(Decimal("1"), now, None, basis="whatever") for s in symbols}


async def _close(session, scope, symbols, now):
    return {
        s: Mark(Decimal("2"), now, None, basis="last_session_close") for s in symbols
    }


@pytest.mark.asyncio
async def test_inside_session_uses_the_fresh_quote_outside_the_last_close() -> None:
    fn = marks.session_aware_marks(_fresh, _close, _bounds)
    kr = ACCOUNTS["kis_live_kr"]
    inside = await fn(None, kr, ["A"], OPEN)
    assert inside["A"].basis == "fresh_quote" and inside["A"].price == 1
    outside = await fn(None, kr, ["A"], CLOSED)
    assert outside["A"].basis == "last_session_close" and outside["A"].price == 2


@pytest.mark.asyncio
async def test_crypto_always_uses_the_fresh_quote() -> None:
    fn = marks.session_aware_marks(_fresh, _close, lambda m, d: None)
    out = await fn(None, ACCOUNTS["upbit_live"], ["KRW-BTC"], CLOSED)
    assert out["KRW-BTC"].basis == "fresh_quote"


def test_non_trading_day_is_outside_the_session() -> None:
    assert not marks.in_regular_session(
        ACCOUNTS["kis_live_us"], OPEN, lambda m, d: None
    )


def test_policy_cost_estimate_reads_the_round_trip_rate() -> None:
    kr = marks.policy_cost_estimate(ACCOUNTS["kis_live_kr"])
    us = marks.policy_cost_estimate(ACCOUNTS["toss_live_us"])
    assert kr is not None and us is not None
    assert kr.round_trip_cost_bps == Decimal("47.4") and kr.sell_tax_bps == Decimal(
        "18"
    )
    assert us.round_trip_cost_bps == Decimal("90") and us.sell_tax_bps == 0
    assert kr.rate_source.endswith("round_trip_cost_bps")
    assert kr.policy_content_hash


def test_unreadable_policy_is_no_estimate(monkeypatch) -> None:
    import app.services.trading_policy_service as svc

    def boom():
        raise OSError("gone")

    monkeypatch.setattr(svc, "load_trading_policy", boom)
    assert marks.policy_cost_estimate(ACCOUNTS["kis_live_kr"]) is None


@pytest.mark.asyncio
async def test_usd_account_records_the_fx_snapshot_and_estimate(monkeypatch) -> None:
    bought = Fill(
        "AAPL",
        "buy",
        Decimal("1"),
        Decimal("100"),
        None,
        None,
        None,
        "USD",
        OPEN - timedelta(minutes=30),
        None,
    )

    async def load(session, scope, *, day_start):
        return AccountFills([], [bought], frozenset())

    monkeypatch.setattr(monitor, "load_account_fills", load)
    snap = FxSnapshot(Decimal("1400"), "exchange_rate_service:test", OPEN)

    async def fx():
        return snap

    class _S:
        async def rollback(self):
            pass

    async def quote(session, scope, symbols, now):
        return {s: Mark(Decimal("100"), now, None) for s in symbols}

    [out] = await monitor.run_once(
        _S(),
        now=OPEN,
        reference_fn=quote,
        quote_fn=quote,
        fx_fn=fx,
        commit=False,
        accounts=["kis_live_us"],
    )
    assert out.fx == snap
    assert out.fee_bases == ("estimated",)
    assert out.cost_estimate is not None
    # 100 USD * 1400 * 90bp = 1,260 KRW cost.
    assert out.pnl_krw == Decimal("-1260")
