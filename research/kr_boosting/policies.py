"""Policies as per-session candidate lists. Every policy shares the same frame.

A policy says, for each decision session ``t``, which listings it wants in rank
order and at what limit. Sizing, capacity, cash and fills belong to
``simulator`` and are identical for all of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from research.kr_boosting.config import (
    ADMIT_ENTRY_CAP,
    ADMIT_HORIZON,
    ADMIT_MIN_Q10,
    L1_LEVEL,
    LIQUIDITY_FLOOR_KRW,
    MEAN_REVERSION_LOOKBACK,
    MID_LEVEL,
    MOMENTUM_LOOKBACK,
    NEAR_LEVEL,
    ROUND_TRIP_COST,
    TOUCH_WINDOW,
    XRET_HOLD,
)
from research.kr_boosting.eligibility import Signal
from research.kr_boosting.features import FeatureStore
from research.kr_boosting.panel import STATE_VALID, Panel
from research.kr_corpus.d3_engine.tick import TickTable

_SPARE = 25  # candidates kept per session; later ranks replace held names


# KRX bands the fixture table of the evaluation contract uses. The frozen D3
# YAML lives outside the repository; these are the same seven bands.
def krx_ticks() -> TickTable:
    return TickTable.from_mapping(
        {
            "schema_version": "d3.krx_tick_table.v1",
            "bands": [
                {"lower_inclusive": 0, "upper_exclusive": 2000, "tick": 1},
                {"lower_inclusive": 2000, "upper_exclusive": 5000, "tick": 5},
                {"lower_inclusive": 5000, "upper_exclusive": 20000, "tick": 10},
                {"lower_inclusive": 20000, "upper_exclusive": 50000, "tick": 50},
                {"lower_inclusive": 50000, "upper_exclusive": 200000, "tick": 100},
                {"lower_inclusive": 200000, "upper_exclusive": 500000, "tick": 500},
                {"lower_inclusive": 500000, "upper_exclusive": None, "tick": 1000},
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    li: int
    t: int
    limit: Decimal
    rest_sessions: int
    hold: int  # sessions; meaning depends on hold_from
    hold_from_fill: bool  # False: exit at t + hold, True: exit at fill + hold
    score: float = 0.0


Plan = dict[int, list[Candidate]]  # keyed by decision session t


def _admit_candidate(
    panel: Panel, ticks: TickTable, li: int, t: int, score: float
) -> Candidate:
    close = Decimal(int(panel.close[li, t]))
    return Candidate(
        li=li,
        t=t,
        limit=ticks.align_buy(close * ADMIT_ENTRY_CAP),
        rest_sessions=1,
        hold=ADMIT_HORIZON,
        hold_from_fill=False,
        score=score,
    )


def _entry_candidate(
    panel: Panel, ticks: TickTable, li: int, t: int, level: Decimal, score: float
) -> Candidate:
    return Candidate(
        li=li,
        t=t,
        limit=ticks.align_buy(level),
        rest_sessions=TOUCH_WINDOW,
        hold=XRET_HOLD,
        hold_from_fill=True,
        score=score,
    )


def _ranked(rows: list[tuple[tuple, Candidate]]) -> list[Candidate]:
    return [c for _, c in sorted(rows, key=lambda item: item[0])][:_SPARE]


def _group(signals: Sequence[Signal]) -> dict[int, list[Signal]]:
    by_t: dict[int, list[Signal]] = {}
    for s in signals:
        by_t.setdefault(s.t, []).append(s)
    return by_t


# ------------------------------------------------------- unchanged policy


def _d3_rank(panel: Panel, s: Signal) -> tuple:
    return (round(s.rsi, 4), round(abs(s.l2_distance), 4), panel.symbols[s.li])


def admit_all_plan(panel: Panel, ticks: TickTable, signals: Sequence[Signal]) -> Plan:
    """Unchanged policy for H-admit: D3's own rank, nothing skipped."""

    return {
        t: _ranked(
            [
                (_d3_rank(panel, s), _admit_candidate(panel, ticks, s.li, t, 0.0))
                for s in rows
            ]
        )
        for t, rows in _group(signals).items()
    }


def anchor_plan(panel: Panel, ticks: TickTable, signals: Sequence[Signal]) -> Plan:
    """Unchanged policy for H-entry-price: the D3 L2 anchor at D3's rank."""

    return {
        t: _ranked(
            [
                (
                    _d3_rank(panel, s),
                    _entry_candidate(panel, ticks, s.li, t, s.l2_price, 0.0),
                )
                for s in rows
            ]
        )
        for t, rows in _group(signals).items()
    }


# ------------------------------------------------------- momentum, reversion


def _liquid_ranked(
    panel: Panel,
    store: FeatureStore,
    t: int,
    column: str,
    *,
    descending: bool,
) -> list[int]:
    values = store.arrays[column][:, t]
    traded = store.arrays["median_traded_20"][:, t]
    mask = (
        (panel.state[:, t] == STATE_VALID)
        & np.isfinite(values)
        & (np.nan_to_num(traded) >= LIQUIDITY_FLOOR_KRW)
    )
    idx = np.flatnonzero(mask)
    keys = -values[idx] if descending else values[idx]
    order = sorted(range(len(idx)), key=lambda i: (keys[i], panel.symbols[idx[i]]))
    return [int(idx[i]) for i in order[:_SPARE]]


def _control_plan(
    panel: Panel,
    ticks: TickTable,
    store: FeatureStore,
    sessions: Sequence[int],
    column: str,
    *,
    descending: bool,
    template: str,
) -> Plan:
    plan: Plan = {}
    for t in sessions:
        rows: list[Candidate] = []
        for li in _liquid_ranked(panel, store, t, column, descending=descending):
            if template == "admit":
                rows.append(_admit_candidate(panel, ticks, li, t, 0.0))
            else:
                close = Decimal(int(panel.close[li, t]))
                rows.append(
                    _entry_candidate(panel, ticks, li, t, close * NEAR_LEVEL, 0.0)
                )
        if rows:
            plan[t] = rows
    return plan


def momentum_plan(
    panel: Panel,
    ticks: TickTable,
    store: FeatureStore,
    sessions: Sequence[int],
    template: str,
) -> Plan:
    return _control_plan(
        panel,
        ticks,
        store,
        sessions,
        f"ret_{MOMENTUM_LOOKBACK}",
        descending=True,
        template=template,
    )


def mean_reversion_plan(
    panel: Panel,
    ticks: TickTable,
    store: FeatureStore,
    sessions: Sequence[int],
    template: str,
) -> Plan:
    return _control_plan(
        panel,
        ticks,
        store,
        sessions,
        f"ret_{MEAN_REVERSION_LOOKBACK}",
        descending=False,
        template=template,
    )


# ------------------------------------------------------------------ ladder


LEVEL_KINDS = ("near_0p5", "mid_1p5", "l1_3", "anchor_l2", "fib_nearest")


def ladder(panel: Panel, ticks: TickTable, s: Signal) -> list[tuple[str, Decimal]]:
    """Tick-floored rungs for one eligible entry; duplicate prices collapse."""

    close = Decimal(int(panel.close[s.li, s.t]))
    raw: list[tuple[str, Decimal]] = [
        ("near_0p5", close * NEAR_LEVEL),
        ("mid_1p5", close * MID_LEVEL),
        ("l1_3", close * L1_LEVEL),
        ("anchor_l2", s.l2_price),
    ]
    if s.fib_nearest is not None:
        raw.append(("fib_nearest", s.fib_nearest))
    seen: dict[Decimal, str] = {}
    for kind, price in raw:
        aligned = ticks.align_buy(price)
        if aligned > 0:
            seen.setdefault(aligned, kind)
    return [(kind, price) for price, kind in seen.items()]


# ------------------------------------------------------------------- models


def gbm_admit_plan(
    panel: Panel,
    ticks: TickTable,
    signals: Sequence[Signal],
    q10: np.ndarray,
    q50: np.ndarray,
) -> Plan:
    """Admit when q50 clears the round trip and q10 is above the tail floor."""

    rows: dict[int, list[tuple[tuple, Candidate]]] = {}
    for s, low, mid in zip(signals, q10, q50, strict=True):
        if not (mid > ROUND_TRIP_COST and low > ADMIT_MIN_Q10):
            continue
        cand = _admit_candidate(panel, ticks, s.li, s.t, float(mid))
        rows.setdefault(s.t, []).append(((-float(mid), panel.symbols[s.li]), cand))
    return {t: _ranked(items) for t, items in rows.items()}


@dataclass(frozen=True, slots=True)
class LadderRow:
    signal_index: int
    kind: str
    price: Decimal


def gbm_entry_plan(
    panel: Panel,
    ticks: TickTable,
    signals: Sequence[Signal],
    rows: Sequence[LadderRow],
    p_touch: np.ndarray,
    xret: np.ndarray,
) -> Plan:
    """Per entry, the rung with the best P(touch) x (xret - costs); skip if <= 0."""

    best: dict[int, tuple[float, LadderRow]] = {}
    for row, p, x in zip(rows, p_touch, xret, strict=True):
        score = float(p) * (float(x) - ROUND_TRIP_COST)
        current = best.get(row.signal_index)
        if current is None or score > current[0]:
            best[row.signal_index] = (score, row)
    by_t: dict[int, list[tuple[tuple, Candidate]]] = {}
    for index, (score, row) in best.items():
        if score <= 0:
            continue
        s = signals[index]
        cand = _entry_candidate(panel, ticks, s.li, s.t, row.price, score)
        by_t.setdefault(s.t, []).append(((-score, panel.symbols[s.li]), cand))
    return {t: _ranked(items) for t, items in by_t.items()}
