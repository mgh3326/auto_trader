"""KR Stage-B economic engine with an explicit run contract.

The signal calculation is intentionally not implemented here.  Stage-B calls
the shadow3 owner directly so a shadow/backtest semantic drift is impossible.
This module is research-only and has no broker, account, database, or scheduler
surface.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from math import sqrt
from statistics import mean, stdev
from typing import Any, Literal

try:
    from .holdout_guard import assert_date_not_holdout, assert_range_not_holdout
except ImportError:  # pragma: no cover - legacy flat-module test entrypoint
    from holdout_guard import assert_date_not_holdout, assert_range_not_holdout
try:
    from .pit import Bar, assert_no_lookahead
except ImportError:  # pragma: no cover - legacy flat-module test entrypoint
    from pit import Bar, assert_no_lookahead
from research.three_market_shadow.calculations import (
    CONTRACT_HASH,
    calculate_signal,
)
from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "APPROVED_COST_PROFILES",
    "KRCostContract",
    "StageBRunContract",
    "StageBResult",
    "Trade",
    "build_run_contract",
    "run_stage_b",
]

APPROVED_COST_PROFILES: Mapping[str, Mapping[str, int]] = {
    "43bp": {"fee_bp": 3, "transaction_tax_bp": 20, "slippage_bp_per_side": 10},
    "83bp": {"fee_bp": 3, "transaction_tax_bp": 20, "slippage_bp_per_side": 30},
}


@dataclass(frozen=True)
class KRCostContract:
    """Operator-approved literal cost binding; no implicit/default profile."""

    profile: Literal["43bp", "83bp"]
    fee_bp: int
    transaction_tax_bp: int
    slippage_bp_per_side: int

    @property
    def round_trip_bp(self) -> int:
        return self.fee_bp + self.transaction_tax_bp + 2 * self.slippage_bp_per_side

    def __post_init__(self) -> None:
        expected = APPROVED_COST_PROFILES.get(self.profile)
        actual = {
            "fee_bp": self.fee_bp,
            "transaction_tax_bp": self.transaction_tax_bp,
            "slippage_bp_per_side": self.slippage_bp_per_side,
        }
        if expected is None or actual != dict(expected):
            raise ValueError(
                "cost contract must exactly match an approved literal profile; "
                "no default cost injection is permitted"
            )


@dataclass(frozen=True)
class StageBRunContract:
    cost: KRCostContract
    window_start: date
    window_end: date
    holding_sessions: int = 5
    entry_timing: str = "t+1_open"
    exit_timing: str = "D+5_close"
    signal_contract_hash: str = CONTRACT_HASH

    def __post_init__(self) -> None:
        assert_range_not_holdout(self.window_start, self.window_end)
        if self.holding_sessions != 5:
            raise ValueError("Stage-B is fixed to D+5")
        if self.entry_timing != "t+1_open" or self.exit_timing != "D+5_close":
            raise ValueError("Stage-B timing contract drift")
        if self.signal_contract_hash != CONTRACT_HASH:
            raise ValueError("signal contract hash mismatch")

    @property
    def config_hash(self) -> str:
        return canonical_sha256(
            {
                "engine": "kr-stage-b-v1",
                "cost": {
                    "profile": self.cost.profile,
                    "fee_bp": self.cost.fee_bp,
                    "transaction_tax_bp": self.cost.transaction_tax_bp,
                    "slippage_bp_per_side": self.cost.slippage_bp_per_side,
                },
                "window_start": self.window_start.isoformat(),
                "window_end": self.window_end.isoformat(),
                "holding_sessions": self.holding_sessions,
                "entry_timing": self.entry_timing,
                "exit_timing": self.exit_timing,
                "signal_contract_hash": self.signal_contract_hash,
            }
        )


def build_run_contract(
    *,
    cost_profile: Literal["43bp", "83bp"],
    window_start: date,
    window_end: date,
) -> StageBRunContract:
    """Bind a cost profile only when the run explicitly names it."""
    literal = APPROVED_COST_PROFILES.get(cost_profile)
    if literal is None:
        raise ValueError(f"unknown explicit KR cost profile: {cost_profile!r}")
    return StageBRunContract(
        cost=KRCostContract(profile=cost_profile, **literal),
        window_start=window_start,
        window_end=window_end,
    )


@dataclass(frozen=True)
class Trade:
    symbol: str
    signal_session: date
    entry_session: date
    exit_session: date
    entry_open: int
    exit_close: int
    gross_return: float
    net_return: float
    cost_bp: int


@dataclass(frozen=True)
class StageBResult:
    contract: StageBRunContract
    trades: tuple[Trade, ...]
    skipped_signals: int
    lookahead_checks: int

    @property
    def net_returns(self) -> tuple[float, ...]:
        return tuple(trade.net_return for trade in self.trades)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": "kr-stage-b-v1",
            "signal_contract_hash": self.contract.signal_contract_hash,
            "config_hash": self.contract.config_hash,
            "cost_profile": self.contract.cost.profile,
            "cost_round_trip_bp": self.contract.cost.round_trip_bp,
            "entry_timing": self.contract.entry_timing,
            "exit_timing": self.contract.exit_timing,
            "window": {
                "start": self.contract.window_start.isoformat(),
                "end": self.contract.window_end.isoformat(),
            },
            "trades": [
                {
                    "symbol": trade.symbol,
                    "signal_session": trade.signal_session.isoformat(),
                    "entry_session": trade.entry_session.isoformat(),
                    "exit_session": trade.exit_session.isoformat(),
                    "entry_open": trade.entry_open,
                    "exit_close": trade.exit_close,
                    "gross_return": trade.gross_return,
                    "net_return": trade.net_return,
                    "cost_bp": trade.cost_bp,
                }
                for trade in self.trades
            ],
            "skipped_signals": self.skipped_signals,
            "lookahead_checks": self.lookahead_checks,
            "orders": 0,
            "account_mutations": 0,
        }


def _group_bars(
    bars: Iterable[Bar], contract: StageBRunContract
) -> dict[str, list[Bar]]:
    grouped: dict[str, list[Bar]] = {}
    for bar in bars:
        assert_date_not_holdout(bar.session_date)
        if contract.window_start <= bar.session_date <= contract.window_end:
            grouped.setdefault(bar.symbol, []).append(bar)
    for symbol, symbol_bars in grouped.items():
        symbol_bars.sort(key=lambda bar: bar.session_date)
        if len({bar.session_date for bar in symbol_bars}) != len(symbol_bars):
            raise ValueError(f"duplicate session for {symbol}")
    return grouped


def run_stage_b(
    *,
    bars: Iterable[Bar],
    contract: StageBRunContract,
) -> StageBResult:
    """Run t+1 open / D+5 close without using future bars for a signal."""
    if contract is None:  # type: ignore[comparison-overlap]
        raise ValueError("explicit Stage-B run contract is required")
    grouped = _group_bars(bars, contract)
    trades: list[Trade] = []
    skipped_signals = 0
    lookahead_checks = 0

    for symbol, symbol_bars in sorted(grouped.items()):
        active_until = -1
        for index, signal_bar in enumerate(symbol_bars):
            history = symbol_bars[: index + 1]
            assert_no_lookahead(history, signal_bar.session_date)
            lookahead_checks += 1
            signal = calculate_signal(
                "kr",
                {
                    "symbol": symbol,
                    "close": [bar.close for bar in history],
                    "volume": [bar.volume for bar in history],
                },
            )
            if signal["signal_state"] != "SIGNAL":
                continue
            entry_index = index + 1
            exit_index = index + contract.holding_sessions
            if index <= active_until or exit_index >= len(symbol_bars):
                skipped_signals += 1
                continue
            entry_bar = symbol_bars[entry_index]
            exit_bar = symbol_bars[exit_index]
            if entry_bar.open <= 0 or exit_bar.close <= 0:
                skipped_signals += 1
                continue
            gross = exit_bar.close / entry_bar.open - 1.0
            net = gross - contract.cost.round_trip_bp / 10_000
            trades.append(
                Trade(
                    symbol=symbol,
                    signal_session=signal_bar.session_date,
                    entry_session=entry_bar.session_date,
                    exit_session=exit_bar.session_date,
                    entry_open=entry_bar.open,
                    exit_close=exit_bar.close,
                    gross_return=gross,
                    net_return=net,
                    cost_bp=contract.cost.round_trip_bp,
                )
            )
            active_until = exit_index

    return StageBResult(
        contract=contract,
        trades=tuple(trades),
        skipped_signals=skipped_signals,
        lookahead_checks=lookahead_checks,
    )


def descriptive_trial_statistics(result: StageBResult) -> dict[str, float | int]:
    """Return descriptive statistics for evidence; no promotion decision."""
    returns = result.net_returns
    if len(returns) < 2:
        raise ValueError("trial evidence requires at least two closed trades")
    average = mean(returns)
    deviation = stdev(returns)
    sharpe = 0.0 if deviation == 0 else average / deviation * sqrt(len(returns))
    return {
        "sharpe": sharpe,
        "p_value": 1.0,
        "sample_size": len(returns),
        "validation_score": average,
    }
