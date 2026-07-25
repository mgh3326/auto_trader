"""ROB-1060 H2 — the 4 measured execution parameters + cost scenarios + gate
thresholds, sealed literally from the Run A preregistration (SHA-256
``67b5d3c2...``), the params-seal-draft (SHA-256 ``dc9232ef...``), and the
operator PEPE/SHIB exclusion decision (Linear ROB-1060 comment,
2026-07-25). No value here is rounded, approximated, or reinterpreted.

Pure stdlib only (via ``source_provenance`` for the SHA-256-verified raw
JSON). No app/DB/network import.
"""

from __future__ import annotations

from dataclasses import dataclass

import source_provenance as sp

__all__ = [
    "CostScenarioCountError",
    "CostScenarioNameError",
    "CostScenarios",
    "EligibleUniverseSeal",
    "FrozenBasisCapSeal",
    "GateCondition",
    "GateThresholds",
    "ParamSealError",
    "PaperFeeSeal",
    "RunStatusBlock",
    "SealedParams",
    "SpreadCensusSeal",
    "SymbolSpreadEntry",
    "SymbolUniverseEntry",
    "UniverseSealError",
    "build_sealed_params",
    "validate_cost_scenarios",
    "validate_sealed_universe",
]

# Operator decision (Linear ROB-1060 comment, 2026-07-25) — binding, no
# post-seal change permitted (NO_THRESHOLD_RELAXATION).
_EXCLUDED_SYMBOLS: tuple[str, ...] = ("PEPE", "SHIB")
_EXCLUSION_REASON = "basis_red_grade_hv_p95_ge_28bp"
_EXCLUSION_AUTHORITY = "operator_decision_2026-07-25"
_SEALED_EFFECTIVE_N = 20

_COST_HETEROGENEITY_BASES = ("BAT", "BCH", "LTC", "XTZ", "AVAX")

_REQUIRED_COST_SCENARIO_NAMES = frozenset({"C50", "C100", "C120", "C150"})


class ParamSealError(Exception):
    """Base error for the sealed execution-parameter domain."""


class CostScenarioCountError(ParamSealError):
    """Cost scenarios are not EXACTLY 4."""


class CostScenarioNameError(ParamSealError):
    """Cost scenario names are not exactly {C50, C100, C120, C150}."""


class UniverseSealError(ParamSealError):
    """The sealed universe violates a sealed invariant (N, exclusion set)."""


@dataclass(frozen=True)
class SymbolUniverseEntry:
    alpaca_symbol: str
    quote_mode: str
    alpaca_first_daily: str
    # H1 AC16 / ROB-1058 SS4 — Alpaca's historical listing date cannot be
    # retrieved retroactively via the current API; `alpaca_first_daily` (the
    # first daily bar) stands in as a PIT proxy. This flag is always True and
    # exists so no consumer can mistake the proxy for a verified listing date.
    alpaca_first_daily_is_pit_proxy: bool = True


@dataclass(frozen=True)
class EligibleUniverseSeal:
    source_sha256: str
    n_raw_today: int
    excluded_symbols: tuple[str, ...]
    exclusion_reason: str
    exclusion_authority: str
    sealed_effective_n: int
    raw_symbols: tuple[SymbolUniverseEntry, ...]
    sealed_symbols: tuple[SymbolUniverseEntry, ...]


@dataclass(frozen=True)
class SymbolSpreadEntry:
    alpaca_symbol: str
    median_bp: float


@dataclass(frozen=True)
class SpreadCensusSeal:
    source_sha256: str
    median_of_medians_all_bp: float
    median_of_medians_eligible_bp: float
    cost_heterogeneity_symbols: tuple[SymbolSpreadEntry, ...]
    note: str


@dataclass(frozen=True)
class PaperFeeSeal:
    source_sha256: str
    paper_fee_bp: float
    manual_fee_deduction: str
    confirmed_end_of_day_posting_format: bool
    provenance_note: str


@dataclass(frozen=True)
class FrozenBasisCapSeal:
    source_sha256: str
    method: str
    proxy_note: str
    raw_cap_bp: dict
    sealed_cap_bp: dict


@dataclass(frozen=True)
class CostScenarios:
    scenarios_bp: dict
    primary: str
    upward: str


@dataclass(frozen=True)
class GateCondition:
    """One literal gate condition. ``value`` is the EXACT literal from the
    authority doc — a 2-tuple like ``(5, 8)`` means "at least 5 of 8 folds",
    never collapsed to a rounded fraction."""

    metric: str
    op: str
    value: object
    unit: str | None = None


@dataclass(frozen=True)
class GateThresholds:
    min_modeled_entries_per_fold: int
    fixed_tp: str
    future_tp_min_bp: int
    ap_a1: tuple[GateCondition, ...]
    ap_a2: tuple[GateCondition, ...]
    ap_a2_turnover_band: tuple[float, float]


@dataclass(frozen=True)
class RunStatusBlock:
    total_configs: int
    oos_folds: int
    oos_days: int
    order_type: str
    economic_execution: str
    min_broker_order_usd: int
    min_strategy_target_usd: int
    no_threshold_relaxation: bool
    no_post_pnl_config_addition: bool


@dataclass(frozen=True)
class SealedParams:
    universe: EligibleUniverseSeal
    spread_census: SpreadCensusSeal
    paper_fee: PaperFeeSeal
    frozen_basis_cap: FrozenBasisCapSeal
    cost_scenarios: CostScenarios
    gate_thresholds: GateThresholds
    run_status: RunStatusBlock


def validate_cost_scenarios(scenarios_bp: dict) -> None:
    """Fail closed unless ``scenarios_bp`` is EXACTLY {C50, C100, C120, C150}
    (Run A SS17/AC14 — 5th scenario, cost-zero substitute, forbidden)."""
    if len(scenarios_bp) != 4:
        raise CostScenarioCountError(
            f"expected exactly 4 cost scenarios, got {len(scenarios_bp)}: "
            f"{sorted(scenarios_bp)}"
        )
    if set(scenarios_bp) != _REQUIRED_COST_SCENARIO_NAMES:
        raise CostScenarioNameError(
            f"expected exactly {sorted(_REQUIRED_COST_SCENARIO_NAMES)}, got "
            f"{sorted(scenarios_bp)}"
        )


def validate_sealed_universe(universe: EligibleUniverseSeal) -> None:
    """Fail closed unless the sealed universe honors the operator exclusion
    decision and the N_t >= 18 invariant (Run A S6.7 / AC11)."""
    if tuple(universe.excluded_symbols) != _EXCLUDED_SYMBOLS:
        raise UniverseSealError(
            f"excluded_symbols must be exactly {_EXCLUDED_SYMBOLS}, got "
            f"{tuple(universe.excluded_symbols)}"
        )
    if universe.sealed_effective_n != _SEALED_EFFECTIVE_N:
        raise UniverseSealError(
            f"sealed_effective_n must be exactly {_SEALED_EFFECTIVE_N}, got "
            f"{universe.sealed_effective_n}"
        )
    if universe.sealed_effective_n < 18:
        raise UniverseSealError(
            f"N_t >= 18 invariant violated: sealed_effective_n="
            f"{universe.sealed_effective_n}"
        )
    sealed_bases = {s.alpaca_symbol.split("/")[0] for s in universe.sealed_symbols}
    for excluded in universe.excluded_symbols:
        if excluded in sealed_bases:
            raise UniverseSealError(
                f"excluded symbol {excluded!r} is still present in sealed_symbols"
            )


def _build_universe_seal() -> EligibleUniverseSeal:
    raw = sp.load_universe_map()
    raw_symbols = tuple(
        SymbolUniverseEntry(
            alpaca_symbol=p["alpaca_symbol"],
            quote_mode=p["quote_mode"],
            alpaca_first_daily=p.get("alpaca_first_daily") or "",
        )
        for p in raw["pairs"]
        if p.get("eligible_today") is True
    )
    sealed_symbols = tuple(
        e for e in raw_symbols if e.alpaca_symbol.split("/")[0] not in _EXCLUDED_SYMBOLS
    )
    seal = EligibleUniverseSeal(
        source_sha256=sp.UNIVERSE_MAP_SHA256,
        n_raw_today=raw["n_eligible_today"],
        excluded_symbols=_EXCLUDED_SYMBOLS,
        exclusion_reason=_EXCLUSION_REASON,
        exclusion_authority=_EXCLUSION_AUTHORITY,
        sealed_effective_n=_SEALED_EFFECTIVE_N,
        raw_symbols=raw_symbols,
        sealed_symbols=sealed_symbols,
    )
    validate_sealed_universe(seal)
    return seal


def _build_spread_census_seal() -> SpreadCensusSeal:
    raw = sp.load_spread_census()
    cross = raw["cross_sectional"]
    per_symbol = raw["per_symbol"]
    heterogeneity = tuple(
        SymbolSpreadEntry(
            alpaca_symbol=f"{base}/USD",
            median_bp=per_symbol[f"{base}/USD"]["median_bp"],
        )
        for base in _COST_HETEROGENEITY_BASES
    )
    return SpreadCensusSeal(
        source_sha256=sp.SPREAD_CENSUS_SHA256,
        median_of_medians_all_bp=cross["median_of_medians_all_bp"],
        median_of_medians_eligible_bp=cross["median_of_medians_eligible_bp"],
        cost_heterogeneity_symbols=heterogeneity,
        note=(
            "BAT/BCH/LTC/XTZ/AVAX median full-quoted spread is 58-65bp, "
            "exceeding the C120 spread component (~62bp p95 assumption) as a "
            "matter of routine, not tail risk. This does NOT relax C120/C150 "
            "for these symbols — it is recorded as a documented fact only "
            "(seal-draft SS2 flag 2)."
        ),
    )


def _build_paper_fee_seal() -> PaperFeeSeal:
    sp.load_fee_probe()  # SHA-256-verifies the probe fixture; content already
    # summarized (coin-side 25.0bp) in the params-seal-draft SS3, which this
    # module treats as the literal authority for the sealed value.
    return PaperFeeSeal(
        source_sha256=sp.FEE_PROBE_SHA256,
        paper_fee_bp=25.0,
        manual_fee_deduction="FORBIDDEN",
        confirmed_end_of_day_posting_format=False,
        provenance_note=(
            "Measured 25.0bp coin-side round-trip taker fee from a single "
            "$25 marketable-limit probe (fee_probe_20260725T142435Z.json). "
            "The end-of-day CFEE activity posting format re-verification is "
            "pending under ROB-1066 — the UTC day of the probe had not "
            "closed as of this seal. This value is sealed as MEASURED, not "
            "as fully confirmed reconciliation."
        ),
    )


def _build_frozen_basis_cap_seal() -> FrozenBasisCapSeal:
    raw = sp.load_basis_analysis_full()
    raw_cap_bp = dict(raw["_frozen_basis_cap_proposal_bp"])
    sealed_cap_bp = {
        symbol: cap
        for symbol, cap in raw_cap_bp.items()
        if symbol.split("/")[0] not in _EXCLUDED_SYMBOLS
    }
    return FrozenBasisCapSeal(
        source_sha256=sp.BASIS_ANALYSIS_FULL_SHA256,
        method="ceil(hv_p95_bp) + 3bp margin (Run B SS10)",
        proxy_note=(
            "close-basis proxy (30-day 1m close, |logret| p95 high-vol "
            "window) — NOT executable/BBO basis. Executable-basis "
            "re-judgment is deferred to the ROB-1067 forward collector; "
            "this cap is not PnL-tunable (Run A S13.1)."
        ),
        raw_cap_bp=raw_cap_bp,
        sealed_cap_bp=sealed_cap_bp,
    )


def _build_cost_scenarios() -> CostScenarios:
    scenarios_bp = {"C50": 50, "C100": 100, "C120": 120, "C150": 150}
    validate_cost_scenarios(scenarios_bp)
    return CostScenarios(scenarios_bp=scenarios_bp, primary="C120", upward="C150")


def _build_ap_a1_gate() -> tuple[GateCondition, ...]:
    # Run A S11.7 — 11 conditions, verbatim.
    return (
        GateCondition("all_folds_entries", ">=", 5, "count"),
        GateCondition("median_hold_days", ">=", 3, "days"),
        GateCondition("pooled_gross_ev_bp", ">=", 180, "bp"),
        GateCondition("pooled_e120_bp", ">=", 60, "bp"),
        GateCondition("e120_bootstrap_95_lower_bound", ">", 0, "bp"),
        GateCondition("e150", ">", 0, "bp"),
        GateCondition("pf120", ">=", 1.15, None),
        GateCondition("positive_folds", ">=", (5, 8), "folds_of_8"),
        GateCondition("max_oos_dd_pct", "<=", 20, "pct"),
        GateCondition("monthly_concentration_pct", "<=", 50, "pct"),
        GateCondition("symbol_concentration_pct", "<=", 40, "pct"),
    )


def _build_ap_a2_gate() -> tuple[GateCondition, ...]:
    # Run A S12.7 — the authority doc literally enumerates 13 conditions
    # (ROB-1060 AC12 labels this "12항"; see test_ap_a2_gate_has_the_13_
    # conditions_literally_present_in_section_12_7 for the flagged
    # discrepancy — sealed verbatim, not truncated to match the AC count).
    return (
        GateCondition("all_folds_entries", ">=", 5, "count"),
        GateCondition("turnover_in_intersection", "==", True, None),
        GateCondition("pooled_gross_ev_bp", ">=", 200, "bp"),
        GateCondition("pooled_e120_bp", ">=", 80, "bp"),
        GateCondition("e120_bootstrap_lower_bound", ">", 0, "bp"),
        GateCondition("e150", ">", 0, "bp"),
        GateCondition("pf120", ">=", 1.15, None),
        GateCondition("positive_folds", ">=", (5, 8), "folds_of_8"),
        GateCondition("equal_weight_e120_positive", "==", True, None),
        GateCondition("topk_vs_middle_wins", ">=", (5, 8), "folds_of_8"),
        GateCondition("max_oos_dd_pct", "<=", 20, "pct"),
        GateCondition("monthly_concentration_pct", "<=", 50, "pct"),
        GateCondition("symbol_concentration_pct", "<=", 35, "pct"),
    )


def _build_gate_thresholds() -> GateThresholds:
    return GateThresholds(
        min_modeled_entries_per_fold=5,
        fixed_tp="NONE",
        future_tp_min_bp=240,
        ap_a1=_build_ap_a1_gate(),
        ap_a2=_build_ap_a2_gate(),
        ap_a2_turnover_band=(0.2083, 0.2885),
    )


def _build_run_status() -> RunStatusBlock:
    return RunStatusBlock(
        total_configs=16,
        oos_folds=8,
        oos_days=28,
        order_type="LIMIT_ONLY",
        economic_execution="TAKER_TAKER",
        min_broker_order_usd=10,
        min_strategy_target_usd=25,
        no_threshold_relaxation=True,
        no_post_pnl_config_addition=True,
    )


def build_sealed_params() -> SealedParams:
    """Build the full, reproducible sealed-parameter record. Pure function of
    the sealed source data files + the literal operator decision — no
    randomness, no wall clock, byte-identical across repeated calls and
    across separate process invocations."""
    return SealedParams(
        universe=_build_universe_seal(),
        spread_census=_build_spread_census_seal(),
        paper_fee=_build_paper_fee_seal(),
        frozen_basis_cap=_build_frozen_basis_cap_seal(),
        cost_scenarios=_build_cost_scenarios(),
        gate_thresholds=_build_gate_thresholds(),
        run_status=_build_run_status(),
    )
