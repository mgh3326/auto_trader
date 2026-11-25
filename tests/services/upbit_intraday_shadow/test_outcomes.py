"""Virtual round trips: next-bar open only, never a price touch (hk 6036)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import numpy as np
import pytest

from app.services.upbit_intraday_shadow import contracts as K
from app.services.upbit_intraday_shadow import outcomes
from app.services.upbit_intraday_shadow.candidates import build_panel
from app.services.upbit_intraday_shadow.public_data import CandlePage, slot_of
from app.services.upbit_intraday_shadow.recorder import run_once
from research.upbit_intraday import rules as R
from tests.services.upbit_intraday_shadow import support as S

pytestmark = pytest.mark.unit

MARKET = "KRW-AAA"
SIGNAL = 100
NUMBERS = (
    "entry_source",
    "virtual_entry_price",
    "exit_basis",
    "virtual_exit_price",
    "entry_tick_krw",
    "exit_tick_krw",
    "gross_return",
    "net_return",
    "mae",
    "mfe",
)


def pending(direction: int = 1, *, resolved=frozenset()) -> K.PendingCandidate:
    return K.PendingCandidate(
        candidate_id=7,
        market=MARKET,
        config_id="A_breakout:N16_X3",
        direction=direction,
        disposition="signal",
        signal_bar_open_at=S.at(SIGNAL),
        roundtrip_cost_rate=Decimal("0.005"),
        resolved_horizons=resolved,
    )


def page(rows: dict, *, covers_from: int | None = None) -> CandlePage:
    base = S.end_slot_after(0)
    return CandlePage(
        MARKET,
        {base + i: r for i, r in rows.items()},
        covers_from,
        base + max(rows) + 1,
        S.run_after(max(rows)),
    )


def resolve(rows: dict, *, end: int, direction: int = 1, **kwargs):
    return outcomes.resolve(
        pending(direction, **kwargs),
        page(rows),
        end_slot=S.end_slot_after(end),
        now=S.run_after(end),
    )


def walk(count: int = 210) -> dict:
    return {i: S.bar(i) for i in range(count)}


@pytest.mark.parametrize("direction", [1, -1])
def test_a_price_touch_without_a_next_bar_gives_no_entry_and_a_reason(
    direction,
) -> None:
    rows = walk()
    rows[SIGNAL] = S.bar(SIGNAL, o="100", h="106", low="94", c="105")
    del rows[SIGNAL + 1]  # no trades: no next-bar open exists
    # The next candle trades through the signal level on both sides.
    rows[SIGNAL + 2] = S.bar(SIGNAL + 2, o="104", h="112", low="90", c="95")
    found = resolve(rows, end=200, direction=direction)
    assert [t.horizon_bars for t in found] == [4, 16, 96]
    for trade in found:
        assert trade.missing_reason == "next_bar_no_trades"
        assert all(getattr(trade, name) is None for name in NUMBERS)
        assert trade.entry_bar_open_at == S.at(SIGNAL + 1)


def test_the_recorder_makes_no_virtual_entry_from_a_touch() -> None:
    rows = {i: S.bar(i) for i in range(205)}
    rows[200] = S.bar(200, o="100", h="106", low="99", c="105", q="200000000")
    del rows[201]
    rows[202] = S.bar(202, o="104", h="107", low="101", c="104")  # low touches 101
    now = S.run_after(204)
    report = asyncio.run(
        run_once(
            S.data_at(S.FakeUpbit({MARKET: list(rows.values())}), now),
            run_id="t",
            now=now,
            catchup_bars=5,
        )
    )
    assert report["rows"]
    assert report["virtual_trades"] == {
        "with_entry": 0,
        "missing": {"next_bar_no_trades": len(report["virtual_rows"])},
    }
    assert {t["virtual_entry_price"] for t in report["virtual_rows"]} == {None}


def test_the_entry_is_the_next_open_even_when_the_low_goes_further() -> None:
    rows = walk()
    rows[SIGNAL + 1] = S.bar(SIGNAL + 1, o="104", h="105", low="90", c="100")
    one_hour = resolve(rows, end=SIGNAL + 4)[0]
    assert one_hour.entry_source == "next_bar_open"
    assert one_hour.virtual_entry_price == Decimal("104")
    assert one_hour.virtual_exit_price == Decimal("100")
    assert one_hour.exit_basis == "exit_bar_close"
    assert one_hour.mae == Decimal("0.134615384615")  # (104 - 90) / 104
    assert one_hour.net_return == one_hour.gross_return - Decimal("0.005")
    assert one_hour.entry_tick_krw == Decimal("1")


def test_a_horizon_waits_for_its_exit_bar() -> None:
    rows = walk()
    assert resolve(rows, end=SIGNAL) == []
    assert resolve(rows, end=SIGNAL + 3) == []
    assert [t.horizon_bars for t in resolve(rows, end=SIGNAL + 4)] == [4]
    assert [t.horizon_bars for t in resolve(rows, end=SIGNAL + 95)] == [4, 16]
    done = frozenset({4, 16})
    found = resolve(rows, end=SIGNAL + 96, resolved=done)
    assert [t.horizon_bars for t in found] == [96]


def test_an_exit_slot_without_trades_keeps_the_last_traded_close() -> None:
    rows = walk()
    rows[SIGNAL + 3] = S.bar(SIGNAL + 3, h="103", c="102")
    del rows[SIGNAL + 4]
    one_hour = resolve(rows, end=SIGNAL + 4)[0]
    assert one_hour.exit_basis == "last_traded_close"
    assert one_hour.virtual_exit_price == Decimal("102")
    assert one_hour.gross_return == Decimal("0.02")


def test_a_page_that_does_not_reach_back_to_the_entry_decides_nothing() -> None:
    rows = {i: S.bar(i) for i in range(SIGNAL + 5, 210)}
    short = page(rows, covers_from=S.end_slot_after(SIGNAL + 5))
    assert (
        outcomes.resolve(
            pending(), short, end_slot=S.end_slot_after(200), now=S.run_after(200)
        )
        == []
    )


def test_a_page_without_the_recorded_signal_bar_decides_nothing() -> None:
    # An empty or truncated response must not become a permanent "no trades".
    empty = CandlePage(
        MARKET, {}, None, S.end_slot_after(205), S.run_after(204)
    )  # fmt: skip
    kwargs = {"end_slot": S.end_slot_after(200), "now": S.run_after(200)}
    assert outcomes.resolve(pending(), empty, **kwargs) == []
    gap = {i: r for i, r in walk().items() if i not in (SIGNAL, SIGNAL + 1)}
    assert outcomes.resolve(pending(), page(gap), **kwargs) == []


def test_an_unlisted_market_is_missing_only_after_the_horizon() -> None:
    found = outcomes.resolve(
        pending(), None, end_slot=S.end_slot_after(SIGNAL + 16), now=S.run_after(200)
    )
    assert [(t.horizon_bars, t.missing_reason) for t in found] == [
        (4, "market_unavailable"),
        (16, "market_unavailable"),
    ]
    assert all(t.virtual_entry_price is None and t.mae is None for t in found)


@pytest.mark.parametrize("direction", [1, -1])
def test_returns_and_excursions_equal_the_frozen_code(direction) -> None:
    rng = np.random.default_rng(3)
    price = 1000 * np.exp(np.cumsum(rng.normal(0, 0.004, 400)))
    rows = {}
    for i in range(400):
        if rng.random() < 0.1:
            continue
        o, c = price[i] * 0.999, price[i]
        rows[i] = S.bar(
            i,
            o=f"{o:.2f}",
            h=f"{max(o, c) * 1.002:.2f}",
            low=f"{min(o, c) * 0.997:.2f}",
            c=f"{c:.2f}",
        )
    base = S.end_slot_after(0)
    panel = build_panel(MARKET, {base + i: r for i, r in rows.items()}, base, 400)
    ctx = R.build_context(panel)
    checked = 0
    for s0 in range(20, 290, 11):
        item = K.PendingCandidate(
            1, MARKET, "x", direction, "signal", S.at(s0), Decimal("0.003")
        )
        trades = outcomes.resolve(
            item, page(rows), end_slot=S.end_slot_after(399), now=S.run_after(399)
        )
        for trade in trades:
            h = trade.horizon_bars
            if trade.missing_reason:
                assert s0 + 1 not in rows
                continue
            args = (panel, np.array([0]), np.array([s0]), h, 0)
            gross = direction * ctx.gross(h, 0)[0, s0]
            assert float(trade.gross_return) == pytest.approx(gross, abs=1e-11)
            assert float(trade.mae) == pytest.approx(
                R.adverse_excursion(*args, direction)[0], abs=1e-11
            )
            assert float(trade.mfe) == pytest.approx(
                R.adverse_excursion(*args, -direction)[0], abs=1e-11
            )
            ratio = float(trade.virtual_exit_price / trade.virtual_entry_price) - 1
            assert direction * ratio == pytest.approx(gross, abs=1e-11)
            assert slot_of(trade.exit_bar_open_at) == base + s0 + h
            checked += 1
    assert checked > 40
