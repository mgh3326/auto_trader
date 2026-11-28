"""hk 7119 — marks by trading window, cost estimate, FX snapshot, holdings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.daily_loss_monitor import marks, monitor
from app.services.daily_loss_monitor.accounts import ACCOUNTS
from app.services.daily_loss_monitor.marks import FxSnapshot, mark_plan
from app.services.daily_loss_monitor.reader import AccountFills
from app.services.daily_loss_monitor.rules import Fill, Mark

KR = ACCOUNTS["kis_live_kr"]
US = ACCOUNTS["kis_live_us"]
UPBIT = ACCOUNTS["upbit_live"]


def _kr_bounds(market, day):
    # XKRX regular 09:00-15:30 KST on every day asked.
    return (
        datetime(day.year, day.month, day.day, 0, 0, tzinfo=UTC),
        datetime(day.year, day.month, day.day, 6, 30, tzinfo=UTC),
    )


def _us_bounds(market, day):
    # XNYS 09:30-16:00 ET (EST, UTC-5).
    return (
        datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC),
        datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
    )


def _kst(h, m=0, day=27):
    return datetime(2026, 11, day, h, m, tzinfo=marks.KST)


@pytest.mark.parametrize(
    ("at", "basis"),
    [
        (_kst(8, 10), "fresh_quote"),  # NXT pre
        (_kst(11), "fresh_quote"),  # regular
        (_kst(15, 35), "segment_close"),  # gap after the 15:30 close
        (_kst(17), "fresh_quote"),  # NXT / KRX after-market
        (_kst(21), "segment_close"),  # after 20:00
        (_kst(7), "last_session_close"),  # before the day's first window
    ],
)
def test_kr_windows_include_nxt_and_after_market(at, basis) -> None:
    assert mark_plan(KR, at, _kr_bounds)[0] == basis


def test_us_post_market_runs_to_20_et_on_an_early_close() -> None:
    def early(market, day):  # 13:00 ET close
        return (
            datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC),
            datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC),
        )

    at = datetime(2026, 11, 28, 0, 30, tzinfo=UTC)  # 19:30 ET Fri 11-27
    assert mark_plan(US, at, early) == ("fresh_quote", at)


@pytest.mark.asyncio
async def test_nonpositive_bar_price_is_no_mark() -> None:
    async def bars(session, scope, symbols, until):
        return {s: Mark(Decimal("0"), until, None) for s in symbols}

    out = await marks.marks_at(bars)(None, UPBIT, ["KRW-BTC"], _kst(3))
    assert out["KRW-BTC"] is None


def test_kr_gap_uses_the_window_end_as_cutoff() -> None:
    """The 15:30 cutoff includes the closing-auction bar (bar end 15:31)."""
    assert mark_plan(KR, _kst(15, 35), _kr_bounds) == (
        "segment_close",
        datetime(2026, 11, 27, 6, 31, tzinfo=UTC),
    )


def test_non_trading_day_uses_the_last_session_close() -> None:
    assert mark_plan(KR, _kst(11), lambda m, d: None)[0] == "last_session_close"


def test_us_is_trading_at_kst_midnight() -> None:
    midnight = datetime(2026, 11, 28, 0, 0, tzinfo=marks.KST)  # 10:00 ET Fri
    assert mark_plan(US, midnight, _us_bounds) == ("fresh_quote", midnight)
    # US pre/post count: 05:00 ET and 19:00 ET.
    assert (
        mark_plan(US, datetime(2026, 11, 27, 10, 0, tzinfo=UTC), _us_bounds)[0]
        == "fresh_quote"
    )
    assert (
        mark_plan(US, datetime(2026, 11, 28, 0, 0, tzinfo=UTC), _us_bounds)[0]
        == "fresh_quote"
    )


def test_crypto_is_always_fresh() -> None:
    assert mark_plan(UPBIT, _kst(3), lambda m, d: None)[0] == "fresh_quote"


@pytest.mark.asyncio
async def test_day_start_mark_reads_the_bar_at_kst_midnight() -> None:
    seen = []

    async def bars(session, scope, symbols, until):
        seen.append(until)
        return {
            s: Mark(Decimal("7"), until - timedelta(minutes=2), None) for s in symbols
        }

    fn = marks.marks_at(bars, day_start=True, bounds_fn=_us_bounds)
    now = datetime(2026, 11, 28, 3, 0, tzinfo=UTC)  # 12:00 KST
    out = await fn(None, US, ["AAPL"], now)
    assert seen == [datetime(2026, 11, 28, 0, 0, tzinfo=marks.KST)]
    assert out["AAPL"].price == 7 and out["AAPL"].basis == "fresh_quote"


@pytest.mark.asyncio
async def test_a_bar_older_than_ten_minutes_is_no_mark() -> None:
    async def bars(session, scope, symbols, until):
        return {
            s: Mark(Decimal("7"), until - timedelta(minutes=11), None) for s in symbols
        }

    out = await marks.marks_at(bars)(None, UPBIT, ["KRW-BTC"], _kst(3))
    assert out["KRW-BTC"] is None


def test_policy_cost_estimate_reads_the_round_trip_rate() -> None:
    kr = marks.policy_cost_estimate(KR)
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
    assert marks.policy_cost_estimate(KR) is None


def test_holdings_must_match_symbol_by_symbol() -> None:
    assert monitor.holdings_match({"A": Decimal(1)}, {"A": Decimal(1), "B": Decimal(0)})
    assert not monitor.holdings_match({"A": Decimal(1)}, {"A": Decimal(2)})
    assert not monitor.holdings_match({}, {"A": Decimal(1)})  # unseeded holding
    assert not monitor.holdings_match({}, None)


@pytest.mark.asyncio
async def test_usd_account_records_the_fx_snapshot_and_estimate(monkeypatch) -> None:
    now = datetime(2026, 11, 27, 2, 0, tzinfo=UTC)
    bought = Fill(
        "AAPL",
        "buy",
        Decimal("1"),
        Decimal("100"),
        None,
        None,
        None,
        "USD",
        now - timedelta(minutes=30),
        None,
    )

    async def load(session, scope, *, day_start, now=None):
        return AccountFills([], [bought], frozenset(), (), {"AAPL": Decimal(1)})

    monkeypatch.setattr(monitor, "load_account_fills", load)
    snap = FxSnapshot(Decimal("1400"), "exchange_rate_service:test", now)

    async def fx():
        return snap

    async def held(scope):
        return {"AAPL": Decimal(1)}

    class _S:
        async def rollback(self):
            pass

    async def quote(session, scope, symbols, at):
        return {s: Mark(Decimal("100"), at, None) for s in symbols}

    [out] = await monitor.run_once(
        _S(),
        now=now,
        reference_fn=quote,
        quote_fn=quote,
        fx_fn=fx,
        commit=False,
        accounts=["kis_live_us"],
        holdings_fn=held,
    )
    assert out.fx == snap
    assert out.fee_bases == ("estimated",)
    assert out.cost_estimate is not None
    assert out.pnl_krw == Decimal("-1260")  # 100 USD * 1400 * 90bp


@pytest.mark.asyncio
async def test_missing_holdings_source_freezes(monkeypatch) -> None:
    async def load(session, scope, *, day_start, now=None):
        return AccountFills([], [], frozenset(), (), {})

    monkeypatch.setattr(monitor, "load_account_fills", load)

    class _S:
        async def rollback(self):
            pass

    async def none(session, scope, symbols, at):
        return {}

    [out] = await monitor.run_once(
        _S(),
        now=datetime(2026, 11, 27, 2, 0, tzinfo=UTC),
        reference_fn=none,
        quote_fn=none,
        fx_fn=None,
        commit=False,
        accounts=["toss_live_kr"],
    )
    assert out.reasons == ("holdings_unverified",) and out.freeze
