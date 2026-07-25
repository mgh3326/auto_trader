"""ROB-944 (H4, ROB-940) — per-fold TRAIN selection authority (pure, stdlib).

Selection authority (orch-fable-answer-strategy-20260717.md, confirmed final
by ``orch-fable-answer-rob944-20260717.md``): the ONE AND ONLY selection/pass
authority is the eligible-symbol EQUAL-WEIGHT MEAN of per-symbol TRAIN net
expectancy (bps/trade) under the independent 17bp primary-stress run.

Trade-level POOLED expectancy (this module still computes and reports it,
per the H5 report contract) is REPORT-ONLY -- ``select_fold_config`` never
reads ``pooled_expectancy_bps`` when ranking. A symbol with fewer than
``MIN_SYMBOL_TRAIN_TRADES`` completed train trades is excluded from a
config's equal-weight mean (``insufficient_symbol_evidence``); if fewer than
``MIN_ELIGIBLE_SYMBOLS`` symbols remain eligible, the config itself is
rejected (``rejected:insufficient_train_evidence``) for that fold but stays
visible in the trace for H6 trial accounting -- never silently dropped.

Ranking of the (exactly 12) candidates: descending equal-weight expectancy,
then descending profit factor, then ascending canonical ``config_id``. NaN/
Inf inputs, a wrong candidate count, and duplicate config_id/symbol are all
rejected fail-closed before any ranking math runs.

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
existing research_contracts canonical-hash authority, deterministic given
its input.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from research_contracts.canonical_hash import canonical_sha256

MIN_SYMBOL_TRAIN_TRADES = 5
MIN_ELIGIBLE_SYMBOLS = 2
INSUFFICIENT_SYMBOL_EVIDENCE_REASON = "insufficient_symbol_evidence"
INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON = "rejected:insufficient_train_evidence"

_EXPECTED_CONFIGS_PER_STRATEGY = 12


@dataclass(frozen=True)
class SymbolTrainEvidence:
    """One symbol's TRAIN-window evidence for one (strategy, config) row, at
    the primary-stress (17bp) cost scenario. ``gross_profit_bps``/
    ``gross_loss_bps`` are the sums of positive/|negative| ``net_bps`` across
    that symbol's completed train trades -- used ONLY for the report-only
    pooled expectancy and the profit-factor tie-break, never for eligibility.

    ``train_artifact_hash`` (captain correction, 2026-07-17) is the H4-owned
    scenario artifact hash (``rob944_scenario_evidence.scenario_artifact_hash``)
    of the ACTUAL train scenario run this evidence was derived from -- i.e.
    it is bound to the exact train bar slice + generated signals' resulting
    trade/no-trade evidence, not merely a re-hash of the four aggregate
    metrics above. Two genuinely different underlying inputs that happen to
    average to the same aggregate metrics MUST still produce different
    ``train_artifact_hash`` values (and therefore a different
    ``ConfigSelectionOutcome.train_input_hash``) -- this is what makes the
    selection trace's "preserve train input hashes" contract actually true
    rather than a re-hash of already-lossy aggregates.

    ``no_trade_reason_counts`` (captain Q3-enforcement follow-up,
    2026-07-17) exposes the SAME no-trade reason histogram
    ``rob944_scenario_evidence.ScenarioRunOutcome`` carries for this train
    run -- so ``funding_evidence_unavailable``/
    ``expected_funding_cost_above_3bps`` (and every other no-trade reason)
    remain separately countable for TRAIN evidence too, not collapsed into
    the generic ``insufficient_symbol_evidence`` eligibility reason.
    """

    symbol: str
    completed_trades: int
    net_expectancy_bps: float  # mean net_bps/trade, TRAIN window, @17bp
    gross_profit_bps: float
    gross_loss_bps: float
    train_artifact_hash: str
    no_trade_reason_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.completed_trades < 0:
            raise ValueError(
                f"{self.symbol}: completed_trades must be >= 0, got "
                f"{self.completed_trades!r}"
            )
        for name in ("net_expectancy_bps", "gross_profit_bps", "gross_loss_bps"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(
                    f"{self.symbol}: SymbolTrainEvidence.{name} must be finite, "
                    f"got {value!r}"
                )
        if self.gross_profit_bps < 0 or self.gross_loss_bps < 0:
            raise ValueError(
                f"{self.symbol}: gross_profit_bps/gross_loss_bps must be >= 0"
            )
        if not self.train_artifact_hash:
            raise ValueError(f"{self.symbol}: train_artifact_hash must be non-empty")


@dataclass(frozen=True)
class ConfigTrainCandidate:
    """One (strategy) config's per-symbol train evidence, as actually
    computed -- callers omit a symbol entirely only if it produced zero
    evidence rows; this dataclass rejects duplicate symbol entries."""

    config_id: str
    symbol_evidence: tuple[SymbolTrainEvidence, ...]

    def __post_init__(self) -> None:
        symbols = [e.symbol for e in self.symbol_evidence]
        if len(set(symbols)) != len(symbols):
            raise ValueError(
                f"{self.config_id}: duplicate symbol evidence in "
                f"symbol_evidence {symbols}"
            )


@dataclass(frozen=True)
class ConfigSelectionOutcome:
    """One config's evaluated selection outcome -- present in the trace for
    EVERY candidate, rejected or not (H6 trial accounting must see rejected
    configs, not just the eventual winner). ``no_trade_reason_counts`` is the
    SUM of every symbol's (eligible AND excluded) train no-trade reason
    histogram -- campaign-report-visible funding/gap/other reason exposure
    for this config's fold-level train evaluation."""

    config_id: str
    eligible_symbols: tuple[str, ...]
    excluded_symbols: tuple[tuple[str, str], ...]  # (symbol, reason)
    equal_weight_expectancy_bps: float | None  # None iff rejected
    pooled_expectancy_bps: float | None  # report-only; None iff rejected
    profit_factor: float  # tie-break value only; meaningless when rejected
    rejected: bool
    rejection_reason: str | None
    train_input_hash: str
    no_trade_reason_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FoldSelectionTrace:
    """The full per-fold, per-strategy selection trace: every candidate in
    ORIGINAL input order (not sorted by score), plus the winner (or ``None``
    if every candidate was rejected for that fold)."""

    strategy: str
    candidates: tuple[ConfigSelectionOutcome, ...]
    selected_config_id: str | None


def _train_input_hash(candidate: ConfigTrainCandidate) -> str:
    """Bound to each symbol's ACTUAL train scenario evidence
    (``train_artifact_hash``, itself derived from the exact train bar slice
    + generated signals' resulting trades/no-trades) as well as the
    aggregate metrics -- two inputs that differ only underneath (different
    bars/signals producing the same aggregate bps) still change this hash,
    because ``train_artifact_hash`` differs."""
    payload = {
        "config_id": candidate.config_id,
        "symbol_evidence": [
            {
                "symbol": e.symbol,
                "completed_trades": e.completed_trades,
                "net_expectancy_bps": e.net_expectancy_bps,
                "gross_profit_bps": e.gross_profit_bps,
                "gross_loss_bps": e.gross_loss_bps,
                "train_artifact_hash": e.train_artifact_hash,
            }
            for e in candidate.symbol_evidence
        ],
    }
    return canonical_sha256(payload)


def _aggregate_no_trade_reason_counts(
    symbol_evidence: Sequence[SymbolTrainEvidence],
) -> dict[str, int]:
    combined: dict[str, int] = {}
    for e in symbol_evidence:
        for reason, count in e.no_trade_reason_counts.items():
            combined[reason] = combined.get(reason, 0) + count
    return combined


def evaluate_config_candidate(
    candidate: ConfigTrainCandidate,
) -> ConfigSelectionOutcome:
    """Evaluate ONE config's eligibility/score in isolation (no ranking)."""
    eligible: list[SymbolTrainEvidence] = []
    excluded: list[tuple[str, str]] = []
    for e in candidate.symbol_evidence:
        if e.completed_trades < MIN_SYMBOL_TRAIN_TRADES:
            excluded.append((e.symbol, INSUFFICIENT_SYMBOL_EVIDENCE_REASON))
        else:
            eligible.append(e)

    train_input_hash = _train_input_hash(candidate)
    no_trade_reason_counts = _aggregate_no_trade_reason_counts(
        candidate.symbol_evidence
    )

    if len(eligible) < MIN_ELIGIBLE_SYMBOLS:
        return ConfigSelectionOutcome(
            config_id=candidate.config_id,
            eligible_symbols=tuple(e.symbol for e in eligible),
            excluded_symbols=tuple(excluded),
            equal_weight_expectancy_bps=None,
            pooled_expectancy_bps=None,
            profit_factor=math.nan,
            rejected=True,
            rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
            train_input_hash=train_input_hash,
            no_trade_reason_counts=no_trade_reason_counts,
        )

    equal_weight = sum(e.net_expectancy_bps for e in eligible) / len(eligible)
    total_trades = sum(e.completed_trades for e in eligible)
    pooled = (
        sum(e.net_expectancy_bps * e.completed_trades for e in eligible) / total_trades
        if total_trades > 0
        else None
    )
    gross_profit = sum(e.gross_profit_bps for e in eligible)
    gross_loss = sum(e.gross_loss_bps for e in eligible)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = 1.0  # degenerate breakeven (no profit, no loss)

    return ConfigSelectionOutcome(
        config_id=candidate.config_id,
        eligible_symbols=tuple(e.symbol for e in eligible),
        excluded_symbols=tuple(excluded),
        equal_weight_expectancy_bps=equal_weight,
        pooled_expectancy_bps=pooled,
        profit_factor=profit_factor,
        rejected=False,
        rejection_reason=None,
        train_input_hash=train_input_hash,
        no_trade_reason_counts=no_trade_reason_counts,
    )


def select_fold_config(
    strategy: str, candidates: Sequence[ConfigTrainCandidate]
) -> FoldSelectionTrace:
    """Evaluate all (exactly 12) candidates and select the fold's winner.

    Fail-closed BEFORE any ranking math: exactly
    ``_EXPECTED_CONFIGS_PER_STRATEGY`` candidates, all with unique
    ``config_id``. Ranking key (descending): equal-weight expectancy, profit
    factor, then ascending ``config_id`` -- NEVER pooled expectancy.
    """
    if len(candidates) != _EXPECTED_CONFIGS_PER_STRATEGY:
        raise ValueError(
            f"{strategy}: expected exactly {_EXPECTED_CONFIGS_PER_STRATEGY} "
            f"config candidates, got {len(candidates)}"
        )
    ids = [c.config_id for c in candidates]
    if len(set(ids)) != len(ids):
        duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
        raise ValueError(f"{strategy}: duplicate config_id(s) {duplicates}")

    outcomes = tuple(evaluate_config_candidate(c) for c in candidates)
    eligible_outcomes = [o for o in outcomes if not o.rejected]

    selected_config_id: str | None = None
    if eligible_outcomes:
        winner = min(
            eligible_outcomes,
            key=lambda o: (
                -o.equal_weight_expectancy_bps,
                -o.profit_factor,
                o.config_id,
            ),
        )
        selected_config_id = winner.config_id

    return FoldSelectionTrace(
        strategy=strategy, candidates=outcomes, selected_config_id=selected_config_id
    )
