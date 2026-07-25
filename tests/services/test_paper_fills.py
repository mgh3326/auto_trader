"""ROB-703 — pure fill-engine helpers for the paper resting-limit sim.

Decimal-in / Decimal-or-None-out. No I/O, no LLM, no DB.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.paper_fills import limit_crossed, snap_limit_down


@pytest.mark.unit
def test_snap_limit_down_bands() -> None:
    # >=2M band=1000
    assert snap_limit_down(Decimal("95001234")) == Decimal("95001000")
    # >=1M band=500
    assert snap_limit_down(Decimal("1234567")) == Decimal("1234500")
    # >=1k band=5
    assert snap_limit_down(Decimal("2317")) == Decimal("2315")
    # >=1 band=0.01
    assert snap_limit_down(Decimal("3.037")) == Decimal("3.03")


@pytest.mark.unit
def test_snap_limit_down_below_minimum_uses_micro_unit() -> None:
    # < 0.01 → 0.00001 tick (floor)
    assert snap_limit_down(Decimal("0.003037")) == Decimal("0.00303")


@pytest.mark.unit
def test_limit_crossed_buy_fills_when_low_touches() -> None:
    # buy limit 100; a bar dipped to 99 -> filled at 100
    assert limit_crossed(
        "buy",
        Decimal("100"),
        [
            (Decimal("101"), Decimal("105")),
            (Decimal("99"), Decimal("102")),
        ],
    ) == Decimal("100")


@pytest.mark.unit
def test_limit_crossed_buy_no_fill_when_low_above() -> None:
    assert (
        limit_crossed(
            "buy",
            Decimal("100"),
            [(Decimal("101"), Decimal("110"))],
        )
        is None
    )


@pytest.mark.unit
def test_limit_crossed_sell_fills_when_high_touches() -> None:
    assert limit_crossed(
        "sell",
        Decimal("100"),
        [(Decimal("95"), Decimal("101"))],
    ) == Decimal("100")


@pytest.mark.unit
def test_limit_crossed_sell_no_fill_when_high_below() -> None:
    assert (
        limit_crossed(
            "sell",
            Decimal("100"),
            [(Decimal("90"), Decimal("99"))],
        )
        is None
    )


@pytest.mark.unit
def test_limit_crossed_empty_bars_none() -> None:
    assert limit_crossed("buy", Decimal("100"), []) is None


@pytest.mark.unit
def test_limit_crossed_case_insensitive_side() -> None:
    # upper-case BUY should still fill on the same rule
    assert limit_crossed(
        "BUY",
        Decimal("100"),
        [(Decimal("90"), Decimal("95"))],
    ) == Decimal("100")
