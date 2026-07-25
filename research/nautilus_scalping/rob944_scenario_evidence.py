"""ROB-944 (H4, ROB-940) — H4-owned scenario terminal-evidence contract (pure,
stdlib).

Captain audit supplement (2026-07-17): ``rob940_engine.ledger_hash`` hashes
ONLY the trade ledger (that is its AC1 determinism contract, and correct for
that narrow purpose) -- it must NEVER be used as the full scenario artifact
hash, because two runs whose trade ledgers are both empty (e.g. every signal
was rejected ``daily_stop_active`` vs every signal was rejected
``tp_below_min_distance``) would otherwise hash identically, silently losing
the no-trade/reason distribution. ``scenario_artifact_hash`` binds identity
(strategy/config/symbol/fold/scenario) + status + the ordered trade ledger +
the no-trade reason histogram, so any of those differing changes the hash.

Captain Q3-enforcement correction (2026-07-17): a hash that merely COMMITS
to the no-trade reason histogram is not REPORT EXPOSURE -- Fable Q3 requires
``funding_evidence_unavailable``/``expected_funding_cost_above_3bps`` (and
every other no-trade reason, including ``rejected:data_gap_in_position``) to
remain separately countable downstream, not hidden behind a SHA-256 digest.
``ScenarioRunOutcome.no_trade_reason_counts`` exposes the SAME histogram
``scenario_artifact_hash`` hashes, as a real, readable field.

``ScenarioRunOutcome`` is the H4-owned, REQUIRED (never-optional) per-scenario
terminal contract -- unlike ``app.schemas.research_campaign_bridge.
ScenarioEvidence`` (H6's generic DTO), whose ``artifact_hash`` is
``str | None`` because H6 must serve any campaign, not just this one. H4
always supplies ``status``, ``artifact_hash``, and
``no_trade_reason_counts`` for every one of a fold's 3 scenario runs.
``status`` has FOUR values: ``completed`` (evidence generation succeeded --
NOT a strategy PASS verdict), ``rejected`` (the trial itself is invalid --
e.g. a position touched a data gap, so ANY trade ledger from that run is
untrustworthy and must not be salvaged), ``crashed``/``timeout`` (the child
invocation itself failed). ``rob944_walkforward`` is the caller that decides
when each applies; this module only defines the per-scenario shape.

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
existing research_contracts canonical-hash authority and ``rob940_engine``'s
result types, deterministic given its input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from rob940_engine import EngineResult

from research_contracts.canonical_hash import canonical_sha256

ScenarioRunStatus = Literal["completed", "rejected", "crashed", "timeout"]

_EXPECTED_SCENARIO_NAMES = ("base", "primary_stress", "upward_stress")


@dataclass(frozen=True)
class ScenarioRunOutcome:
    """One (strategy, config, symbol, fold)'s single cost-scenario run
    outcome. ``status="completed"`` means the engine invocation produced
    evidence successfully -- NOT a strategy PASS verdict (same "completed
    != PASS" discipline as ROB-846/ROB-946). ``no_trade_reason_counts`` is
    the SAME reason -> count histogram ``scenario_artifact_hash`` commits
    to, exposed here as real, readable, countable data (never just implicit
    in a hash) -- this is what makes ``funding_evidence_unavailable``/
    ``expected_funding_cost_above_3bps``/etc. Fable-Q3-report-visible rather
    than merely hash-committed.
    """

    scenario_name: str
    status: ScenarioRunStatus
    trade_count: int
    artifact_hash: str
    error_reason: str | None = None
    no_trade_reason_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.scenario_name not in _EXPECTED_SCENARIO_NAMES:
            raise ValueError(
                f"unknown scenario_name {self.scenario_name!r}, expected one of "
                f"{_EXPECTED_SCENARIO_NAMES}"
            )
        if self.trade_count < 0:
            raise ValueError(f"trade_count must be >= 0, got {self.trade_count!r}")
        if self.status != "completed" and self.error_reason is None:
            raise ValueError(
                f"{self.scenario_name}: error_reason is required for non-completed "
                f"status {self.status!r}"
            )


def no_trade_reason_counts(result: EngineResult) -> dict[str, int]:
    """The reason -> count histogram over ``result.no_trades`` -- the SAME
    computation ``scenario_artifact_hash`` folds into its digest, exposed as
    a standalone, reusable, readable function (Fable Q3 report exposure)."""
    counts: dict[str, int] = {}
    for nt in result.no_trades:
        counts[nt.reason] = counts.get(nt.reason, 0) + 1
    return counts


def scenario_artifact_hash(
    result: EngineResult,
    *,
    strategy: str,
    config_id: str,
    symbol: str,
    fold_id: str,
    scenario_name: str,
) -> str:
    """Full terminal-evidence hash for one scenario run.

    Binds identity + the ordered trade ledger + a no-trade REASON HISTOGRAM
    (reason -> count, plus the total). Two runs with equally-empty trade
    ledgers but different no-trade reason distributions hash differently;
    two runs with the same reason histogram (even over different
    ``signal_ts`` values) hash the same -- this is a deliberate, documented
    identity-level summary, not a full no-trade-record dump.
    """
    reason_counts = no_trade_reason_counts(result)

    payload = {
        "strategy": strategy,
        "config_id": config_id,
        "symbol": symbol,
        "fold_id": fold_id,
        "scenario_name": scenario_name,
        "trades": [
            {
                "side": t.side,
                "signal_ts": t.signal_ts,
                "entry_ts": t.entry_ts,
                "entry_price": t.entry_price,
                "exit_ts": t.exit_ts,
                "exit_price": t.exit_price,
                "exit_reason": t.exit_reason,
                "gross_bps": t.gross_bps,
                "fee_bps": t.fee_bps,
                "all_in_bps": t.all_in_bps,
                "funding_bps": t.funding_bps,
                "net_bps": t.net_bps,
                "gap_fill": t.gap_fill,
            }
            for t in result.trades
        ],
        "no_trade_reason_counts": reason_counts,
        "no_trade_total": len(result.no_trades),
    }
    return canonical_sha256(payload)


def scenario_run_outcome_from_engine_result(
    result: EngineResult,
    *,
    strategy: str,
    config_id: str,
    symbol: str,
    fold_id: str,
    scenario_name: str,
) -> ScenarioRunOutcome:
    """Build a ``completed`` outcome from a successful engine invocation.

    Non-``completed`` outcomes (``rejected``/``crashed``/``timeout``) are
    constructed directly by the caller (the walk-forward runner) at the
    point a trial is invalidated or a child invocation actually fails --
    this factory only covers the success path.
    """
    artifact_hash = scenario_artifact_hash(
        result,
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        fold_id=fold_id,
        scenario_name=scenario_name,
    )
    return ScenarioRunOutcome(
        scenario_name=scenario_name,
        status="completed",
        trade_count=len(result.trades),
        artifact_hash=artifact_hash,
        no_trade_reason_counts=no_trade_reason_counts(result),
    )
