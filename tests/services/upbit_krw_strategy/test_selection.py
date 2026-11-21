from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.upbit_krw_strategy import selection as sel
from app.services.upbit_krw_strategy.signals import Signal
from app.services.upbit_krw_strategy.universe import Eligibility

D = Decimal


def elig(market: str, total: str | None = "300", ok: bool = True) -> Eligibility:
    return Eligibility(
        market, ok, "ok" if ok else "inactive",
        D("10") if ok else None, None if total is None else D(total), D("1000"),
    )  # fmt: skip


def sig(enter: bool) -> Signal:
    return Signal("trend", enter, "ok" if enter else "r60_not_positive", D("0.1"), None)


def run(markets, *, held=(), banned=(), open_positions=0, signals=None):
    signals = signals or {m: sig(True) for m in markets}
    return {
        d.market: (d.selected, d.reason_code)
        for d in sel.select_entries(
            signals=signals,
            eligibility={m: elig(m, str(100 + i)) for i, m in enumerate(markets)},
            held_markets=held,
            banned_markets=banned,
            open_positions=open_positions,
        )
    }


def test_surplus_candidates_are_ranked_by_liquidity_then_cut_at_four() -> None:
    markets = ["KRW-A", "KRW-B", "KRW-C", "KRW-D", "KRW-E", "KRW-F"]
    result = run(markets)  # turnover rises with the index, F is the most liquid
    chosen = sorted(m for m, (picked, _) in result.items() if picked)
    assert chosen == ["KRW-C", "KRW-D", "KRW-E", "KRW-F"]
    assert result["KRW-A"] == (False, "no_free_slot")


def test_open_positions_reduce_the_free_slots() -> None:
    result = run(["KRW-A", "KRW-B", "KRW-C"], open_positions=3)
    assert [m for m, (p, _) in result.items() if p] == ["KRW-C"]
    assert run(["KRW-A"], open_positions=4)["KRW-A"] == (False, "no_free_slot")
    assert run(["KRW-A"], open_positions=9)["KRW-A"] == (False, "no_free_slot")


def test_a_held_market_is_never_bought_again() -> None:
    assert run(["KRW-A"], held=["KRW-A"])["KRW-A"] == (False, "already_held")


def test_banned_market_and_ineligible_and_no_signal() -> None:
    result = run(["KRW-A", "KRW-B"], banned=["KRW-A"])
    assert result["KRW-A"] == (False, "reentry_banned")
    assert result["KRW-B"] == (True, "selected")
    out = sel.select_entries(
        signals={"KRW-X": sig(True), "KRW-Y": sig(False)},
        eligibility={"KRW-X": elig("KRW-X", ok=False), "KRW-Y": elig("KRW-Y")},
        held_markets=(),
        banned_markets=(),
        open_positions=0,
    )
    assert {d.market: d.reason_code for d in out} == {
        "KRW-X": "not_eligible", "KRW-Y": "no_signal",
    }  # fmt: skip


def test_equal_liquidity_breaks_ties_by_market_code() -> None:
    out = sel.select_entries(
        signals={m: sig(True) for m in ("KRW-B", "KRW-A")},
        eligibility={m: elig(m, "5") for m in ("KRW-B", "KRW-A")},
        held_markets=(),
        banned_markets=(),
        open_positions=3,
    )
    assert {d.market: d.selected for d in out} == {"KRW-A": True, "KRW-B": False}


def test_late_issue_boundary_is_sixty_minutes() -> None:
    closed = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)  # 09:00 KST
    assert not sel.is_late_issue(
        candle_closed_at=closed, issued_at=closed + timedelta(minutes=60)
    )
    assert sel.is_late_issue(
        candle_closed_at=closed, issued_at=closed + timedelta(minutes=60, seconds=1)
    )
    with pytest.raises(ValueError):
        sel.is_late_issue(candle_closed_at=datetime(2026, 11, 22), issued_at=closed)
