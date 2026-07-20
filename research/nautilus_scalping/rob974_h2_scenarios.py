"""ROB-979 (H2, ROB-974 R2) CP4 -- scenarios, funding, provenance, deterministic
ledgers (pure, stdlib).

H4 (not built here) invokes this module's ledger builders ONCE PER explicit
path scenario -- base13/primary_stress17/upward_stress22 -- each call a fresh,
independent, stateless computation over whatever raw membership H4 supplies
that call. Nothing in this module holds state ACROSS calls.

ultrathink decisions (frozen for CP4-CP5; revisit only if orch authority
changes -- see ``/tmp/strategy-worker-rob979-sonnet-checkpoints.md`` CP4 entry):

  * ``path_scenario`` labels (``base13``/``primary_stress17``/
    ``upward_stress22``) are THIS module's own literal strings, matching the
    H2 doc's exact AC27 wording -- distinct from
    ``rob940_cost_model.CostScenario.name`` (``"base"``/``"primary_stress"``/
    ``"upward_stress"``, no bp suffix). The underlying cost economics ARE
    reused verbatim (``COST_SCENARIO_BASE``/``_PRIMARY_STRESS``/
    ``_UPWARD_STRESS`` and ``net_bps``, imported not reimplemented); only the
    ledger-row provenance LABEL is this module's own, because H4's contract
    is written in terms of the bp-suffixed names.
  * AC29 ("a row stores gross and E13/E17/E22 ... for its own membership
    only") is read as: EVERY row, regardless of which scenario call produced
    it, carries ALL THREE E13/E17/E22 columns (three cheap subtractions of
    the same ``gross_bps``/``funding_bps`` -- there is no reason to withhold
    two of them). "Its own membership only" is the caveat that a caller must
    never merge/compare rows ACROSS two different ledger-builder CALLS as if
    they were one population (since H4 could in principle feed genuinely
    different candidate sets per scenario run) -- not a restriction on which
    columns exist on a single row. ``path_scenario`` is exactly that row's
    provenance tag for which call produced it.
  * "Fresh engine state... no hidden API may linearly revalue a shared
    ledger" (AC27) is satisfied by these builders being ordinary pure
    functions with NO module-level mutable cache and no in-place mutation of
    their ``trades`` argument -- each call independently re-derives every
    column from scratch. Two calls on the SAME raw trades produce two
    structurally-independent output tuples (never the same list object,
    never aliased); two calls on DIFFERENT raw trades naturally diverge in
    membership/count, and this module never asserts or relies on equality
    between them (ROB-979's v1 S3/S4 execution mechanics are not
    cost-scenario-gated the way ROB-940's day-halt was, so membership
    happens to be identical when the SAME raw trades are supplied to every
    scenario call -- but nothing here assumes that; H4 remains free to
    supply different membership per call, e.g. across genuinely different
    fold/config runs, and this module's independence guarantees still hold).
  * Funding reuses ``rob941_funding_sidecar``'s frozen PIT
    ``[entry_ts, exit_ts)`` realized-crossing window via a caller-supplied
    ``funding_lookup(symbol, side, entry_ts, exit_ts) -> Sequence[FundingCrossing]``
    (the same shape as ``rob944_gap_funding.build_funding_lookup`` produces)
    -- H2 does not reimplement crossing selection. S3 funding is the single
    leg's signed ``realized_funding_bps`` (reused from ``rob940_cost_model``).
    S4 funding is ``weight_a*funding_a + weight_b*funding_b`` -- entry-frozen
    weights applied to each leg's OWN signed funding, summing to "once on
    basket notional" exactly because ``weight_a+weight_b==1`` (mirrors the
    cost-once principle already enforced on ``gross_bps`` itself in
    ``rob974_h2_s4_engine``, which computes basket return via the SAME
    weighted-once construction).
  * Every scenario E-value reuses ``rob940_cost_model.net_bps`` verbatim
    (``gross - cost_scenario.all_in_bps - funding``, each subtracted exactly
    once) -- not a local reimplementation, so the "subtracted exactly once"
    invariant is provably the SAME code path S1/S2 already rely on.
  * ``thesis_exit_flag``: S3 true only for ``exit_reason=="THESIS_EXIT"``; S4
    true for ``exit_reason in ("MEAN_EXIT","STALL_EXIT")`` (AC31: "S4
    MEAN/STALL"). ``timeout_flag`` true only for ``exit_reason=="TIMEOUT"``
    in both.
  * Canonical ordering/hash (AC33) reuses
    ``research_contracts.canonical_hash.canonical_sha256`` (the repo's
    typed-AST/float-hex authority, same as ``rob940_engine.ledger_hash``) over
    a manually-built list of per-row dicts in a FIXED sort key
    (``(signal_ts, symbol)`` for S3, ``(signal_ts, pair)`` for S4) -- never
    the caller's input order, dict/set iteration order, or filesystem order.
  * There is deliberately NO ``COST_SCENARIO_ZERO``/"E0" builder or constant
    anywhere in this module (AC34/item 39): ``gross_bps`` is already
    preserved unchanged on every row (via the embedded raw trade), which is
    sufficient for H4/H5 to derive E0 downstream from exact primary
    membership without this module replaying or linearly revaluing anything.
  * No broad exception handling anywhere in this module: a broken
    ``funding_lookup`` (or any other caller-supplied callable) propagates its
    ORIGINAL exception unchanged, so H4's first catch can hand the live
    object to the ROB-970 sanitizer (AC37/item 37).

No DB/network/app/broker/order/fill/scheduler/random/current-time imports --
pure stdlib, deterministic given its input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rob940_cost_model import (
    COST_SCENARIO_BASE,
    COST_SCENARIO_PRIMARY_STRESS,
    COST_SCENARIO_UPWARD_STRESS,
    net_bps,
    realized_funding_bps,
)
from rob974_h2_dtos import S3Trade, S4PairTrade

from research_contracts.canonical_hash import canonical_sha256

PATH_SCENARIO_BASE13 = "base13"
PATH_SCENARIO_PRIMARY_STRESS17 = "primary_stress17"
PATH_SCENARIO_UPWARD_STRESS22 = "upward_stress22"
PATH_SCENARIOS: tuple[str, ...] = (
    PATH_SCENARIO_BASE13,
    PATH_SCENARIO_PRIMARY_STRESS17,
    PATH_SCENARIO_UPWARD_STRESS22,
)

_VALID_PATH_SCENARIOS = frozenset(PATH_SCENARIOS)


def _e_columns(gross_bps: float, funding_bps: float) -> tuple[float, float, float]:
    return (
        net_bps(gross_bps, COST_SCENARIO_BASE, funding_bps),
        net_bps(gross_bps, COST_SCENARIO_PRIMARY_STRESS, funding_bps),
        net_bps(gross_bps, COST_SCENARIO_UPWARD_STRESS, funding_bps),
    )


def _require_path_scenario(path_scenario: str) -> str:
    if path_scenario not in _VALID_PATH_SCENARIOS:
        raise ValueError(
            f"path_scenario must be one of {PATH_SCENARIOS}, got {path_scenario!r}"
        )
    return path_scenario


@dataclass(frozen=True)
class S3ScenarioTradeRow:
    trade: S3Trade
    path_scenario: str
    funding_bps: float
    e13_bps: float
    e17_bps: float
    e22_bps: float
    thesis_exit_flag: bool
    timeout_flag: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "path_scenario", _require_path_scenario(self.path_scenario)
        )


@dataclass(frozen=True)
class S4ScenarioTradeRow:
    trade: S4PairTrade
    path_scenario: str
    funding_bps: float
    e13_bps: float
    e17_bps: float
    e22_bps: float
    thesis_exit_flag: bool
    timeout_flag: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "path_scenario", _require_path_scenario(self.path_scenario)
        )


def build_s3_scenario_ledger(
    trades: Sequence[S3Trade],
    path_scenario: str,
    funding_lookup=None,
) -> tuple[S3ScenarioTradeRow, ...]:
    _require_path_scenario(path_scenario)
    rows = []
    for trade in trades:
        crossings = (
            funding_lookup(trade.symbol, trade.side, trade.entry_ts, trade.exit_ts)
            if funding_lookup is not None
            else ()
        )
        funding_bps = realized_funding_bps(trade.side, crossings)
        e13, e17, e22 = _e_columns(trade.gross_bps, funding_bps)
        rows.append(
            S3ScenarioTradeRow(
                trade=trade,
                path_scenario=path_scenario,
                funding_bps=funding_bps,
                e13_bps=e13,
                e17_bps=e17,
                e22_bps=e22,
                thesis_exit_flag=trade.exit_reason == "THESIS_EXIT",
                timeout_flag=trade.exit_reason == "TIMEOUT",
            )
        )
    return tuple(rows)


def build_s4_scenario_ledger(
    trades: Sequence[S4PairTrade],
    path_scenario: str,
    funding_lookup=None,
) -> tuple[S4ScenarioTradeRow, ...]:
    _require_path_scenario(path_scenario)
    rows = []
    for trade in trades:
        symbol_a, symbol_b = trade.pair
        if funding_lookup is not None:
            crossings_a = funding_lookup(
                symbol_a, trade.side_a, trade.entry_ts, trade.exit_ts
            )
            crossings_b = funding_lookup(
                symbol_b, trade.side_b, trade.entry_ts, trade.exit_ts
            )
        else:
            crossings_a, crossings_b = (), ()
        funding_a = realized_funding_bps(trade.side_a, crossings_a)
        funding_b = realized_funding_bps(trade.side_b, crossings_b)
        funding_bps = trade.weight_a * funding_a + trade.weight_b * funding_b
        e13, e17, e22 = _e_columns(trade.gross_bps, funding_bps)
        rows.append(
            S4ScenarioTradeRow(
                trade=trade,
                path_scenario=path_scenario,
                funding_bps=funding_bps,
                e13_bps=e13,
                e17_bps=e17,
                e22_bps=e22,
                thesis_exit_flag=trade.exit_reason in ("MEAN_EXIT", "STALL_EXIT"),
                timeout_flag=trade.exit_reason == "TIMEOUT",
            )
        )
    return tuple(rows)


def _s3_row_payload(row: S3ScenarioTradeRow) -> dict:
    t = row.trade
    return {
        "symbol": t.symbol,
        "side": t.side,
        "config_id": t.config_id,
        "fold_id": t.fold_id,
        "signal_ts": t.signal_ts,
        "entry_ts": t.entry_ts,
        "entry_price": t.entry_price,
        "exit_ts": t.exit_ts,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "mfe_bps": t.mfe_bps,
        "mae_bps": t.mae_bps,
        "gross_bps": t.gross_bps,
        "volatility_percentile": t.volatility_percentile,
        "path_scenario": row.path_scenario,
        "funding_bps": row.funding_bps,
        "e13_bps": row.e13_bps,
        "e17_bps": row.e17_bps,
        "e22_bps": row.e22_bps,
        "thesis_exit_flag": row.thesis_exit_flag,
        "timeout_flag": row.timeout_flag,
    }


def _s4_row_payload(row: S4ScenarioTradeRow) -> dict:
    t = row.trade
    return {
        "pair": list(t.pair),
        "side_a": t.side_a,
        "side_b": t.side_b,
        "config_id": t.config_id,
        "fold_id": t.fold_id,
        "signal_ts": t.signal_ts,
        "entry_ts": t.entry_ts,
        "weight_a": t.weight_a,
        "weight_b": t.weight_b,
        "beta_a": t.beta_a,
        "beta_b": t.beta_b,
        "mu": t.mu,
        "sigma": t.sigma,
        "z_entry": t.z_entry,
        "gross_notional": t.gross_notional,
        "entry_price_a": t.entry_price_a,
        "entry_price_b": t.entry_price_b,
        "exit_ts": t.exit_ts,
        "exit_price_a": t.exit_price_a,
        "exit_price_b": t.exit_price_b,
        "exit_reason": t.exit_reason,
        "mfe_bps": t.mfe_bps,
        "mae_bps": t.mae_bps,
        "gross_bps": t.gross_bps,
        "order_id_a": t.order_id_a,
        "order_id_b": t.order_id_b,
        "pair_exec_status": t.pair_exec_status,
        "pair_executor_validated": t.pair_executor_validated,
        "demo_eligible": t.demo_eligible,
        "pair_exec_fail": t.pair_exec_fail,
        "promotion_status": t.promotion_status,
        "volatility_percentile": t.volatility_percentile,
        "volatility_percentile_provenance": t.volatility_percentile_provenance,
        "path_scenario": row.path_scenario,
        "funding_bps": row.funding_bps,
        "e13_bps": row.e13_bps,
        "e17_bps": row.e17_bps,
        "e22_bps": row.e22_bps,
        "thesis_exit_flag": row.thesis_exit_flag,
        "timeout_flag": row.timeout_flag,
    }


def s3_ledger_hash(rows: Sequence[S3ScenarioTradeRow]) -> str:
    """AC33: canonical chronological key ``(signal_ts, symbol)``, independent
    of caller/dict/set/filesystem order; typed AST/float-hex sealed."""
    ordered = sorted(rows, key=lambda r: (r.trade.signal_ts, r.trade.symbol))
    return canonical_sha256([_s3_row_payload(r) for r in ordered])


def s4_ledger_hash(rows: Sequence[S4ScenarioTradeRow]) -> str:
    ordered = sorted(rows, key=lambda r: (r.trade.signal_ts, r.trade.pair))
    return canonical_sha256([_s4_row_payload(r) for r in ordered])
