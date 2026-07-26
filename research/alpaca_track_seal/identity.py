"""ROB-1060 H2 — pure ROB-846 identity-component builders.

Mirrors ``research/nautilus_scalping/rob946_campaign_identity.py``'s
precedent: this module builds the 11 ROB-846 ``IDENTITY_COMPONENTS`` as PLAIN
DATA. It never imports ``app.*`` or ``StrategyExperimentIdentity`` — the
app-side registration CLI (``registry_cli.py``) is the only place that
constructs the real Pydantic identity, using this module's output as pure
input.

Design note on the ``code`` component (open question, see H2 completion
report): H3 (the actual DATS state-machine / WCM-B ranking signal engine,
ROB-1061) does not exist yet — there is no real implementation source to
hash. ``default_formula_provenance`` seals the FORMULA SPECIFICATION text
verbatim from the Run A preregistration (SS11.3 / SS12.2-12.3) as the ``code``
identity for now. This is deliberately NOT a stand-in for H3's eventual
implementation hash — once H3 merges, registering with its real source text
derives a DIFFERENT ``code_hash`` and therefore a different experiment_id,
which must be registered as a NEW experiment superseding this one (same
strategy_key, ROB-846 lineage), never an in-place edit.

Pure stdlib + ``canonical_hash``/``research_contracts``. No app/DB/network
import.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import configs as cfg
import params as prm

__all__ = [
    "SourceMismatchError",
    "StrategySourceProvenance",
    "build_components_for_config",
    "default_formula_provenance",
    "validate_same_family_components_are_identical",
]

_NON_PARAMS_COMPONENT_NAMES = (
    "strategy",
    "code",
    "dataset_manifest",
    "universe",
    "pit",
    "frozen_config",
    "policy",
    "benchmark",
    "cost",
    "mdd",
)

# Verbatim formula specifications from the Run A preregistration authority
# doc (SHA-256 67b5d3c2...), SS11.3 (AP-A1) / SS12.2-12.3 (AP-A2).
_AP_A1_FORMULA_SPEC = (
    "R[i,m,t] = C[i,t]/C[i,t-m] - 1; D[i,f,s,t] = EMA_f(C)/EMA_s(C) - 1. "
    "entry: flat AND D >= +0.005 AND R > 0. "
    "exit: long AND (D <= -0.005 OR R <= 0). "
    "hysteresis: -0.005 < D < +0.005 keeps existing state."
)
_AP_A2_FORMULA_SPEC = (
    "Score[i,L,t] = C[i,t]/C[i,t-L] - 1, descending sort, ties by symbol "
    "ascending. order: (1) held with Score<=0 OR rank>k+buffer -> exit "
    "queued; (2) exits submitted first; (3) after exits, from remaining "
    "cash buy Score>0 unheld symbols in rank order; (4) stop once k held; "
    "(5) held symbols with rank<=k+buffer AND Score>0 -> no trade (hold); "
    "(6) fewer than k positive-Score symbols -> remainder stays cash. no "
    "restoration of existing holdings' weight (rank buffer suppresses "
    "turnover)."
)


class SourceMismatchError(ValueError):
    """A formula/source provenance's asserted hash is stale."""


@dataclass(frozen=True)
class StrategySourceProvenance:
    """Actual formula/source text + its verified (not merely asserted)
    SHA-256. Mirrors ``rob946_campaign_identity.StrategySourceProvenance``."""

    strategy_key: str
    strategy_version: str
    source_text: str
    expected_source_sha256: str | None = None

    def verified_source_sha256(self) -> str:
        actual = hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
        if (
            self.expected_source_sha256 is not None
            and actual != self.expected_source_sha256
        ):
            raise SourceMismatchError(
                f"{self.strategy_key}: source SHA-256 mismatch (expected "
                f"{self.expected_source_sha256}, actual {actual}) — stale or "
                "tampered source"
            )
        return actual


def default_formula_provenance(family: str) -> StrategySourceProvenance:
    """The formula-specification provenance sealed at H2 time (see module
    docstring — NOT H3's eventual implementation source)."""
    if family == "AP-A1":
        return StrategySourceProvenance(
            strategy_key="alpaca_track_ap_a1",
            strategy_version="2026-07-25-h2-formula-seal-v1",
            source_text=_AP_A1_FORMULA_SPEC,
        )
    if family == "AP-A2":
        return StrategySourceProvenance(
            strategy_key="alpaca_track_ap_a2",
            strategy_version="2026-07-25-h2-formula-seal-v1",
            source_text=_AP_A2_FORMULA_SPEC,
        )
    raise ValueError(f"unknown family {family!r}")


def _build_strategy_component(
    config: cfg.ConfigSpec, source: StrategySourceProvenance
) -> dict[str, Any]:
    return {
        "family": config.family,
        "strategy_key": source.strategy_key,
        "strategy_version": source.strategy_version,
    }


def _build_code_component(source: StrategySourceProvenance) -> dict[str, Any]:
    return {
        "kind": "formula_specification_not_implementation",
        "source_sha256": source.verified_source_sha256(),
    }


def _build_params_component(config: cfg.ConfigSpec) -> dict[str, Any]:
    """The ONLY component allowed to vary within one family's 8 configs."""
    return {"config_id": config.config_id, **config.params}


def _build_dataset_manifest_component() -> dict[str, Any]:
    return {
        "status": "pending_h1_corpus_manifest",
        "note": (
            "H1's real Binance archive corpus (a separate, operator-approved "
            "one-time network collection) has not been produced in this "
            "environment. This is an explicit, honest pending fact — never a "
            "fabricated hash standing in for missing data."
        ),
    }


def _build_universe_component(seal: prm.SealedParams) -> dict[str, Any]:
    u = seal.universe
    return {
        "sealed_effective_n": u.sealed_effective_n,
        "excluded_symbols": list(u.excluded_symbols),
        "exclusion_reason": u.exclusion_reason,
        "exclusion_authority": u.exclusion_authority,
        "symbols": sorted(e.alpaca_symbol for e in u.sealed_symbols),
        "quote_mode": {e.alpaca_symbol: e.quote_mode for e in u.sealed_symbols},
    }


def _build_pit_component(seal: prm.SealedParams) -> dict[str, Any]:
    return {
        "warmup_days": 180,
        "n_t_minimum": 18,
        "alpaca_first_daily_is_pit_proxy": True,
        "universe_source_sha256": seal.universe.source_sha256,
    }


def _build_frozen_config_component(
    config: cfg.ConfigSpec, seal: prm.SealedParams
) -> dict[str, Any]:
    if config.family == "AP-A1":
        return {
            "evaluation": "daily_00:05_utc",
            "initial_equity_usd": 2000,
            "base_slot_usd": 62.50,
            "min_strategy_target_usd": seal.run_status.min_strategy_target_usd,
            "gross_ev_floor_bp": 180,
            "e120_floor_bp": 60,
            "annual_stress_cost_cap_pct": 6,
            "fixed_tp": seal.gate_thresholds.fixed_tp,
            "future_tp_min_bp": seal.gate_thresholds.future_tp_min_bp,
        }
    if config.family == "AP-A2":
        return {
            "evaluation": "weekly_monday_00:05_utc",
            "initial_equity_usd": 2000,
            "min_strategy_target_usd": seal.run_status.min_strategy_target_usd,
            "gross_ev_floor_bp": 200,
            "e120_floor_bp": 80,
            "annual_stress_cost_cap_pct": 18,
            "turnover_band": list(seal.gate_thresholds.ap_a2_turnover_band),
            "fixed_tp": seal.gate_thresholds.fixed_tp,
            "future_tp_min_bp": seal.gate_thresholds.future_tp_min_bp,
        }
    raise ValueError(f"unknown family {config.family!r}")


def _build_policy_component(seal: prm.SealedParams) -> dict[str, Any]:
    return {
        "walk_forward": {
            "oos_folds": seal.run_status.oos_folds,
            "oos_days": seal.run_status.oos_days,
            "train_days": 365,
            "embargo_days": 7,
            "roll_days": 28,
        },
        "min_modeled_entries_per_fold": seal.gate_thresholds.min_modeled_entries_per_fold,
        "dry_count_gate": "pnl_blind_dry_count_before_oos_unmask",
        "no_threshold_relaxation": seal.run_status.no_threshold_relaxation,
        "no_post_pnl_config_addition": seal.run_status.no_post_pnl_config_addition,
        "order_type": seal.run_status.order_type,
        "economic_execution": seal.run_status.economic_execution,
        "min_broker_order_usd": seal.run_status.min_broker_order_usd,
    }


def _build_benchmark_component() -> dict[str, Any]:
    return {
        "benchmarks": ["BTC", "ETH", "cash", "pit_equal_weight"],
        "role": "reported_alongside_not_a_pass_authority",
    }


def _build_cost_component(seal: prm.SealedParams) -> dict[str, Any]:
    cs = seal.cost_scenarios
    return {
        "scenarios_bp": dict(cs.scenarios_bp),
        "primary": cs.primary,
        "upward": cs.upward,
    }


def _build_mdd_component() -> dict[str, Any]:
    return {
        "definition": "peak_to_trough_oos_window",
        "max_oos_dd_pct": 20,
        "hard_gate": True,
    }


def build_components_for_config(
    config: cfg.ConfigSpec, seal: prm.SealedParams
) -> dict[str, Any]:
    """Build the full 11-component ROB-846 identity dict for one config."""
    source = default_formula_provenance(config.family)
    return {
        "strategy": _build_strategy_component(config, source),
        "code": _build_code_component(source),
        "params": _build_params_component(config),
        "dataset_manifest": _build_dataset_manifest_component(),
        "universe": _build_universe_component(seal),
        "pit": _build_pit_component(seal),
        "frozen_config": _build_frozen_config_component(config, seal),
        "policy": _build_policy_component(seal),
        "benchmark": _build_benchmark_component(),
        "cost": _build_cost_component(seal),
        "mdd": _build_mdd_component(),
    }


def validate_same_family_components_are_identical(
    config_and_components: list[tuple[cfg.ConfigSpec, dict[str, Any]]],
) -> None:
    """Fail closed unless every non-``params`` component is identical across
    all configs sharing a family (ROB-946 SS3 pattern: only ``params`` may vary
    within one strategy's configs)."""
    by_family: dict[str, list[tuple[cfg.ConfigSpec, dict[str, Any]]]] = {}
    for config, components in config_and_components:
        by_family.setdefault(config.family, []).append((config, components))
    for family, group in by_family.items():
        first_config, first = group[0]
        for other_config, other in group[1:]:
            for name in _NON_PARAMS_COMPONENT_NAMES:
                if first[name] != other[name]:
                    raise ValueError(
                        f"family {family!r}: component {name!r} differs between "
                        f"{first_config.config_id!r} and {other_config.config_id!r} "
                        "— only 'params' may vary within one family's configs"
                    )
