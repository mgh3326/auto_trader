from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from research.us_stage_b.contracts import USCostLiteral, USStageBRunContract
from research.us_stage_b.registry import CandidateBinding, CandidateRegistry
from research.us_stage_b.source import USStageBDailyBar

FROZEN_YAML = Path(
    "/Users/mgh3326/Documents/prior-art-map-v1/02-active-candidates.yaml"
)


@pytest.fixture(scope="session")
def registry() -> CandidateRegistry:
    return CandidateRegistry.load(FROZEN_YAML)


def candidate(registry: CandidateRegistry, strategy_id: str) -> CandidateBinding:
    return registry.binding_for(strategy_id)


def contract(
    candidate_binding: CandidateBinding,
    sessions: tuple[date, ...],
) -> USStageBRunContract:
    return USStageBRunContract(
        candidate=candidate_binding,
        exploration_start=sessions[0],
        exploration_end=sessions[-1],
        cost=USCostLiteral(base_bp_per_side=10, sensitivity_bp_per_side=5),
    )


def sequential_sessions(
    count: int,
    *,
    start: date = date(2023, 1, 3),
) -> tuple[date, ...]:
    return tuple(start + timedelta(days=index) for index in range(count))


def volbreak_bars(
    symbol: str,
    sessions: tuple[date, ...],
    *,
    adv_multiplier: float = 1.0,
    no_entry_open: bool = False,
    omit_exit: bool = False,
) -> tuple[USStageBDailyBar, ...]:
    """Build a single exact VOLBREAK signal at session index 55."""

    bars: list[USStageBDailyBar] = []
    base_volume = 50_000.0 * adv_multiplier
    for index, session in enumerate(sessions):
        adjusted_close = 100.0 + index * 0.1
        volume = base_volume
        if index == 55:
            adjusted_close = 107.0
            volume = 2.0 * base_volume
        elif index > 55:
            adjusted_close = 106.0
        if omit_exit and index == 66:
            continue
        bars.append(
            USStageBDailyBar(
                symbol=symbol,
                session_date=session,
                open=None if no_entry_open and index == 56 else adjusted_close,
                adjusted_close=adjusted_close,
                volume=volume,
            )
        )
    return tuple(bars)
