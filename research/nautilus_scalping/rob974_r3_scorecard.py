"""ROB-974 R3 H5 all-cell scorecard and canonical artifact pair.

This is an additive R3-only boundary.  It consumes the code-issued R3 plan
context and exact all-cell evidence, never the frozen R2 selected-winner H5
surface.  Artifact construction is pure and in-memory; physical publication
belongs to H6-B/M5.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal

from funding_oi_archive import FundingRow
from rob940_cost_model import (
    COST_SCENARIO_BASE,
    COST_SCENARIO_PRIMARY_STRESS,
    COST_SCENARIO_UPWARD_STRESS,
    FundingCrossing,
    net_bps,
    realized_funding_bps,
)
from rob941_funding_sidecar import FundingSidecar
from rob944_gap_funding import build_funding_lookup
from rob974_features import CommonSnapshot
from rob974_h2_dtos import S3Trade
from rob974_h2_scenarios import PATH_SCENARIOS
from rob974_h3_manifest import SYMBOLS
from rob974_h3_s3 import S3Candidate
from rob974_h3_s4 import S4Candidate
from rob974_h6a_accounting import AttemptAccountingRow
from rob974_r3_accounting import (
    Exact12AccountingReport,
    build_exact_12_accounting,
)
from rob974_r3_evidence_context import (
    R3ProductionEvidenceContext,
    require_r3_production_evidence_context,
)
from rob974_r3_gate_adapter import ProductionGateCampaignEvidence
from rob974_r3_manifest import (
    FROZEN_R3_ROSTER,
    R3_ADJACENCY_EDGES,
    R3_RELAXATION_RAYS,
)
from rob974_r3_relaxation import (
    R3_FOLD_IDS,
    CellFoldLedger,
    PhaseLedgerEvidence,
    RelaxationCampaignAnalysis,
    RelaxationTrade,
    TerminalIncompleteEvidence,
    analyze_relaxation_campaign,
)
from rob974_r3_relaxation_h2_adapter import (
    R3H2CellFoldInput,
    normalize_r3_phase_ledgers,
    normalize_r3_s3_trade,
    normalize_r3_s4_trade,
)
from rob974_r3_s4_dtos import R3S4PairTrade

R3_SCORECARD_SCHEMA_VERSION = "rob974.r3.h5.scorecard.v1"
_PBO_NOT_OBSERVED_REASON = "pbo_not_available_from_frozen_r3_inputs"
_ZERO_TRADES_REASON = "zero_oos_basket_trades"
_OPERATIONAL_INCOMPLETE_REASON = "operational_evidence_incomplete"
_ISSUED_LEDGER_SEAL = object()

_SECTION3_CLAIMS: tuple[tuple[str, str, str], ...] = (
    (
        "sample_hypothesis",
        "sample hypothesis",
        "any observed OOS fold has fewer than five basket trades",
    ),
    (
        "gross_edge_hypothesis",
        "gross-edge hypothesis",
        "a sample-qualified cell has pooled E0 below +25bp",
    ),
    (
        "after_cost_hypothesis",
        "after-cost hypothesis",
        "E17<+5bp, PF17<1.15, win margin<+3pp, or E22_up<=0",
    ),
    (
        "fold_generalization",
        "fold generalization",
        "positive OOS folds are at most four of eight",
    ),
    (
        "monthly_concentration",
        "monthly concentration",
        "monthly concentration exceeds 50%",
    ),
    (
        "threshold_region",
        "threshold region",
        "fewer than two adjacent cells jointly pass sample and E0",
    ),
    (
        "relaxation_selection_effect",
        "relaxation selection effect",
        "monotone edge decay is observed with no sample-edge intersection",
    ),
)


@dataclass(frozen=True, slots=True)
class R3PrunedBoundaryNeighbor:
    config_id: str
    family: Literal["S3", "S4"]
    external_parameters: tuple[tuple[str, float | int], ...]


R3_PRUNED_BOUNDARY_NEIGHBORS: tuple[R3PrunedBoundaryNeighbor, ...] = (
    R3PrunedBoundaryNeighbor("S3-R3-00", "S3", (("S_min", 0.10), ("M_min_bp", 0))),
    R3PrunedBoundaryNeighbor("S3-R3-00", "S3", (("S_min", 0.05), ("M_min_bp", 25))),
    R3PrunedBoundaryNeighbor("S3-R3-01", "S3", (("S_min", 0.05), ("M_min_bp", 25))),
    R3PrunedBoundaryNeighbor("S4-R3-00", "S4", (("z_entry", 1.20), ("d_min_bp", 140))),
    R3PrunedBoundaryNeighbor("S4-R3-01", "S4", (("z_entry", 1.00), ("d_min_bp", 180))),
    R3PrunedBoundaryNeighbor("S4-R3-03", "S4", (("z_entry", 1.00), ("d_min_bp", 180))),
)


class R3ScorecardError(ValueError):
    """R3 H5 evidence or canonical artifacts are malformed or mismatched."""


def _exact_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise R3ScorecardError(f"{name} must be an exact non-negative int")
    return value


def _finite_float(value: object, name: str) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise R3ScorecardError(f"{name} must be an exact finite float")
    return value


@dataclass(frozen=True, slots=True)
class R3TradeRiskAttribution:
    """Code-issued scenario member, sealed to H3/H2/PIT authorities."""

    path_scenario: str
    source_trade: RelaxationTrade
    source_engine_trade: S3Trade | R3S4PairTrade
    candidate: S3Candidate | S4Candidate
    decision_snapshot: CommonSnapshot
    funding_crossings_by_leg: tuple[tuple[FundingCrossing, ...], ...]
    funding_bps: float
    e13_bps: float
    e17_bps: float
    e22_bps: float
    sl_bps: float
    tp_bps: float
    market_return_24h: float
    realized_holding_minutes: float
    strength_s: float | None
    pullback_q: float | None
    volatility_percentile: float | None
    entry_z: float | None
    distance_bps: float | None
    correlation: float | None
    half_life_4h_bars: float | None
    half_life_hours: float | None
    beta_stability: float | None
    realized_pair_beta: float | None
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ISSUED_LEDGER_SEAL:
            raise R3ScorecardError("trade attribution was not code-issued")
        if self.path_scenario not in PATH_SCENARIOS:
            raise R3ScorecardError("trade attribution scenario drifted")
        if type(self.source_trade) is not RelaxationTrade:
            raise TypeError("source_trade must be exact RelaxationTrade")
        if type(self.decision_snapshot) is not CommonSnapshot:
            raise TypeError("decision_snapshot must be exact CommonSnapshot")
        for name in (
            "funding_bps",
            "e13_bps",
            "e17_bps",
            "e22_bps",
            "sl_bps",
            "tp_bps",
            "market_return_24h",
            "realized_holding_minutes",
        ):
            _finite_float(getattr(self, name), name)
        if self.sl_bps <= 0.0:
            raise R3ScorecardError("sl_bps must be positive")
        if self.tp_bps <= 0.0:
            raise R3ScorecardError("tp_bps must be positive")
        for name in (
            "strength_s",
            "pullback_q",
            "volatility_percentile",
            "entry_z",
            "distance_bps",
            "correlation",
            "half_life_4h_bars",
            "half_life_hours",
            "beta_stability",
            "realized_pair_beta",
        ):
            value = getattr(self, name)
            if value is not None:
                _finite_float(value, name)
        self._validate_exact_derivation()

    def _validate_exact_derivation(self) -> None:
        trade = self.source_engine_trade
        candidate = self.candidate
        if type(trade) is S3Trade and type(candidate) is S3Candidate:
            if (
                candidate.config_id != trade.config_id
                or candidate.decision_ts != trade.signal_ts
                or candidate.symbol != trade.symbol
                or candidate.side != trade.side
                or self.decision_snapshot.decision_ts != candidate.decision_ts
                or self.decision_snapshot.M != candidate.market_return_24h
            ):
                raise R3ScorecardError("S3 candidate/trade/snapshot binding drifted")
            expected_trade = normalize_r3_s3_trade(
                trade=trade,
                config=self._source_config(),
                fold_id=trade.fold_id,
            )
            expected_optional = (
                candidate.S,
                candidate.Q,
                candidate.volatility_percentile,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
            sides = (trade.side,)
        elif type(trade) is R3S4PairTrade and type(candidate) is S4Candidate:
            if (
                candidate.config_id != trade.config_id
                or candidate.decision_ts != trade.signal_ts
                or (candidate.symbol_a, candidate.symbol_b) != trade.pair
                or (candidate.side_a, candidate.side_b) != (trade.side_a, trade.side_b)
                or self.decision_snapshot.decision_ts != candidate.decision_ts
            ):
                raise R3ScorecardError("S4 candidate/trade/snapshot binding drifted")
            expected_trade = normalize_r3_s4_trade(
                trade=trade,
                config=self._source_config(),
                fold_id=trade.fold_id,
            )
            sign = {"long": 1.0, "short": -1.0}
            realized_beta = (
                sign[trade.side_a] * trade.weight_a * trade.beta_a
                + sign[trade.side_b] * trade.weight_b * trade.beta_b
            )
            expected_optional = (
                None,
                None,
                None,
                candidate.observed_z,
                candidate.D_bps,
                candidate.rho,
                candidate.half_life_4h_bars,
                candidate.half_life_4h_bars * 4.0,
                candidate.beta_stability,
                realized_beta,
            )
            sides = (trade.side_a, trade.side_b)
        else:
            raise TypeError("trade attribution family concrete types drifted")
        if self.source_trade != expected_trade:
            raise R3ScorecardError("normalized trade differs from H2 terminal trade")
        if self.decision_snapshot.M != self.market_return_24h:
            raise R3ScorecardError("market return differs from CommonSnapshot.M")
        if (
            self.sl_bps != candidate.d_SL * 10_000.0
            or self.tp_bps != candidate.d_TP * 10_000.0
        ):
            raise R3ScorecardError("trade SL/TP differs from its H3 candidate")
        if self.realized_holding_minutes != (trade.exit_ts - trade.entry_ts) / 60_000.0:
            raise R3ScorecardError("holding time differs from H2 terminal trade")
        if (trade.exit_ts - trade.entry_ts) % 60_000:
            raise R3ScorecardError("holding time must align to exact minutes")
        if type(self.funding_crossings_by_leg) is not tuple or len(
            self.funding_crossings_by_leg
        ) != len(sides):
            raise R3ScorecardError("funding crossing legs differ from trade legs")
        funding_by_leg: list[float] = []
        for side, crossings in zip(sides, self.funding_crossings_by_leg, strict=True):
            if type(crossings) is not tuple or any(
                type(item) is not FundingCrossing for item in crossings
            ):
                raise TypeError("funding crossings must be exact tuples")
            if any(
                type(item.ts) is not int
                or not trade.entry_ts <= item.ts < trade.exit_ts
                for item in crossings
            ) or tuple(item.ts for item in crossings) != tuple(
                sorted(item.ts for item in crossings)
            ):
                raise R3ScorecardError("funding crossings violate PIT hold window")
            funding_by_leg.append(realized_funding_bps(side, crossings))
        if len(funding_by_leg) == 1:
            expected_funding = funding_by_leg[0]
        else:
            expected_funding = (
                trade.weight_a * funding_by_leg[0] + trade.weight_b * funding_by_leg[1]
            )
        expected_e = (
            net_bps(trade.gross_bps, COST_SCENARIO_BASE, expected_funding),
            net_bps(
                trade.gross_bps,
                COST_SCENARIO_PRIMARY_STRESS,
                expected_funding,
            ),
            net_bps(
                trade.gross_bps,
                COST_SCENARIO_UPWARD_STRESS,
                expected_funding,
            ),
        )
        if (
            self.funding_bps != expected_funding
            or (self.e13_bps, self.e17_bps, self.e22_bps) != expected_e
            or (
                self.strength_s,
                self.pullback_q,
                self.volatility_percentile,
                self.entry_z,
                self.distance_bps,
                self.correlation,
                self.half_life_4h_bars,
                self.half_life_hours,
                self.beta_stability,
                self.realized_pair_beta,
            )
            != expected_optional
        ):
            raise R3ScorecardError("trade attribution economics were not derived")

    def _source_config(self):
        from rob974_r3_manifest import get_r3_config

        return get_r3_config(self.candidate.config_id)


@dataclass(frozen=True, slots=True)
class R3FoldScenarioAttribution:
    """One independently issued cost-scenario path for a config/fold."""

    path_scenario: str
    source: R3H2CellFoldInput
    decision_snapshots: tuple[CommonSnapshot, ...]
    funding_sidecars: tuple[FundingSidecar, ...]
    rows: tuple[R3TradeRiskAttribution, ...]
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ISSUED_LEDGER_SEAL:
            raise R3ScorecardError("fold scenario attribution was not code-issued")
        if self.path_scenario not in PATH_SCENARIOS:
            raise R3ScorecardError("fold scenario order drifted")
        if type(self.source) is not R3H2CellFoldInput:
            raise TypeError("source must be exact R3H2CellFoldInput")
        _validate_snapshot_authority(self.source, self.decision_snapshots)
        _validate_funding_sidecars(self.funding_sidecars)
        if type(self.rows) is not tuple or any(
            type(row) is not R3TradeRiskAttribution for row in self.rows
        ):
            raise TypeError("scenario rows must be exact attribution tuple")
        if any(row.path_scenario != self.path_scenario for row in self.rows):
            raise R3ScorecardError("scenario row path differs from its receipt")
        if tuple(row.source_engine_trade for row in self.rows) != (
            self.source.terminal.result.trades
        ):
            raise R3ScorecardError("scenario rows differ from sealed terminal trades")
        snapshots = {row.decision_ts: row for row in self.decision_snapshots}
        lookup = build_funding_lookup(
            {sidecar.symbol: sidecar for sidecar in self.funding_sidecars}
        )
        for row in self.rows:
            if row.decision_snapshot != snapshots[row.candidate.decision_ts]:
                raise R3ScorecardError("scenario row snapshot authority drifted")
            trade = row.source_engine_trade
            expected_crossings = tuple(
                tuple(lookup(symbol, side, trade.entry_ts, trade.exit_ts))
                for symbol, side in zip(
                    row.source_trade.event.instruments,
                    (
                        (trade.side,)
                        if type(trade) is S3Trade
                        else (trade.side_a, trade.side_b)
                    ),
                    strict=True,
                )
            )
            if row.funding_crossings_by_leg != expected_crossings:
                raise R3ScorecardError("scenario funding differs from PIT sidecars")


def _validate_snapshot_authority(
    source: R3H2CellFoldInput,
    snapshots: tuple[CommonSnapshot, ...],
) -> None:
    if type(snapshots) is not tuple or any(
        type(item) is not CommonSnapshot for item in snapshots
    ):
        raise TypeError("decision snapshots must be exact CommonSnapshot tuple")
    if tuple(item.decision_ts for item in snapshots) != tuple(
        candidate.decision_ts for candidate in source.h3_candidates
    ):
        raise R3ScorecardError(
            "decision snapshots must exactly follow H3 candidate order"
        )
    if len({item.decision_ts for item in snapshots}) != len(snapshots):
        raise R3ScorecardError("duplicate decision snapshot timestamp")
    for candidate, snapshot in zip(source.h3_candidates, snapshots, strict=True):
        if type(candidate) is S3Candidate and (
            candidate.market_return_24h != snapshot.M
        ):
            raise R3ScorecardError(
                "S3 candidate M differs from decision CommonSnapshot.M"
            )


def _validate_funding_sidecars(
    sidecars: tuple[FundingSidecar, ...],
) -> None:
    if type(sidecars) is not tuple or any(
        type(item) is not FundingSidecar for item in sidecars
    ):
        raise TypeError("funding sidecars must be exact FundingSidecar tuple")
    if tuple(item.symbol for item in sidecars) != SYMBOLS:
        raise R3ScorecardError("funding sidecars must use canonical symbol order")
    for sidecar in sidecars:
        if type(sidecar.rows) is not tuple or any(
            type(item) is not FundingRow for item in sidecar.rows
        ):
            raise TypeError("funding sidecar rows must be exact FundingRow tuple")
        for row in sidecar.rows:
            if (
                type(row.calc_time) is not int
                or type(row.funding_interval_hours) is not int
                or row.funding_interval_hours <= 0
                or type(row.last_funding_rate) is not float
                or not math.isfinite(row.last_funding_rate)
            ):
                raise R3ScorecardError("funding sidecar contains malformed row")
        if any(
            right.calc_time <= left.calc_time
            for left, right in zip(sidecar.rows, sidecar.rows[1:], strict=False)
        ):
            raise R3ScorecardError(
                "funding sidecar rows must be strictly chronological"
            )


def issue_r3_fold_scenario_attribution(
    *,
    path_scenario: str,
    source: R3H2CellFoldInput,
    decision_snapshots: tuple[CommonSnapshot, ...],
    funding_sidecars: tuple[FundingSidecar, ...],
) -> R3FoldScenarioAttribution:
    """Bind one actual scenario path without accepting derived economics."""

    if path_scenario not in PATH_SCENARIOS:
        raise R3ScorecardError("path_scenario is outside exact-three order")
    if type(source) is not R3H2CellFoldInput:
        raise TypeError("source must be exact R3H2CellFoldInput")
    _validate_snapshot_authority(source, decision_snapshots)
    _validate_funding_sidecars(funding_sidecars)
    snapshot_by_ts = {row.decision_ts: row for row in decision_snapshots}
    candidate_by_identity = {
        (
            candidate.decision_ts,
            (
                (candidate.symbol,)
                if type(candidate) is S3Candidate
                else (candidate.symbol_a, candidate.symbol_b)
            ),
        ): candidate
        for candidate in source.h3_candidates
    }
    funding_lookup = build_funding_lookup(
        {sidecar.symbol: sidecar for sidecar in funding_sidecars}
    )
    rows: list[R3TradeRiskAttribution] = []
    for trade in source.terminal.result.trades:
        if type(trade) is S3Trade:
            instruments = (trade.symbol,)
            sides = (trade.side,)
            normalized = normalize_r3_s3_trade(
                trade=trade, config=source.config, fold_id=source.fold_id
            )
        elif type(trade) is R3S4PairTrade:
            instruments = trade.pair
            sides = (trade.side_a, trade.side_b)
            normalized = normalize_r3_s4_trade(
                trade=trade, config=source.config, fold_id=source.fold_id
            )
        else:  # pragma: no cover - source DTO validates the closed families
            raise TypeError("sealed terminal trade concrete type drifted")
        candidate = candidate_by_identity.get((trade.signal_ts, instruments))
        if candidate is None:
            raise R3ScorecardError("terminal trade lacks exact H3 candidate")
        crossings = tuple(
            tuple(funding_lookup(symbol, side, trade.entry_ts, trade.exit_ts))
            for symbol, side in zip(instruments, sides, strict=True)
        )
        funding_by_leg = tuple(
            realized_funding_bps(side, rows_)
            for side, rows_ in zip(sides, crossings, strict=True)
        )
        funding = (
            funding_by_leg[0]
            if len(funding_by_leg) == 1
            else trade.weight_a * funding_by_leg[0] + trade.weight_b * funding_by_leg[1]
        )
        if type(candidate) is S3Candidate:
            optionals = (
                candidate.S,
                candidate.Q,
                candidate.volatility_percentile,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        else:
            sign = {"long": 1.0, "short": -1.0}
            optionals = (
                None,
                None,
                None,
                candidate.observed_z,
                candidate.D_bps,
                candidate.rho,
                candidate.half_life_4h_bars,
                candidate.half_life_4h_bars * 4.0,
                candidate.beta_stability,
                sign[trade.side_a] * trade.weight_a * trade.beta_a
                + sign[trade.side_b] * trade.weight_b * trade.beta_b,
            )
        rows.append(
            R3TradeRiskAttribution(
                path_scenario=path_scenario,
                source_trade=normalized,
                source_engine_trade=trade,
                candidate=candidate,
                decision_snapshot=snapshot_by_ts[candidate.decision_ts],
                funding_crossings_by_leg=crossings,
                funding_bps=funding,
                e13_bps=net_bps(trade.gross_bps, COST_SCENARIO_BASE, funding),
                e17_bps=net_bps(trade.gross_bps, COST_SCENARIO_PRIMARY_STRESS, funding),
                e22_bps=net_bps(trade.gross_bps, COST_SCENARIO_UPWARD_STRESS, funding),
                sl_bps=candidate.d_SL * 10_000.0,
                tp_bps=candidate.d_TP * 10_000.0,
                market_return_24h=snapshot_by_ts[candidate.decision_ts].M,
                realized_holding_minutes=(trade.exit_ts - trade.entry_ts) / 60_000.0,
                strength_s=optionals[0],
                pullback_q=optionals[1],
                volatility_percentile=optionals[2],
                entry_z=optionals[3],
                distance_bps=optionals[4],
                correlation=optionals[5],
                half_life_4h_bars=optionals[6],
                half_life_hours=optionals[7],
                beta_stability=optionals[8],
                realized_pair_beta=optionals[9],
                _seal=_ISSUED_LEDGER_SEAL,
            )
        )
    return R3FoldScenarioAttribution(
        path_scenario=path_scenario,
        source=source,
        decision_snapshots=decision_snapshots,
        funding_sidecars=funding_sidecars,
        rows=tuple(rows),
        _seal=_ISSUED_LEDGER_SEAL,
    )


@dataclass(frozen=True, slots=True)
class R3FoldOOSInput:
    """One H2-issued fold envelope plus trade-aligned H5 attribution fields.

    Accepted and basket-trade counts are deliberately absent.  They are
    derived only after the complete canonical 12x8 source tuple is normalized.
    """

    scenario_attributions: tuple[R3FoldScenarioAttribution, ...]

    def __post_init__(self) -> None:
        if type(self.scenario_attributions) is not tuple or any(
            type(item) is not R3FoldScenarioAttribution
            for item in self.scenario_attributions
        ):
            raise TypeError("scenario_attributions must be exact receipt tuple")
        if tuple(item.path_scenario for item in self.scenario_attributions) != (
            PATH_SCENARIOS
        ):
            raise R3ScorecardError("fold requires exact-three scenario order")
        primary = self.primary
        for receipt in self.scenario_attributions:
            if (
                receipt.source != primary.source
                or receipt.decision_snapshots != primary.decision_snapshots
                or receipt.funding_sidecars != primary.funding_sidecars
            ):
                raise R3ScorecardError(
                    "scenario paths differ from frozen membership/PIT authority"
                )

    @property
    def primary(self) -> R3FoldScenarioAttribution:
        return self.scenario_attributions[1]


@dataclass(frozen=True, slots=True)
class R3CellOOSInput:
    config_id: str
    folds: tuple[R3FoldOOSInput, ...]

    def __post_init__(self) -> None:
        if type(self.config_id) is not str:
            raise TypeError("config_id must be exact str")
        if type(self.folds) is not tuple or any(
            type(item) is not R3FoldOOSInput for item in self.folds
        ):
            raise TypeError("folds must be exact R3FoldOOSInput tuple")
        headers = tuple(
            (item.primary.source.config.config_id, item.primary.source.fold_id)
            for item in self.folds
        )
        expected = tuple((self.config_id, fold_id) for fold_id in R3_FOLD_IDS)
        if headers != expected:
            raise R3ScorecardError(
                "cell folds must use exact canonical eight-fold order"
            )


@dataclass(frozen=True, slots=True)
class R3FoldOOSLedger:
    """Code-issued fold receipt derived from one exact sealed H2 envelope."""

    scenario_attributions: tuple[R3FoldScenarioAttribution, ...]
    source_ledger: CellFoldLedger
    terminal_incomplete: TerminalIncompleteEvidence | None
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ISSUED_LEDGER_SEAL:
            raise R3ScorecardError("fold OOS ledger was not code-issued")
        if (
            type(self.scenario_attributions) is not tuple
            or tuple(item.path_scenario for item in self.scenario_attributions)
            != PATH_SCENARIOS
        ):
            raise R3ScorecardError("issued fold scenario order drifted")
        if type(self.source_ledger) is not CellFoldLedger:
            raise TypeError("source_ledger must be exact CellFoldLedger")
        if (self.source.config.config_id, self.source.fold_id) != (
            self.source_ledger.config_id,
            self.source_ledger.fold_id,
        ):
            raise R3ScorecardError("normalized ledger differs from its H2 source")
        if self.accepted_count < self.source_ledger.basket_trade_count:
            raise R3ScorecardError("derived accepted count is below basket trades")
        if tuple(item.source_trade for item in self.trade_attributions) != (
            self.source_ledger.trades
        ):
            raise R3ScorecardError(
                "trade attributions must exactly bind source trade identity, "
                "economics, and order"
            )
        terminal = self.terminal_incomplete
        if terminal is not None:
            if type(terminal) is not TerminalIncompleteEvidence:
                raise TypeError("terminal_incomplete must be exact evidence or None")
            if terminal.phase != "OOS" or (
                terminal.config_id,
                terminal.fold_id,
            ) != (self.source_ledger.config_id, self.source_ledger.fold_id):
                raise R3ScorecardError(
                    "terminal incomplete differs from its OOS cell/fold"
                )

    @property
    def accepted_count(self) -> int:
        """Exact accepted count, derived from the sealed H3 candidate tuple."""

        return len(self.source.h3_candidates)

    @property
    def source(self) -> R3H2CellFoldInput:
        return self.scenario_attributions[1].source

    @property
    def trade_attributions(self) -> tuple[R3TradeRiskAttribution, ...]:
        return self.scenario_attributions[1].rows


@dataclass(frozen=True, slots=True)
class R3CellOOSLedger:
    config_id: str
    folds: tuple[R3FoldOOSLedger, ...]

    def __post_init__(self) -> None:
        if type(self.config_id) is not str:
            raise TypeError("config_id must be exact str")
        if type(self.folds) is not tuple or any(
            type(item) is not R3FoldOOSLedger for item in self.folds
        ):
            raise TypeError("folds must be exact R3FoldOOSLedger tuple")
        headers = tuple(
            (item.source_ledger.config_id, item.source_ledger.fold_id)
            for item in self.folds
        )
        expected = tuple((self.config_id, fold_id) for fold_id in R3_FOLD_IDS)
        if headers != expected:
            raise R3ScorecardError(
                "cell folds must use exact canonical eight-fold order"
            )


@dataclass(frozen=True, slots=True)
class R3AllCellOOSLedger:
    schema_version: Literal["rob974.r3.h5.all_cell_oos_ledger.v1"]
    campaign_identity_sha256: str
    campaign_run_id: str
    exact_12_mapping_hash: str
    ordered_mapping: tuple[tuple[str, str], ...]
    cells: tuple[R3CellOOSLedger, ...]
    operational_status: Literal["COMPLETE", "INCOMPLETE"]
    incomplete_reasons: tuple[str, ...]
    _evidence_context: R3ProductionEvidenceContext = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_issued_oos_ledger(self)


def _validate_issued_oos_ledger(ledger: R3AllCellOOSLedger) -> None:
    if ledger._seal is not _ISSUED_LEDGER_SEAL:
        raise R3ScorecardError("all-cell OOS ledger was not code-issued")
    context = require_r3_production_evidence_context(ledger._evidence_context)
    if ledger.schema_version != "rob974.r3.h5.all_cell_oos_ledger.v1":
        raise R3ScorecardError("all-cell OOS ledger schema drifted")
    if type(ledger.cells) is not tuple or any(
        type(cell) is not R3CellOOSLedger for cell in ledger.cells
    ):
        raise TypeError("all-cell OOS ledger cells must be exact tuple")
    expected_ids = tuple(row.config_id for row in FROZEN_R3_ROSTER)
    if tuple(cell.config_id for cell in ledger.cells) != expected_ids:
        raise R3ScorecardError("all-cell OOS ledger must use canonical exact-12 order")
    if (
        ledger.campaign_identity_sha256 != context.campaign_identity_sha256
        or ledger.campaign_run_id != context.campaign_run_id
        or ledger.exact_12_mapping_hash != context.exact_12_mapping_hash
        or ledger.ordered_mapping != context.ordered_mapping
    ):
        raise R3ScorecardError("all-cell OOS ledger identity differs from plan")
    flattened = tuple(fold.source for cell in ledger.cells for fold in cell.folds)
    normalized = normalize_r3_phase_ledgers(phase="OOS", sources=flattened)
    expected_ledgers = iter(normalized.ledgers)
    terminal_by_header = {
        (item.config_id, item.fold_id): item for item in normalized.terminal_incompletes
    }
    for cell in ledger.cells:
        for fold in cell.folds:
            expected_ledger = next(expected_ledgers)
            expected_terminal = terminal_by_header.get(
                (expected_ledger.config_id, expected_ledger.fold_id)
            )
            if (
                fold.source_ledger != expected_ledger
                or fold.terminal_incomplete != expected_terminal
            ):
                raise R3ScorecardError(
                    "issued OOS ledger diverges from sealed H2 source authority"
                )
    reasons = tuple(
        f"OOS:{terminal.signal_identity.family}:{terminal.config_id}:"
        f"{terminal.fold_id}:{terminal.signal_identity.signal_ts}:"
        f"{terminal.entry_ts}:{terminal.reason}"
        for cell in ledger.cells
        for fold in cell.folds
        if (terminal := fold.terminal_incomplete) is not None
    )
    expected_status = "INCOMPLETE" if reasons else "COMPLETE"
    if (
        ledger.incomplete_reasons != reasons
        or ledger.operational_status != expected_status
    ):
        raise R3ScorecardError("all-cell OOS ledger operational status drifted")


def issue_r3_all_cell_oos_ledger(
    *,
    evidence_context: object,
    cells: tuple[R3CellOOSInput, ...],
) -> R3AllCellOOSLedger:
    """Issue a plan-bound exact 12x8 ledger; no selection surface exists."""

    context = require_r3_production_evidence_context(evidence_context)
    if type(cells) is not tuple or any(
        type(cell) is not R3CellOOSInput for cell in cells
    ):
        raise TypeError("cells must be an exact R3CellOOSInput tuple")
    expected_ids = tuple(row.config_id for row in FROZEN_R3_ROSTER)
    if tuple(cell.config_id for cell in cells) != expected_ids:
        raise R3ScorecardError("all-cell OOS ledger must use canonical exact-12 order")
    source_inputs = tuple(fold.primary.source for cell in cells for fold in cell.folds)
    normalized = normalize_r3_phase_ledgers(phase="OOS", sources=source_inputs)
    terminal_by_header = {
        (item.config_id, item.fold_id): item for item in normalized.terminal_incompletes
    }
    input_folds = iter(fold for cell in cells for fold in cell.folds)
    issued_folds: list[R3FoldOOSLedger] = []
    for source_ledger in normalized.ledgers:
        fold_input = next(input_folds)
        issued_folds.append(
            R3FoldOOSLedger(
                scenario_attributions=fold_input.scenario_attributions,
                source_ledger=source_ledger,
                terminal_incomplete=terminal_by_header.get(
                    (source_ledger.config_id, source_ledger.fold_id)
                ),
                _seal=_ISSUED_LEDGER_SEAL,
            )
        )
    issued_cells = tuple(
        R3CellOOSLedger(
            config_id=cell.config_id,
            folds=tuple(issued_folds[index * 8 : (index + 1) * 8]),
        )
        for index, cell in enumerate(cells)
    )
    reasons = tuple(
        f"OOS:{terminal.signal_identity.family}:{terminal.config_id}:"
        f"{terminal.fold_id}:{terminal.signal_identity.signal_ts}:"
        f"{terminal.entry_ts}:{terminal.reason}"
        for terminal in normalized.terminal_incompletes
    )
    return R3AllCellOOSLedger(
        schema_version="rob974.r3.h5.all_cell_oos_ledger.v1",
        campaign_identity_sha256=context.campaign_identity_sha256,
        campaign_run_id=context.campaign_run_id,
        exact_12_mapping_hash=context.exact_12_mapping_hash,
        ordered_mapping=context.ordered_mapping,
        cells=issued_cells,
        operational_status="INCOMPLETE" if reasons else "COMPLETE",
        incomplete_reasons=reasons,
        _evidence_context=context,
        _seal=_ISSUED_LEDGER_SEAL,
    )


@dataclass(frozen=True, slots=True)
class R3ScorecardAccountingEvidence:
    """Code-issued exact-12 attempt receipt, recomputable at consumption."""

    report: Exact12AccountingReport
    attempts: tuple[AttemptAccountingRow, ...]
    registered_total: int
    mismatch_row_ids: tuple[str, ...]
    extra_experiment_ids: tuple[str, ...]
    _evidence_context: R3ProductionEvidenceContext = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ISSUED_LEDGER_SEAL:
            raise R3ScorecardError("scorecard accounting was not code-issued")
        context = require_r3_production_evidence_context(self._evidence_context)
        if type(self.report) is not Exact12AccountingReport:
            raise TypeError("report must be exact Exact12AccountingReport")
        if type(self.attempts) is not tuple or any(
            type(item) is not AttemptAccountingRow for item in self.attempts
        ):
            raise TypeError("attempts must be exact AttemptAccountingRow tuple")
        recomputed = build_exact_12_accounting(
            campaign_run_id=context.campaign_run_id,
            ordered_mapping=context.ordered_mapping,
            registered_total=self.registered_total,
            attempts=self.attempts,
            mismatch_row_ids=self.mismatch_row_ids,
            extra_experiment_ids=self.extra_experiment_ids,
        )
        if self.report != recomputed:
            raise R3ScorecardError(
                "scorecard accounting differs from exact attempts and plan mapping"
            )


def issue_r3_scorecard_accounting(
    *,
    evidence_context: object,
    registered_total: int,
    attempts: tuple[AttemptAccountingRow, ...],
    mismatch_row_ids: tuple[str, ...] = (),
    extra_experiment_ids: tuple[str, ...] = (),
) -> R3ScorecardAccountingEvidence:
    context = require_r3_production_evidence_context(evidence_context)
    report = build_exact_12_accounting(
        campaign_run_id=context.campaign_run_id,
        ordered_mapping=context.ordered_mapping,
        registered_total=registered_total,
        attempts=attempts,
        mismatch_row_ids=mismatch_row_ids,
        extra_experiment_ids=extra_experiment_ids,
    )
    return R3ScorecardAccountingEvidence(
        report=report,
        attempts=attempts,
        registered_total=registered_total,
        mismatch_row_ids=mismatch_row_ids,
        extra_experiment_ids=extra_experiment_ids,
        _evidence_context=context,
        _seal=_ISSUED_LEDGER_SEAL,
    )


@dataclass(frozen=True, slots=True)
class R3ScorecardRelaxationEvidence:
    """Raw §7 evidence plus its exactly recomputable campaign analysis."""

    oos_evidence: PhaseLedgerEvidence
    train_evidence: PhaseLedgerEvidence | None
    analysis: RelaxationCampaignAnalysis
    _evidence_context: R3ProductionEvidenceContext = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ISSUED_LEDGER_SEAL:
            raise R3ScorecardError("scorecard relaxation evidence was not code-issued")
        context = require_r3_production_evidence_context(self._evidence_context)
        if type(self.oos_evidence) is not PhaseLedgerEvidence:
            raise TypeError("oos_evidence must be exact PhaseLedgerEvidence")
        if (
            self.train_evidence is not None
            and type(self.train_evidence) is not PhaseLedgerEvidence
        ):
            raise TypeError("train_evidence must be exact PhaseLedgerEvidence or None")
        recomputed = analyze_relaxation_campaign(
            evidence_context=context,
            oos_evidence=self.oos_evidence,
            train_evidence=self.train_evidence,
        )
        if self.analysis != recomputed:
            raise R3ScorecardError(
                "relaxation analysis differs from raw exact-ray evidence"
            )


def issue_r3_scorecard_relaxation_evidence(
    *,
    evidence_context: object,
    oos_evidence: PhaseLedgerEvidence,
    train_evidence: PhaseLedgerEvidence | None = None,
) -> R3ScorecardRelaxationEvidence:
    context = require_r3_production_evidence_context(evidence_context)
    analysis = analyze_relaxation_campaign(
        evidence_context=context,
        oos_evidence=oos_evidence,
        train_evidence=train_evidence,
    )
    return R3ScorecardRelaxationEvidence(
        oos_evidence=oos_evidence,
        train_evidence=train_evidence,
        analysis=analysis,
        _evidence_context=context,
        _seal=_ISSUED_LEDGER_SEAL,
    )


@dataclass(frozen=True, slots=True)
class R3ArtifactPair:
    json_bytes: bytes
    markdown_bytes: bytes
    semantic_sha256: str
    markdown_sha256: str


def _accounting_payload(
    accounting: R3ScorecardAccountingEvidence | Exact12AccountingReport | None,
) -> dict[str, Any]:
    if accounting is None:
        return {
            "available": False,
            "accounting_complete": False,
            "performance_usable": False,
            "reason": "exact_12_accounting_missing_or_wrong_type",
        }
    report = (
        accounting.report
        if type(accounting) is R3ScorecardAccountingEvidence
        else accounting
    )
    assert type(report) is Exact12AccountingReport
    return {
        "available": True,
        "attempt_authority": (
            "code_issued_exact_attempts"
            if type(accounting) is R3ScorecardAccountingEvidence
            else "unsealed_report_only"
        ),
        "campaign_run_id": report.campaign_run_id,
        "exact_12_mapping_hash": report.exact_12_mapping_hash,
        "expected_total": report.expected_total,
        "registered_total": report.registered_total,
        "primary_attempts": report.primary_attempts,
        "total_attempts": report.total_attempts,
        "retry_attempts": report.retry_attempts,
        "status_counts": dict(report.status_counts),
        "missing_row_ids": list(report.missing_row_ids),
        "extra_experiment_ids": list(report.extra_experiment_ids),
        "mismatch_row_ids": list(report.mismatch_row_ids),
        "duplicate_or_gap_row_ids": list(report.duplicate_or_gap_row_ids),
        "accounting_complete": report.accounting_complete,
        "all_primary_completed": report.all_primary_completed,
        "performance_usable": report.performance_usable,
        "trial_accounting_hash": report.trial_accounting_hash,
        "attempts": (
            []
            if type(accounting) is not R3ScorecardAccountingEvidence
            else [_plain(item) for item in accounting.attempts]
        ),
    }


def _null_metric(reason: str) -> dict[str, Any]:
    return {"value": None, "reason": reason}


def _metric(value: float | int | None, reason: str | None = None) -> dict[str, Any]:
    if value is None:
        if reason is None:
            raise R3ScorecardError("null metric requires a closed reason")
        return _null_metric(reason)
    if reason is not None:
        raise R3ScorecardError("observed metric cannot carry a null reason")
    if type(value) is float and not math.isfinite(value):
        raise R3ScorecardError("metric value must be finite")
    return {"value": value, "reason": None}


def _mean(values: list[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _pf_metric(values: list[float]) -> tuple[dict[str, Any], bool]:
    if not values:
        return _null_metric(_ZERO_TRADES_REASON), False
    profit = math.fsum(value for value in values if value > 0.0)
    loss = -math.fsum(value for value in values if value < 0.0)
    if loss == 0.0:
        if profit > 0.0:
            return (
                {
                    "value": None,
                    "reason": "no_negative_e17_returns_infinite_pass",
                },
                True,
            )
        return _null_metric("no_positive_or_negative_e17_returns"), False
    value = profit / loss
    return _metric(value), value >= 1.15


def _month_key(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=UTC)
    return f"{value.year:04d}-{value.month:02d}"


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if not xs or len(xs) != len(ys):
        return None
    mean_x = math.fsum(xs) / len(xs)
    mean_y = math.fsum(ys) / len(ys)
    variance_x = math.fsum((value - mean_x) ** 2 for value in xs) / len(xs)
    variance_y = math.fsum((value - mean_y) ** 2 for value in ys) / len(ys)
    if variance_x == 0.0 or variance_y == 0.0:
        return None
    covariance = math.fsum(
        (x_value - mean_x) * (y_value - mean_y)
        for x_value, y_value in zip(xs, ys, strict=True)
    ) / len(xs)
    return covariance / math.sqrt(variance_x * variance_y)


def _attribution_bucket(
    rows: list[R3TradeRiskAttribution], *, reason: str = "no_rows_in_bucket"
) -> dict[str, Any]:
    if not rows:
        return {
            "trades": 0,
            "e0_bps": _null_metric(reason),
            "e13_bps": _null_metric(reason),
            "e17_bps": _null_metric(reason),
            "e22_bps": _null_metric(reason),
            "pf17": _null_metric(reason),
            "average_holding_minutes": _null_metric(reason),
        }
    e17 = [row.e17_bps for row in rows]
    pf, _passed = _pf_metric(e17)
    return {
        "trades": len(rows),
        "e0_bps": _metric(
            _mean([row.source_trade.execution.gross_bps for row in rows])
        ),
        "e13_bps": _metric(_mean([row.e13_bps for row in rows])),
        "e17_bps": _metric(_mean(e17)),
        "e22_bps": _metric(_mean([row.e22_bps for row in rows])),
        "pf17": pf,
        "average_holding_minutes": _metric(
            _mean([row.realized_holding_minutes for row in rows])
        ),
    }


def _gate(
    *,
    gate_id: str,
    observed: object,
    passed: bool | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "status": (
            "PASS" if passed is True else "FAIL" if passed is False else "NOT_OBSERVED"
        ),
        "observed": observed,
        "reason": reason,
    }


def _common_full_gate_passed(
    *,
    minimum_fold_trades: int,
    e0_bps: float,
    e17_bps: float,
    pf17_passed: bool,
    positive_folds: int,
    monthly_concentration: float | None,
    e22_up_bps: float,
    win_margin: float,
    strategy_passed: bool,
) -> bool:
    """Apply the frozen inclusive/strict H5 thresholds in one testable seam."""

    return (
        minimum_fold_trades >= 5
        and e0_bps >= 25.0
        and e17_bps >= 5.0
        and pf17_passed
        and positive_folds > 4
        and monthly_concentration is not None
        and monthly_concentration <= 0.50
        and e22_up_bps > 0.0
        and win_margin >= 0.03
        and strategy_passed
    )


def _strategy_gates(
    *, config_id: str, folds: tuple[R3FoldOOSLedger, ...]
) -> dict[str, Any]:
    primary = [row for fold in folds for row in fold.scenario_attributions[1].rows]
    upward = [row for fold in folds for row in fold.scenario_attributions[2].rows]
    fold_counts = {
        fold.source_ledger.fold_id: len(fold.scenario_attributions[1].rows)
        for fold in folds
    }
    fold_timeout = {
        fold.source_ledger.fold_id: sum(
            row.source_trade.execution.exit_reason == "TIMEOUT"
            for row in fold.scenario_attributions[1].rows
        )
        for fold in folds
    }
    timeout_ratio = (
        sum(row.source_trade.execution.exit_reason == "TIMEOUT" for row in primary)
        / len(primary)
        if primary
        else None
    )
    fold_timeout_ratios = {
        fold_id: (fold_timeout[fold_id] / count if count else None)
        for fold_id, count in fold_counts.items()
    }
    family = config_id[:2]
    gates: list[dict[str, Any]] = []
    if family == "S3":
        gates.extend(
            (
                _gate(
                    gate_id="s3_pooled_timeout_lte_15pct",
                    observed=timeout_ratio,
                    passed=None if timeout_ratio is None else timeout_ratio <= 0.15,
                    reason=(
                        "s3_pooled_timeout_undefined"
                        if timeout_ratio is None
                        else "pooled_timeout_must_be_lte_0_15"
                    ),
                ),
                _gate(
                    gate_id="s3_each_fold_timeout_lte_25pct",
                    observed=fold_timeout_ratios,
                    passed=(
                        None
                        if any(value is None for value in fold_timeout_ratios.values())
                        else all(
                            value is not None and value <= 0.25
                            for value in fold_timeout_ratios.values()
                        )
                    ),
                    reason="every_observed_fold_timeout_must_be_lte_0_25",
                ),
            )
        )
        bullish = [
            row.e22_bps for row in upward if row.source_trade.event.direction == "long"
        ]
        bullish_e22 = _mean(bullish)
        gates.append(
            _gate(
                gate_id="s3_bullish_long_e22_gt_zero",
                observed=bullish_e22,
                passed=None if bullish_e22 is None else bullish_e22 > 0.0,
                reason="bullish_long_upward_membership_e22_must_be_positive",
            )
        )
        loss_denominator = math.fsum(
            abs(row.e17_bps) for row in primary if row.e17_bps < 0.0
        )
        first4 = (
            None
            if loss_denominator == 0.0
            else math.fsum(
                abs(row.e17_bps)
                for row in primary
                if row.e17_bps < 0.0
                and row.source_trade.execution.exit_reason == "SL"
                and row.realized_holding_minutes <= 240.0
            )
            / loss_denominator
        )
        gates.append(
            _gate(
                gate_id="s3_first_4h_sl_loss_dependence_lte_50pct",
                observed=first4,
                passed=None if first4 is None else first4 <= 0.50,
                reason=(
                    "s3_first_4h_sl_denominator_undefined"
                    if first4 is None
                    else "first_4h_sl_loss_share_must_be_lte_0_50"
                ),
            )
        )
        dimensions = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
        by_dimension = {
            dimension: [
                row
                for row in primary
                if row.source_trade.event.instruments == (dimension,)
            ]
            for dimension in dimensions
        }
        dimension_e17 = {
            key: _mean([row.e17_bps for row in rows])
            for key, rows in by_dimension.items()
        }
        if any(not rows for rows in by_dimension.values()):
            dependence_pass: bool | None = None
            dependence_reason = "s3_symbol_evidence_missing"
        else:
            positive = [key for key, value in dimension_e17.items() if value > 0.0]
            dependence_pass = True
            if len(positive) == 1:
                lone = positive[0]
                other_rows = [
                    row
                    for key, rows in by_dimension.items()
                    if key != lone
                    for row in rows
                ]
                dependence_pass = _mean([row.e17_bps for row in other_rows]) > 0.0
            dependence_reason = (
                "no_exactly_one_positive_symbol_with_nonpositive_other_pool"
            )
        gates.append(
            _gate(
                gate_id="s3_symbol_dependence",
                observed=dimension_e17,
                passed=dependence_pass,
                reason=dependence_reason,
            )
        )
        executor_state = None
    else:
        gates.extend(
            (
                _gate(
                    gate_id="s4_pooled_timeout_lte_20pct",
                    observed=timeout_ratio,
                    passed=None if timeout_ratio is None else timeout_ratio <= 0.20,
                    reason=(
                        "s4_pooled_timeout_undefined"
                        if timeout_ratio is None
                        else "pooled_timeout_must_be_lte_0_20"
                    ),
                ),
                _gate(
                    gate_id="s4_each_fold_timeout_lte_30pct",
                    observed=fold_timeout_ratios,
                    passed=(
                        None
                        if any(value is None for value in fold_timeout_ratios.values())
                        else all(
                            value is not None and value <= 0.30
                            for value in fold_timeout_ratios.values()
                        )
                    ),
                    reason="every_observed_fold_timeout_must_be_lte_0_30",
                ),
            )
        )
        high_m = [row.e22_bps for row in upward if row.market_return_24h > 0.03]
        high_m_e22 = _mean(high_m)
        gates.append(
            _gate(
                gate_id="s4_high_m_e22_gt_zero",
                observed=high_m_e22,
                passed=None if high_m_e22 is None else high_m_e22 > 0.0,
                reason="M_gt_0_03_upward_membership_e22_must_be_positive",
            )
        )
        correlation = _pearson(
            [row.market_return_24h for row in primary],
            [row.source_trade.execution.gross_bps for row in primary],
        )
        gates.append(
            _gate(
                gate_id="s4_abs_corr_gross_m_lte_0_15",
                observed=correlation,
                passed=None if correlation is None else abs(correlation) <= 0.15 + 1e-9,
                reason=(
                    "s4_correlation_undefined"
                    if correlation is None
                    else "absolute_pearson_correlation_must_be_lte_0_15"
                ),
            )
        )
        pairs = (
            ("XRPUSDT", "DOGEUSDT"),
            ("XRPUSDT", "SOLUSDT"),
            ("DOGEUSDT", "SOLUSDT"),
        )
        pair_rows = {
            "-".join(symbol.removesuffix("USDT") for symbol in pair): [
                row for row in primary if row.source_trade.event.instruments == pair
            ]
            for pair in pairs
        }
        pair_net = {
            key: math.fsum(row.e17_bps for row in rows)
            for key, rows in pair_rows.items()
        }
        positive_pair_net = {
            key: value for key, value in pair_net.items() if value > 0.0
        }
        pair_concentration = (
            max(positive_pair_net.values()) / math.fsum(positive_pair_net.values())
            if positive_pair_net
            else None
        )
        if any(not rows for rows in pair_rows.values()):
            pair_pass: bool | None = None
            pair_reason = "s4_pair_evidence_missing"
        elif pair_concentration is None:
            pair_pass = True
            pair_reason = "no_positive_pair_net_for_concentration_predicate"
        else:
            dominant = max(positive_pair_net, key=positive_pair_net.__getitem__)
            other_rows = [
                row
                for key, rows in pair_rows.items()
                if key != dominant
                for row in rows
            ]
            others_e17 = _mean([row.e17_bps for row in other_rows])
            pair_pass = not (
                pair_concentration > 0.70
                and others_e17 is not None
                and others_e17 <= 0.0
            )
            pair_reason = "dominant_share_gt_0_70_requires_other_pool_positive"
        gates.append(
            _gate(
                gate_id="s4_pair_concentration_dependence",
                observed={
                    "pair_net_e17": pair_net,
                    "positive_pair_concentration": pair_concentration,
                },
                passed=pair_pass,
                reason=pair_reason,
            )
        )
        middle = [
            row.e17_bps
            for row in primary
            if 480.0 <= row.realized_holding_minutes < 1920.0
        ]
        slow = [
            row.e17_bps
            for row in primary
            if 1920.0 <= row.realized_holding_minutes <= 2880.0
        ]
        middle_e17 = _mean(middle)
        slow_e17 = _mean(slow)
        slow_pass = (
            None
            if middle_e17 is None or slow_e17 is None
            else not (middle_e17 <= 0.0 and slow_e17 > 0.0)
        )
        gates.append(
            _gate(
                gate_id="s4_not_slow_only",
                observed={"8_to_32h_e17": middle_e17, "32_to_48h_e17": slow_e17},
                passed=slow_pass,
                reason=(
                    "s4_slow_bucket_evidence_missing"
                    if slow_pass is None
                    else "edge_must_not_exist_only_in_32_to_48h_tail"
                ),
            )
        )
        executor_state = {
            "pair_executor_state": "not_evaluated",
            "readiness": "historical_screen_only",
            "demo_eligible": False,
            "order_count": None,
            "residual_count": None,
            "pair_exec_fail_count": None,
        }
    all_passed = bool(gates) and all(row["status"] == "PASS" for row in gates)
    return {
        "all_passed": all_passed,
        "status": "PASS" if all_passed else "FAIL",
        "gates": gates,
        "executor_state": executor_state,
    }


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field_.name: _plain(getattr(value, field_.name))
            for field_ in dataclasses.fields(value)
            if not field_.name.startswith("_")
        }
    if type(value) is tuple:
        return [_plain(item) for item in value]
    if type(value) is list:
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _build_cell(
    *,
    config: object,
    experiment_id: str,
    cell: R3CellOOSLedger | None,
    operational_incomplete: bool,
    gate_reports: tuple[object, object] | None,
    relaxation_evidence: RelaxationCampaignAnalysis | None,
) -> dict[str, Any]:
    config_id = config.config_id
    family = config_id[:2]
    if cell is None:
        accepted_by_fold = [
            {
                "fold_id": fold_id,
                "accepted": None,
                "reason": _OPERATIONAL_INCOMPLETE_REASON,
            }
            for fold_id in R3_FOLD_IDS
        ]
        trades_by_fold = [
            {
                "fold_id": fold_id,
                "basket_trades": None,
                "reason": _OPERATIONAL_INCOMPLETE_REASON,
            }
            for fold_id in R3_FOLD_IDS
        ]
        total_accepted = None
        total_trades = None
        metric_reason = _OPERATIONAL_INCOMPLETE_REASON
        primary_rows: list[R3TradeRiskAttribution] = []
        base_rows: list[R3TradeRiskAttribution] = []
        upward_rows: list[R3TradeRiskAttribution] = []
    else:
        accepted_by_fold = [
            {
                "fold_id": fold.source_ledger.fold_id,
                "accepted": fold.accepted_count,
                "reason": None,
            }
            for fold in cell.folds
        ]
        trades_by_fold = [
            {
                "fold_id": fold.source_ledger.fold_id,
                "basket_trades": fold.source_ledger.basket_trade_count,
                "reason": None,
            }
            for fold in cell.folds
        ]
        total_accepted = sum(fold.accepted_count for fold in cell.folds)
        total_trades = sum(fold.source_ledger.basket_trade_count for fold in cell.folds)
        metric_reason = _ZERO_TRADES_REASON if total_trades == 0 else None
        base_rows = [
            row for fold in cell.folds for row in fold.scenario_attributions[0].rows
        ]
        primary_rows = [
            row for fold in cell.folds for row in fold.scenario_attributions[1].rows
        ]
        upward_rows = [
            row for fold in cell.folds for row in fold.scenario_attributions[2].rows
        ]
    conversion = (
        _null_metric(
            _OPERATIONAL_INCOMPLETE_REASON
            if total_accepted is None
            else "zero_accepted_denominator"
        )
        if not total_accepted
        else {"value": total_trades / total_accepted, "reason": None}
    )
    if not primary_rows:
        metrics = {
            "e0_bps": _null_metric(metric_reason),
            "e13_bps": _null_metric(metric_reason),
            "e17_bps": _null_metric(metric_reason),
            "e22_up_bps": _null_metric(metric_reason),
            "pf17": _null_metric(metric_reason),
            "win_margin_at_17": _null_metric(metric_reason),
        }
        observed_win_rate = _null_metric(metric_reason)
        weighted_p_be = _null_metric(metric_reason)
        positive_folds: int | None = None if cell is None else 0
        concentration = _null_metric(metric_reason)
        strategy = {
            "all_passed": False,
            "status": "INCOMPLETE" if cell is None else "NOT_OBSERVED",
            "reason": metric_reason,
            "gates": [],
            "executor_state": None,
        }
        sample_qualified = False
        gross_qualified = False
        full_gate = False
        common_gates = [
            _gate(
                gate_id="min_fold_trades_gte_5",
                observed=(
                    None
                    if cell is None
                    else min(
                        fold.source_ledger.basket_trade_count for fold in cell.folds
                    )
                ),
                passed=None if cell is None else False,
                reason=(
                    _OPERATIONAL_INCOMPLETE_REASON
                    if cell is None
                    else "minimum_fold_basket_trades_below_5"
                ),
            )
        ] + [
            _gate(
                gate_id=gate_id,
                observed=None,
                passed=None,
                reason=metric_reason,
            )
            for gate_id in (
                "pooled_e0_gte_25bp",
                "pooled_e17_gte_5bp",
                "pf17_gte_1_15",
                "positive_folds_gt_4",
                "monthly_concentration_lte_50pct",
                "e22_up_gt_zero",
                "win_margin_gte_3pp",
            )
        ]
    else:
        e0 = _mean([row.source_trade.execution.gross_bps for row in primary_rows])
        e13 = _mean([row.e13_bps for row in base_rows])
        e17_values = [row.e17_bps for row in primary_rows]
        e17 = _mean(e17_values)
        e22 = _mean([row.e22_bps for row in upward_rows])
        pf17, pf_passed = _pf_metric(e17_values)
        wins = sum(value > 0.0 for value in e17_values)
        win_rate = wins / len(e17_values)
        weights = [
            (1.0 if family == "S3" else row.source_trade.execution.gross_notional)
            for row in primary_rows
        ]
        if any(weight is None for weight in weights):  # pragma: no cover - DTO guard
            raise R3ScorecardError("S4 pBE weight lacks gross basket notional")
        exact_weights = [float(weight) for weight in weights if weight is not None]
        pbe = math.fsum(
            ((row.sl_bps + 17.0) / (row.tp_bps + row.sl_bps)) * weight
            for row, weight in zip(primary_rows, exact_weights, strict=True)
        ) / math.fsum(exact_weights)
        win_margin = win_rate - pbe
        metrics = {
            "e0_bps": _metric(e0),
            "e13_bps": _metric(e13),
            "e17_bps": _metric(e17),
            "e22_up_bps": _metric(e22),
            "pf17": pf17,
            "win_margin_at_17": _metric(win_margin),
        }
        observed_win_rate = _metric(win_rate)
        weighted_p_be = _metric(pbe)
        positive_folds = sum(
            bool(rows) and math.fsum(row.e17_bps for row in rows) > 0.0
            for fold in cell.folds
            for rows in (list(fold.scenario_attributions[1].rows),)
        )
        monthly: dict[str, float] = defaultdict(float)
        for row in primary_rows:
            monthly[_month_key(row.source_trade.execution.exit_ts)] += row.e17_bps
        positive_months = {key: value for key, value in monthly.items() if value > 0.0}
        monthly_concentration = (
            max(positive_months.values()) / math.fsum(positive_months.values())
            if positive_months
            else None
        )
        concentration = _metric(
            monthly_concentration,
            None if monthly_concentration is not None else "no_positive_months",
        )
        strategy = _strategy_gates(config_id=config_id, folds=cell.folds)
        sample_qualified = (
            min(fold.source_ledger.basket_trade_count for fold in cell.folds) >= 5
        )
        gross_qualified = e0 is not None and e0 >= 25.0
        full_gate = _common_full_gate_passed(
            minimum_fold_trades=min(
                fold.source_ledger.basket_trade_count for fold in cell.folds
            ),
            e0_bps=e0,
            e17_bps=e17,
            pf17_passed=pf_passed,
            positive_folds=positive_folds,
            monthly_concentration=monthly_concentration,
            e22_up_bps=e22,
            win_margin=win_margin,
            strategy_passed=strategy["all_passed"],
        )
        common_gates = [
            _gate(
                gate_id="min_fold_trades_gte_5",
                observed=min(
                    fold.source_ledger.basket_trade_count for fold in cell.folds
                ),
                passed=sample_qualified,
                reason="minimum_fold_basket_trades_must_be_gte_5",
            ),
            _gate(
                gate_id="pooled_e0_gte_25bp",
                observed=e0,
                passed=gross_qualified,
                reason="pooled_gross_expectancy_must_be_gte_25bp",
            ),
            _gate(
                gate_id="pooled_e17_gte_5bp",
                observed=e17,
                passed=e17 is not None and e17 >= 5.0,
                reason="pooled_stress17_expectancy_must_be_gte_5bp",
            ),
            _gate(
                gate_id="pf17_gte_1_15",
                observed=pf17,
                passed=pf_passed,
                reason="profit_factor_at_17_must_be_gte_1_15",
            ),
            _gate(
                gate_id="positive_folds_gt_4",
                observed=positive_folds,
                passed=positive_folds > 4,
                reason="at_least_five_of_eight_fold_e17_sums_must_be_positive",
            ),
            _gate(
                gate_id="monthly_concentration_lte_50pct",
                observed=monthly_concentration,
                passed=(
                    None
                    if monthly_concentration is None
                    else monthly_concentration <= 0.50
                ),
                reason=(
                    "no_positive_months"
                    if monthly_concentration is None
                    else "positive_month_net_concentration_must_be_lte_0_50"
                ),
            ),
            _gate(
                gate_id="e22_up_gt_zero",
                observed=e22,
                passed=e22 is not None and e22 > 0.0,
                reason="upward_path_expectancy_must_be_strictly_positive",
            ),
            _gate(
                gate_id="win_margin_gte_3pp",
                observed=win_margin,
                passed=win_margin >= 0.03,
                reason="observed_win_rate_minus_weighted_pbe_must_be_gte_0_03",
            ),
        ]
    if cell is None:
        exit_attribution = {
            "status": "INCOMPLETE",
            "reason": _OPERATIONAL_INCOMPLETE_REASON,
            "by_exit_reason": {},
        }
        dimension_attribution = {
            "status": "INCOMPLETE",
            "reason": _OPERATIONAL_INCOMPLETE_REASON,
            "by_dimension": {},
        }
    else:
        exit_order = (
            ("TP", "SL", "THESIS_EXIT", "TIMEOUT")
            if family == "S3"
            else ("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT")
        )
        dimensions = (
            ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
            if family == "S3"
            else ("XRP-DOGE", "XRP-SOL", "DOGE-SOL")
        )
        exit_attribution = {
            "status": "OBSERVED" if primary_rows else "NOT_OBSERVED",
            "reason": None if primary_rows else _ZERO_TRADES_REASON,
            "by_exit_reason": {
                reason: _attribution_bucket(
                    [
                        row
                        for row in primary_rows
                        if row.source_trade.execution.exit_reason == reason
                    ]
                )
                for reason in exit_order
            },
        }
        dimension_attribution = {
            "status": "OBSERVED" if primary_rows else "NOT_OBSERVED",
            "reason": None if primary_rows else _ZERO_TRADES_REASON,
            "by_dimension": {
                dimension: _attribution_bucket(
                    [
                        row
                        for row in primary_rows
                        if (
                            row.source_trade.event.instruments == (dimension,)
                            if family == "S3"
                            else "-".join(
                                symbol.removesuffix("USDT")
                                for symbol in row.source_trade.event.instruments
                            )
                            == dimension
                        )
                    ]
                )
                for dimension in dimensions
            },
        }
    gate_summary = (
        {
            "status": "INCOMPLETE",
            "train": None,
            "oos": None,
        }
        if gate_reports is None
        else {
            "status": "COMPLETE",
            "train": _plain(gate_reports[0]),
            "oos": _plain(gate_reports[1]),
        }
    )
    ray_ids = [ray.ray_id for ray in R3_RELAXATION_RAYS if config_id in ray.config_ids]
    relaxation_reference = {
        "status": "COMPLETE" if relaxation_evidence is not None else "INCOMPLETE",
        "ray_ids": ray_ids,
        "step_ids": (
            []
            if relaxation_evidence is None
            else [
                step.step_id
                for ray in relaxation_evidence.oos.rays
                if ray.ray_id in ray_ids
                for step in ray.steps
                if config_id in (step.strict_config_id, step.looser_config_id)
            ]
        ),
    }
    return {
        "config_id": config_id,
        "experiment_id": experiment_id,
        "family": family,
        "planning_class": config.planning_class,
        "frozen_config": dataclasses.asdict(config),
        "accepted_by_fold": accepted_by_fold,
        "basket_trades_by_fold": trades_by_fold,
        "accepted_total": total_accepted,
        "basket_trades_total": total_trades,
        "accepted_to_trade_conversion": conversion,
        "preregistered_7_tuple": {
            "accepted_and_basket_trades_by_fold": [
                {
                    "fold_id": accepted["fold_id"],
                    "accepted": accepted["accepted"],
                    "basket_trades": trades["basket_trades"],
                }
                for accepted, trades in zip(
                    accepted_by_fold, trades_by_fold, strict=True
                )
            ],
            **metrics,
        },
        "economics": metrics,
        "positive_oos_folds": positive_folds,
        "monthly_concentration": concentration,
        "observed_win_rate": observed_win_rate,
        "weighted_p_be": weighted_p_be,
        "exit_and_timeout_attribution": exit_attribution,
        "symbol_or_pair_attribution": dimension_attribution,
        "pbo": {
            "value": None,
            "status": "NOT_OBSERVED",
            "reason": _PBO_NOT_OBSERVED_REASON,
        },
        "common_gates": {
            "all_passed": all(row["status"] == "PASS" for row in common_gates),
            "gates": common_gates,
        },
        "strategy_gates": strategy,
        "section5_gate_audit_summary": gate_summary,
        "section7_relaxation_reference": relaxation_reference,
        "sample_qualified": sample_qualified,
        "gross_e0_qualified": gross_qualified,
        "sample_and_e0_qualified": sample_qualified and gross_qualified,
        "full_gate_passed": full_gate,
    }


def _fail_close_cell_research_surface(
    cell: dict[str, Any], *, operational_incomplete: bool
) -> dict[str, Any]:
    """Keep diagnostics visible while suppressing an ineligible cell verdict."""

    closed = dict(cell)
    closed["operational_status"] = (
        "INCOMPLETE" if operational_incomplete else "COMPLETE"
    )
    closed["research_eligible"] = not operational_incomplete
    if operational_incomplete:
        closed["full_gate_passed"] = False
    return closed


def _incomplete_reasons(
    *,
    context: R3ProductionEvidenceContext,
    accounting: object,
    oos_ledger: object,
    gate_evidence: object,
    relaxation_evidence: object,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if context.operational_status != "COMPLETE":
        reasons.append(
            f"plan:{context.operational_status}:{context.operational_blocker_reason}"
        )
    if type(accounting) is R3ScorecardAccountingEvidence:
        accounting.__post_init__()
        report = accounting.report
    elif type(accounting) is Exact12AccountingReport:
        report = accounting
        reasons.append("accounting:unsealed_attempt_evidence")
    else:
        report = None
        reasons.append("accounting:missing_or_wrong_type")
    if report is not None:
        if report.campaign_run_id != context.campaign_run_id:
            reasons.append("accounting:campaign_run_id_mismatch")
        if report.exact_12_mapping_hash != context.exact_12_mapping_hash:
            reasons.append("accounting:exact_12_mapping_hash_mismatch")
        expected_counts = {"completed": 12, "rejected": 0, "crashed": 0, "timeout": 0}
        exact_complete = (
            report.expected_total == 12
            and report.registered_total == 12
            and report.primary_attempts == 12
            and report.total_attempts == 12
            and report.retry_attempts == 0
            and dict(report.status_counts) == expected_counts
            and not report.missing_row_ids
            and not report.extra_experiment_ids
            and not report.mismatch_row_ids
            and not report.duplicate_or_gap_row_ids
            and report.accounting_complete is True
            and report.all_primary_completed is True
            and report.performance_usable is True
        )
        if not report.accounting_complete:
            reasons.append("accounting:incomplete")
        if not report.performance_usable:
            reasons.append("accounting:performance_unusable")
        if not exact_complete:
            reasons.append("accounting:exact_complete_invariants_failed")
    if type(oos_ledger) is not R3AllCellOOSLedger:
        reasons.append("oos_ledger:missing_or_wrong_type")
    else:
        _validate_issued_oos_ledger(oos_ledger)
        if oos_ledger.campaign_identity_sha256 != context.campaign_identity_sha256:
            reasons.append("oos_ledger:campaign_identity_mismatch")
        if oos_ledger.operational_status != "COMPLETE":
            reasons.extend(
                f"oos_ledger:{reason}" for reason in oos_ledger.incomplete_reasons
            )
    if type(gate_evidence) is not ProductionGateCampaignEvidence:
        reasons.append("section5:missing_or_wrong_type")
    else:
        gate_evidence.__post_init__()
        if (
            gate_evidence.campaign_identity_sha256 != context.campaign_identity_sha256
            or gate_evidence.campaign_run_id != context.campaign_run_id
            or gate_evidence.exact_12_mapping_hash != context.exact_12_mapping_hash
            or gate_evidence.ordered_mapping != context.ordered_mapping
        ):
            reasons.append("section5:campaign_identity_mismatch")
        if (
            len(gate_evidence.reports) != 24
            or len(gate_evidence.evidence_cell_order) != 192
        ):
            reasons.append("section5:exact_24_report_or_192_cell_coverage_missing")
        if not gate_evidence.evidence_promoted:
            reasons.append("section5:evidence_not_promoted")
    if type(relaxation_evidence) is R3ScorecardRelaxationEvidence:
        relaxation_evidence.__post_init__()
        relaxation = relaxation_evidence.analysis
        if type(oos_ledger) is R3AllCellOOSLedger and (
            relaxation_evidence.oos_evidence.ledgers
            != tuple(
                fold.source_ledger for cell in oos_ledger.cells for fold in cell.folds
            )
            or relaxation_evidence.oos_evidence.terminal_incompletes
            != tuple(
                fold.terminal_incomplete
                for cell in oos_ledger.cells
                for fold in cell.folds
                if fold.terminal_incomplete is not None
            )
        ):
            reasons.append("section7:oos_ledger_cross_seal_mismatch")
    elif type(relaxation_evidence) is RelaxationCampaignAnalysis:
        relaxation = relaxation_evidence
        reasons.append("section7:unsealed_raw_cohort_evidence")
    else:
        relaxation = None
        reasons.append("section7:missing_or_wrong_type")
    if relaxation is not None:
        if (
            relaxation.schema_version != "rob974.r3.relaxation.v1"
            or relaxation.campaign_hash != context.campaign_identity_sha256
            or relaxation.campaign_run_id != context.campaign_run_id
            or relaxation.exact_12_mapping_hash != context.exact_12_mapping_hash
            or relaxation.ordered_mapping != context.ordered_mapping
            or relaxation.plan_operational_status != context.operational_status
            or relaxation.plan_operational_blocker_reason
            != context.operational_blocker_reason
        ):
            reasons.append("section7:campaign_identity_mismatch")
        if relaxation.oos.fold_ids != R3_FOLD_IDS:
            reasons.append("section7:fold_order_mismatch")
        if relaxation.operational_status == "COMPLETE":
            expected_ray_ids = tuple(ray.ray_id for ray in R3_RELAXATION_RAYS)
            if tuple(ray.ray_id for ray in relaxation.oos.rays) != (expected_ray_ids):
                reasons.append("section7:ray_order_mismatch")
            else:
                for authority, observed in zip(
                    R3_RELAXATION_RAYS,
                    relaxation.oos.rays,
                    strict=True,
                ):
                    expected_steps = tuple(
                        (left, right, f"{left}->{right}")
                        for left, right in zip(
                            authority.config_ids[:-1],
                            authority.config_ids[1:],
                            strict=True,
                        )
                    )
                    actual_steps = tuple(
                        (
                            step.strict_config_id,
                            step.looser_config_id,
                            step.step_id,
                        )
                        for step in observed.steps
                    )
                    if actual_steps != expected_steps or any(
                        tuple(fold.fold_id for fold in step.folds) != R3_FOLD_IDS
                        for step in observed.steps
                    ):
                        reasons.append(
                            f"section7:{authority.ray_id}:step_or_fold_drift"
                        )
        if relaxation.operational_status != "COMPLETE":
            reasons.extend(
                f"section7:{reason}" for reason in relaxation.incomplete_reasons
            )
        if not relaxation.evidence_promoted:
            reasons.append("section7:evidence_not_promoted")
    return tuple(dict.fromkeys(reasons))


def _family_verdict(*, family: str, cells: list[dict[str, Any]]) -> dict[str, Any]:
    family_cells = [cell for cell in cells if cell["family"] == family]
    by_id = {cell["config_id"]: cell for cell in family_cells}
    adjacent_pairs = [
        (left, right)
        for left, right in R3_ADJACENCY_EDGES
        if left in by_id and right in by_id
    ]
    continue_pairs = [
        [left, right]
        for left, right in adjacent_pairs
        if by_id[left]["sample_and_e0_qualified"]
        and by_id[right]["sample_and_e0_qualified"]
        and (by_id[left]["full_gate_passed"] or by_id[right]["full_gate_passed"])
    ]
    full_winners = [
        cell["config_id"] for cell in family_cells if cell["full_gate_passed"]
    ]
    pruned = {
        item.config_id for item in R3_PRUNED_BOUNDARY_NEIGHBORS if item.family == family
    }
    if continue_pairs:
        decision = "CONTINUE"
        reason_codes = ["adjacent_sample_e0_pair_with_full_gate_member"]
    elif len(full_winners) == 1 and full_winners[0] in pruned:
        decision = "NARROW"
        reason_codes = ["single_full_gate_pruned_boundary_winner"]
    else:
        decision = "TERMINATE"
        reason_codes = []
        if not any(cell["sample_and_e0_qualified"] for cell in family_cells):
            reason_codes.append("no_sample_and_e0_intersection")
        if full_winners:
            reason_codes.append("isolated_or_internal_full_gate_winner")
        if not reason_codes:
            reason_codes.append("no_adjacent_region_satisfies_continue")
    return {
        "family": family,
        "operational_status": "COMPLETE",
        "research_decision": decision,
        "reason_codes": reason_codes,
        "qualifying_adjacent_pairs": continue_pairs,
        "full_gate_winners": full_winners,
    }


def _section3_rows_from_observations(
    *,
    cells: list[dict[str, Any]],
    monotone_rays: list[str],
    relaxation_rays_present: bool,
) -> list[dict[str, Any]]:
    by_claim: dict[str, tuple[str, object, str]] = {}
    failing_sample = [
        {
            "config_id": cell["config_id"],
            "fold_ids": [
                row["fold_id"]
                for row in cell["basket_trades_by_fold"]
                if row["basket_trades"] < 5
            ],
        }
        for cell in cells
        if any(row["basket_trades"] < 5 for row in cell["basket_trades_by_fold"])
    ]
    by_claim["sample_hypothesis"] = (
        "FAIL" if failing_sample else "PASS",
        failing_sample,
        "observed_min_fold_basket_trade_contract",
    )
    sample_cells = [cell for cell in cells if cell["sample_qualified"]]
    if not sample_cells:
        by_claim["gross_edge_hypothesis"] = (
            "NOT_OBSERVED",
            None,
            "no_sample_qualified_cells",
        )
    else:
        gross_failures = [
            cell["config_id"] for cell in sample_cells if not cell["gross_e0_qualified"]
        ]
        by_claim["gross_edge_hypothesis"] = (
            "FAIL" if gross_failures else "PASS",
            gross_failures,
            "sample_qualified_pooled_e0_checked_against_25bp",
        )
    edge_cells = [cell for cell in cells if cell["sample_and_e0_qualified"]]
    for claim_id, gate_ids, reason in (
        (
            "after_cost_hypothesis",
            {
                "pooled_e17_gte_5bp",
                "pf17_gte_1_15",
                "e22_up_gt_zero",
                "win_margin_gte_3pp",
            },
            "sample_e0_cells_checked_against_after_cost_gates",
        ),
        (
            "fold_generalization",
            {"positive_folds_gt_4"},
            "sample_e0_cells_checked_for_five_positive_fold_sums",
        ),
        (
            "monthly_concentration",
            {"monthly_concentration_lte_50pct"},
            "sample_e0_cells_checked_on_positive_month_net_share",
        ),
    ):
        if not edge_cells:
            by_claim[claim_id] = ("NOT_OBSERVED", None, "no_sample_e0_cells")
            continue
        failures = [
            cell["config_id"]
            for cell in edge_cells
            if any(
                gate["gate_id"] in gate_ids and gate["status"] != "PASS"
                for gate in cell["common_gates"]["gates"]
            )
        ]
        by_claim[claim_id] = (
            "FAIL" if failures else "PASS",
            failures,
            reason,
        )
    qualified = {cell["config_id"] for cell in edge_cells}
    qualifying_edges = [
        [left, right]
        for left, right in R3_ADJACENCY_EDGES
        if left in qualified and right in qualified
    ]
    by_claim["threshold_region"] = (
        "PASS" if qualifying_edges else "FAIL",
        qualifying_edges,
        "exact_manifest_adjacency_over_sample_e0_cells",
    )
    if not relaxation_rays_present:
        by_claim["relaxation_selection_effect"] = (
            "NOT_OBSERVED",
            None,
            "no_complete_oos_relaxation_rays",
        )
    else:
        death = bool(monotone_rays and not edge_cells)
        by_claim["relaxation_selection_effect"] = (
            "FAIL" if death else "PASS",
            {
                "monotone_edge_decay_rays": monotone_rays,
                "sample_e0_cell_ids": sorted(qualified),
            },
            "monotone_decay_requires_sample_edge_intersection",
        )
    return [
        {
            "claim_id": claim_id,
            "claim": claim,
            "falsifying_observation": death,
            "status": by_claim[claim_id][0],
            "observed": by_claim[claim_id][1],
            "reason": by_claim[claim_id][2],
        }
        for claim_id, claim, death in _SECTION3_CLAIMS
    ]


def _section3_rows(
    *,
    cells: list[dict[str, Any]],
    relaxation_evidence: RelaxationCampaignAnalysis,
) -> list[dict[str, Any]]:
    return _section3_rows_from_observations(
        cells=cells,
        monotone_rays=[
            ray.ray_id
            for ray in relaxation_evidence.oos.rays
            if ray.monotone_edge_decay is True
        ],
        relaxation_rays_present=bool(relaxation_evidence.oos.rays),
    )


def build_r3_scorecard(
    *,
    evidence_context: object,
    accounting: object,
    oos_ledger: object,
    gate_evidence: object,
    relaxation_evidence: object,
) -> dict[str, object]:
    """Build the R3 scorecard, fail-closing incomplete evidence before §8."""

    context = require_r3_production_evidence_context(evidence_context)
    reasons = _incomplete_reasons(
        context=context,
        accounting=accounting,
        oos_ledger=oos_ledger,
        gate_evidence=gate_evidence,
        relaxation_evidence=relaxation_evidence,
    )
    operational_incomplete = bool(reasons)
    ledger = oos_ledger if type(oos_ledger) is R3AllCellOOSLedger else None
    cell_by_id = (
        {} if ledger is None else {cell.config_id: cell for cell in ledger.cells}
    )
    gate_by_id: dict[str, tuple[object, object]] = {}
    if type(gate_evidence) is ProductionGateCampaignEvidence:
        for index, config in enumerate(FROZEN_R3_ROSTER):
            gate_by_id[config.config_id] = (
                gate_evidence.reports[index * 2],
                gate_evidence.reports[index * 2 + 1],
            )
    exact_relaxation = (
        relaxation_evidence.analysis
        if type(relaxation_evidence) is R3ScorecardRelaxationEvidence
        else (
            relaxation_evidence
            if type(relaxation_evidence) is RelaxationCampaignAnalysis
            else None
        )
    )
    experiment_by_id = dict(context.ordered_mapping)
    cells = [
        _fail_close_cell_research_surface(
            _build_cell(
                config=config,
                experiment_id=experiment_by_id[config.config_id],
                cell=cell_by_id.get(config.config_id),
                operational_incomplete=operational_incomplete,
                gate_reports=gate_by_id.get(config.config_id),
                relaxation_evidence=exact_relaxation,
            ),
            operational_incomplete=operational_incomplete,
        )
        for config in FROZEN_R3_ROSTER
    ]
    if operational_incomplete or exact_relaxation is None:
        section3 = [
            {
                "claim_id": claim_id,
                "claim": claim,
                "falsifying_observation": death,
                "status": "INCOMPLETE",
                "observed": None,
                "reason": ";".join(reasons) or _OPERATIONAL_INCOMPLETE_REASON,
            }
            for claim_id, claim, death in _SECTION3_CLAIMS
        ]
        family_verdicts = [
            {
                "family": family,
                "operational_status": "INCOMPLETE",
                "research_decision": None,
                "reason_codes": list(reasons),
                "qualifying_adjacent_pairs": [],
                "full_gate_winners": [],
            }
            for family in ("S3", "S4")
        ]
        campaign_decision = None
        campaign_reason_codes = list(reasons)
    else:
        section3 = _section3_rows(
            cells=cells,
            relaxation_evidence=exact_relaxation,
        )
        family_verdicts = [
            _family_verdict(family=family, cells=cells) for family in ("S3", "S4")
        ]
        priority = {"CONTINUE": 0, "NARROW": 1, "TERMINATE": 2}
        winner = min(
            family_verdicts,
            key=lambda row: priority[row["research_decision"]],
        )
        campaign_decision = winner["research_decision"]
        campaign_reason_codes = [
            f"{row['family']}:{reason}"
            for row in family_verdicts
            if row["research_decision"] == campaign_decision
            for reason in row["reason_codes"]
        ]
    scorecard: dict[str, object] = {
        "schema_version": R3_SCORECARD_SCHEMA_VERSION,
        "lineage": {
            "campaign_identity_sha256": context.campaign_identity_sha256,
            "campaign_run_id": context.campaign_run_id,
            "exact_12_mapping_hash": context.exact_12_mapping_hash,
            "ordered_mapping": [
                {"config_id": row_id, "experiment_id": experiment_id}
                for row_id, experiment_id in context.ordered_mapping
            ],
            "folds": [
                {
                    "fold_id": fold.fold_id,
                    "fold_index": fold.fold_index,
                    "train_start_ms": fold.train_start_ms,
                    "train_end_ms": fold.train_end_ms,
                    "embargo_start_ms": fold.embargo_start_ms,
                    "embargo_end_ms": fold.embargo_end_ms,
                    "oos_start_ms": fold.oos_start_ms,
                    "oos_end_ms": fold.oos_end_ms,
                }
                for fold in context.folds
            ],
        },
        "operational": {
            "status": "INCOMPLETE" if operational_incomplete else "COMPLETE",
            "incomplete_reasons": list(reasons),
        },
        "exact_12_accounting": _accounting_payload(
            accounting
            if type(accounting)
            in (R3ScorecardAccountingEvidence, Exact12AccountingReport)
            else None
        ),
        "cells": cells,
        "section3_falsification": section3,
        "section5_gate_audit": {
            "status": "INCOMPLETE" if operational_incomplete else "COMPLETE",
            "schema_version": (
                gate_evidence.schema_version
                if type(gate_evidence) is ProductionGateCampaignEvidence
                else None
            ),
            "report_order": (
                []
                if type(gate_evidence) is not ProductionGateCampaignEvidence
                else [
                    {
                        "config_id": report.scope.config_id,
                        "phase": report.scope.phase,
                        "fold_ids": [fold.fold_id for fold in report.folds],
                    }
                    for report in gate_evidence.reports
                ]
            ),
            "evidence_cell_order": (
                []
                if type(gate_evidence) is not ProductionGateCampaignEvidence
                else [list(row) for row in gate_evidence.evidence_cell_order]
            ),
            "reports": (
                []
                if type(gate_evidence) is not ProductionGateCampaignEvidence
                else [_plain(report) for report in gate_evidence.reports]
            ),
        },
        "section7_relaxation": {
            "status": "INCOMPLETE" if operational_incomplete else "COMPLETE",
            "schema_version": (
                exact_relaxation.schema_version
                if exact_relaxation is not None
                else None
            ),
            "ray_order": [ray.ray_id for ray in R3_RELAXATION_RAYS],
            "oos": (None if exact_relaxation is None else _plain(exact_relaxation.oos)),
            "train_diagnostic": (
                None
                if exact_relaxation is None
                else _plain(exact_relaxation.train_diagnostic)
            ),
        },
        "pruned_boundary_neighbors": [
            {
                "config_id": item.config_id,
                "family": item.family,
                "external_parameters": [
                    {"name": name, "value": value}
                    for name, value in item.external_parameters
                ],
            }
            for item in R3_PRUNED_BOUNDARY_NEIGHBORS
        ],
        "family_verdicts": family_verdicts,
        "campaign_verdict": {
            "operational_status": (
                "INCOMPLETE" if operational_incomplete else "COMPLETE"
            ),
            "research_decision": campaign_decision,
            "priority": ["CONTINUE", "NARROW", "TERMINATE"],
            "reason_codes": campaign_reason_codes,
        },
    }
    return scorecard


def _validate_canonical_value(value: Any, path: str = "$") -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise R3ScorecardError(f"nonfinite canonical value at {path}")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_canonical_value(item, f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise R3ScorecardError(f"non-string canonical key at {path}")
            _validate_canonical_value(item, f"{path}.{key}")
        return
    raise R3ScorecardError(f"unsupported canonical type at {path}: {type(value)!r}")


def _validate_scorecard_order(scorecard: Mapping[str, object]) -> None:
    if scorecard.get("schema_version") != R3_SCORECARD_SCHEMA_VERSION:
        raise R3ScorecardError("R3 scorecard schema version mismatch")
    cells = scorecard.get("cells")
    if type(cells) is not list or [cell.get("config_id") for cell in cells] != [
        row.config_id for row in FROZEN_R3_ROSTER
    ]:
        raise R3ScorecardError("R3 scorecard cells are unordered or incomplete")
    section3 = scorecard.get("section3_falsification")
    if type(section3) is not list or [row.get("claim_id") for row in section3] != [
        claim_id for claim_id, _claim, _death in _SECTION3_CLAIMS
    ]:
        raise R3ScorecardError("R3 §3 falsification rows are unordered or incomplete")
    relaxation = scorecard.get("section7_relaxation")
    if type(relaxation) is not dict or relaxation.get("ray_order") != [
        ray.ray_id for ray in R3_RELAXATION_RAYS
    ]:
        raise R3ScorecardError("R3 relaxation rays are unordered or incomplete")


def canonical_r3_json_bytes(scorecard: Mapping[str, object]) -> bytes:
    """Return strict deterministic semantic JSON; NaN/Infinity are forbidden."""

    if not isinstance(scorecard, Mapping):
        raise TypeError("scorecard must be a mapping")
    _validate_scorecard_order(scorecard)
    _validate_canonical_value(scorecard)
    try:
        rendered = json.dumps(
            scorecard,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise R3ScorecardError("R3 scorecard is not canonical JSON") from exc
    return rendered.encode("ascii")


def hash_r3_canonical_bytes(canonical_bytes: bytes) -> str:
    if type(canonical_bytes) is not bytes:
        raise TypeError("canonical_bytes must be exact bytes")
    return sha256(canonical_bytes).hexdigest()


def build_r3_artifact_pair(scorecard: Mapping[str, object]) -> R3ArtifactPair:
    """Build paired JSON/Markdown bytes without publishing either artifact."""

    from rob974_r3_markdown import render_r3_markdown

    json_bytes = canonical_r3_json_bytes(scorecard)
    semantic_sha256 = hash_r3_canonical_bytes(json_bytes)
    decoded = json.loads(json_bytes)
    markdown_bytes = render_r3_markdown(
        decoded,
        semantic_sha256=semantic_sha256,
    )
    return R3ArtifactPair(
        json_bytes=json_bytes,
        markdown_bytes=markdown_bytes,
        semantic_sha256=semantic_sha256,
        markdown_sha256=sha256(markdown_bytes).hexdigest(),
    )


def verify_r3_artifact_pair(
    *, json_bytes: bytes, markdown_bytes: bytes
) -> dict[str, object]:
    """Require canonical JSON and byte-exact Markdown regenerated from it."""

    from rob974_r3_markdown import render_r3_markdown

    if type(json_bytes) is not bytes or type(markdown_bytes) is not bytes:
        raise TypeError("artifact pair members must be exact bytes")
    try:
        decoded = json.loads(json_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise R3ScorecardError("canonical JSON artifact is malformed") from exc
    if canonical_r3_json_bytes(decoded) != json_bytes:
        raise R3ScorecardError("JSON artifact is not exact canonical bytes")
    semantic_sha256 = hash_r3_canonical_bytes(json_bytes)
    expected = render_r3_markdown(
        decoded,
        semantic_sha256=semantic_sha256,
    )
    if markdown_bytes != expected:
        raise R3ScorecardError("Markdown semantic mismatch")
    return decoded


__all__ = [
    "R3_SCORECARD_SCHEMA_VERSION",
    "R3AllCellOOSLedger",
    "R3ArtifactPair",
    "R3CellOOSInput",
    "R3CellOOSLedger",
    "R3FoldOOSInput",
    "R3FoldOOSLedger",
    "R3FoldScenarioAttribution",
    "R3PrunedBoundaryNeighbor",
    "R3ScorecardAccountingEvidence",
    "R3ScorecardError",
    "R3ScorecardRelaxationEvidence",
    "R3TradeRiskAttribution",
    "build_r3_artifact_pair",
    "build_r3_scorecard",
    "canonical_r3_json_bytes",
    "hash_r3_canonical_bytes",
    "issue_r3_all_cell_oos_ledger",
    "issue_r3_fold_scenario_attribution",
    "issue_r3_scorecard_accounting",
    "issue_r3_scorecard_relaxation_evidence",
    "verify_r3_artifact_pair",
]
