"""Shared deterministic fixtures for the evaluation-contract tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from research.eval_contract.contract import RegistryRef
from research.kr_corpus.d3_engine.acceptance import _contract_signal_bars
from research.kr_corpus.d3_engine.models import Arm, CashflowView, PortfolioRunInput
from research.kr_corpus.d3_engine.tick import TickTable

REGISTRY = RegistryRef("H-kr-d3-fixture", "ab" * 32, 1)
LEGACY_TERMINAL_NAV = Decimal("10020402.57250")


def ticks() -> TickTable:
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


def session_days(n: int = 201) -> list[date]:
    return [date(2014, 1, 1) + timedelta(days=i) for i in range(n)]


def make_run_input(
    arm: Arm = Arm.B0,
    *,
    symbols: tuple[str, ...] = ("005930",),
    last_low: Decimal | None = None,
) -> PortfolioRunInput:
    sessions = session_days()
    decision = sessions[-121:]
    bars = list(_contract_signal_bars(decision, symbols=symbols))
    if last_low is not None:
        bars = [
            replace(b, low=last_low) if b.session == decision[-1] else b for b in bars
        ]
    return PortfolioRunInput(
        arm=arm,
        cashflow_view=CashflowView.NO_CONTRIBUTION,
        bars=tuple(bars),
        market_sessions=tuple(sessions),
        index_closes=tuple(
            (session, Decimal(2000 + i)) for i, session in enumerate(sessions)
        ),
        decision_start=decision[-1],
    )
