"""ROB-946 (H6, ROB-940) — pure, generic campaign-identity builder.

This module is deliberately a GENERIC, manifest-injected mechanism, not the
production 24-row campaign itself. ROB-946 §9 (H3 parallel-dependency
handling): H3 owns the actual production 24-row manifest and the real
strategy source once it merges; this module must not fabricate a competing
"production" copy of that data or a temporary hash authority. Callers (tests
today, the future captain pipeline once H3 lands) supply:

  * the 24 ``CampaignConfigRow``s (config_id/params/hypothesis) — literal data
    that is already frozen/approved (Fable Q1=A, 2026-07-17) independent of
    any strategy source code;
  * a ``StrategySourceProvenance`` per strategy (S1/S2) carrying the ACTUAL
    strategy source text, whose SHA-256 is recomputed here and compared
    against any caller-asserted ``expected_source_sha256`` — a stale/tampered
    assertion fails closed before any identity is built (same discipline as
    ROB-941's archive checksum re-verification);
  * the full committed ROB-941 corpus manifest dict, verified byte-for-byte
    against its expected ``content_hash`` before use as the ``dataset_manifest``
    identity component.

Every other identity component (universe/pit/cost/policy/benchmark/mdd, and
per-strategy frozen_config) is built from already-frozen ROB-940/941 constants
(``rob941_frozen_scope``, ``rob940_cost_model``) — literal, not tunable, and
identical for both strategies except the two documented per-strategy
constants sets (frozen_config) and the two strategy-specific components
(strategy, code). ``params`` is the ONLY component that legitimately varies
row-to-row within one strategy's 12 configs.

No DB/network/app import — pure stdlib plus the existing
``research_contracts.canonical_hash`` authority (via the local ``canonical_hash``
shim) and sibling rob941_*/rob940_* modules, all already pure themselves.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import canonical_hash
import rob941_frozen_scope as frozen
from rob940_cost_model import (
    COST_SCENARIOS,
    FEE_ENTRY_BPS,
    FEE_EXIT_BPS,
    FEE_ROUND_TRIP_BPS,
    MIN_TP_DISTANCE_BPS,
)

__all__ = [
    "CampaignConfigRow",
    "CampaignExperimentSpec",
    "CampaignIdentityError",
    "CampaignRowCountError",
    "CampaignRowIdError",
    "DatasetManifestHashMismatchError",
    "StrategySourceMismatchError",
    "StrategySourceProvenance",
    "build_benchmark_component",
    "build_campaign_experiment_specs",
    "build_code_component",
    "build_cost_component",
    "build_dataset_manifest_component",
    "build_frozen_config_component",
    "build_mdd_component",
    "build_params_component",
    "build_pit_component",
    "build_policy_component",
    "build_strategy_component",
    "build_universe_component",
    "validate_campaign_rows",
    "validate_same_strategy_components_are_identical",
]

_CONFIG_ID_PATTERN = re.compile(r"^S(?P<slug>[12])-(?P<idx>\d{2})$")
_CONFIGS_PER_STRATEGY = 12
_EXPECTED_TOTAL = 24
_EXPECTED_SLUGS = ("S1", "S2")
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


class CampaignIdentityError(ValueError):
    """Base error for campaign-identity construction/validation failures."""


class CampaignRowCountError(CampaignIdentityError):
    """The campaign does not have exactly 24 rows (12 per strategy)."""


class CampaignRowIdError(CampaignIdentityError):
    """A config_id is malformed, duplicated, or the per-strategy set is wrong."""


class StrategySourceMismatchError(CampaignIdentityError):
    """A strategy source's asserted hash is stale, or S1/S2 sources collide."""


class DatasetManifestHashMismatchError(CampaignIdentityError):
    """The injected dataset manifest does not hash to the expected value."""


@dataclass(frozen=True)
class CampaignConfigRow:
    """One of the 24 approved (strategy, parameter-set) rows.

    Deliberately carries no symbol field — there is no per-row universe
    override hook; ``build_universe_component`` always returns the single
    frozen 4-symbol universe regardless of which row is being built.
    """

    config_id: str
    params: dict[str, Any]
    hypothesis: str

    def strategy_slug(self) -> str:
        match = _CONFIG_ID_PATTERN.match(self.config_id)
        if not match:
            raise CampaignRowIdError(
                f"config_id {self.config_id!r} does not match the required "
                "S<1|2>-NN pattern"
            )
        return f"S{match.group('slug')}"


@dataclass(frozen=True)
class StrategySourceProvenance:
    """Actual strategy source text + its verified (not merely asserted) SHA-256.

    ``expected_source_sha256`` is an optional caller-supplied pin (e.g. from a
    prior registration); if given, it is compared against the SHA-256 actually
    computed from ``source_text`` and a mismatch fails closed rather than
    silently trusting the assertion — the same "trust but verify" discipline
    ROB-941 uses for archive checksums.
    """

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
            raise StrategySourceMismatchError(
                f"{self.strategy_key}: source SHA-256 mismatch (expected "
                f"{self.expected_source_sha256}, actual {actual}) — stale or "
                "tampered strategy source"
            )
        return actual


@dataclass(frozen=True)
class CampaignExperimentSpec:
    """One row's full identity, ready to feed an app-side
    ``StrategyExperimentIdentity`` (this module never imports that schema —
    pure research side stays app-free)."""

    strategy_key: str
    strategy_version: str
    hypothesis: str
    components: dict[str, Any]  # the 11 ROB-846 IDENTITY_COMPONENTS


def validate_campaign_rows(rows: list[CampaignConfigRow]) -> None:
    """Fail closed unless ``rows`` is exactly 12 S1 + 12 S2 rows, no dup/missing."""
    if len(rows) != _EXPECTED_TOTAL:
        raise CampaignRowCountError(
            f"expected exactly {_EXPECTED_TOTAL} campaign rows, got {len(rows)}"
        )
    ids = [row.config_id for row in rows]
    if len(set(ids)) != len(ids):
        duplicates = sorted(
            {config_id for config_id in ids if ids.count(config_id) > 1}
        )
        raise CampaignRowIdError(f"duplicate config_id(s): {duplicates}")

    by_slug: dict[str, list[str]] = {}
    for row in rows:
        by_slug.setdefault(row.strategy_slug(), []).append(row.config_id)

    if sorted(by_slug) != sorted(_EXPECTED_SLUGS):
        raise CampaignRowIdError(
            f"expected exactly strategy slugs {sorted(_EXPECTED_SLUGS)}, got "
            f"{sorted(by_slug)}"
        )
    for slug in _EXPECTED_SLUGS:
        expected_ids = [f"{slug}-{i:02d}" for i in range(_CONFIGS_PER_STRATEGY)]
        if sorted(by_slug[slug]) != expected_ids:
            raise CampaignRowIdError(
                f"{slug}: expected exactly {expected_ids}, got {sorted(by_slug[slug])}"
            )


def build_dataset_manifest_component(
    dataset_manifest: dict[str, Any], *, expected_content_hash: str
) -> dict[str, Any]:
    """Verify the injected ROB-941 manifest hashes to the expected value.

    Uses the SAME canonical-hash authority ``CorpusManifest.content_hash()``
    uses, over the SAME dict shape, so this reproduces the identical digest —
    no wrapper/locator, the full manifest content is the identity component.
    """
    actual = canonical_hash.canonical_sha256(dataset_manifest)
    if actual != expected_content_hash:
        raise DatasetManifestHashMismatchError(
            f"dataset manifest content_hash mismatch (expected "
            f"{expected_content_hash}, actual {actual}) — missing/tampered corpus"
        )
    return dataset_manifest


def build_universe_component() -> dict[str, Any]:
    """The single frozen 4-symbol universe + eligibility — no override hook."""
    return {
        "symbols": list(frozen.UNIVERSE),
        "eligibility": [
            {"symbol": symbol, **frozen.eligibility(symbol)}
            for symbol in frozen.UNIVERSE
        ],
    }


def build_pit_component() -> dict[str, Any]:
    return {
        "window_start_iso": frozen.WINDOW_START_ISO,
        "window_end_iso": frozen.WINDOW_END_ISO,
        "funding_pit_policy": (
            "entry_gate_last_known_rate_interval;pnl_realized_crossing;no_lookahead"
        ),
    }


def build_cost_component() -> dict[str, Any]:
    return {
        "fee_entry_bps": FEE_ENTRY_BPS,
        "fee_exit_bps": FEE_EXIT_BPS,
        "fee_round_trip_bps": FEE_ROUND_TRIP_BPS,
        "scenarios": {
            scenario.name: scenario.all_in_bps for scenario in COST_SCENARIOS
        },
        "primary_scenario": "primary_stress",
        "min_tp_distance_bps": MIN_TP_DISTANCE_BPS,
    }


def build_policy_component() -> dict[str, Any]:
    return {
        "walk_forward": {
            "train_days": 120,
            "embargo_hours": 3,
            "oos_days": 28,
            "roll_days": 28,
            "min_folds": 6,
        },
        "selection": {
            "authority": (
                "equal_weight_mean_eligible_symbol_train_net_expectancy_bps_at_"
                "primary_stress"
            ),
            "min_symbol_train_trades": 5,
            "min_eligible_symbols": 2,
            "insufficient_symbol_evidence_reason": "insufficient_symbol_evidence",
            "insufficient_eligible_symbols_reason": "rejected:insufficient_train_evidence",
            "tie_break": ["profit_factor", "config_id_ascending"],
            "pooled_expectancy_role": "report_only_not_selection_authority",
        },
        "pass_thresholds_primary_stress": {
            "expectancy_bps_min": 5,
            "profit_factor_min": 1.15,
            "positive_folds_min": 4,
            "month_concentration_max": 0.50,
        },
        "pass_thresholds_upward_stress": {"expectancy_bps_min": 0},
        "pbo": {"slices": 4, "role": "auxiliary_report_only_not_pass_gate"},
        "no_broker_execution": True,
        "data_gap_reject_reason": "rejected:data_gap_in_position",
    }


def build_benchmark_component() -> dict[str, Any]:
    """Explicit canonical sentinel — NOT a historical pass authority (ROB-946 §4)."""
    return {"kind": "none_explicit_sentinel", "role": "not_a_historical_pass_authority"}


def build_mdd_component() -> dict[str, Any]:
    """Historical MDD is report-only — never a hard gate (ROB-946 §4)."""
    return {
        "definition": "peak_to_trough_R_multiples",
        "historical_role": "report_only",
        "hard_gate": False,
    }


def build_frozen_config_component(strategy_slug: str) -> dict[str, Any]:
    """Per-strategy FIXED (non-tunable) constants — identical for all 12 configs
    of that strategy, different between S1 and S2."""
    if strategy_slug == "S1":
        return {
            "timeframe": "15m",
            "atr_period": 20,
            "a_t_range_pct": [0.20, 1.20],
            "chase_guard_atr_mult": 0.50,
            "timeout_bars": 12,
            "cooldown_bars": 4,
            "daily_max_entries": 3,
            "daily_max_consecutive_stop_outs": 2,
            "daily_max_loss_r": -2.0,
        }
    if strategy_slug == "S2":
        return {
            "timeframe": "5m",
            "mad_window": 288,
            "er_window": 48,
            "shock_return_floor_pct": 0.60,
            "target_cap_pct": 1.20,
            "cost_multiple_floor": 4,
            "timeout_bars": 6,
            "cooldown_bars": 12,
            "daily_max_entries": 3,
            "daily_max_consecutive_stop_outs": 2,
            "daily_max_loss_r": -2.0,
        }
    raise CampaignIdentityError(f"unknown strategy slug {strategy_slug!r}")


def build_strategy_component(
    slug: str, source: StrategySourceProvenance
) -> dict[str, Any]:
    return {
        "slug": slug,
        "strategy_key": source.strategy_key,
        "strategy_version": source.strategy_version,
    }


def build_code_component(source: StrategySourceProvenance) -> dict[str, Any]:
    return {"source_sha256": source.verified_source_sha256()}


def build_params_component(row: CampaignConfigRow) -> dict[str, Any]:
    """The ONLY component allowed to vary within a strategy's 12 rows."""
    return {"config_id": row.config_id, "hypothesis": row.hypothesis, **row.params}


def validate_same_strategy_components_are_identical(
    specs: list[CampaignExperimentSpec],
) -> None:
    """Fail closed unless every non-``params`` component is identical across
    all rows sharing a ``strategy_key`` — the ROB-946 §3 params-only-variation
    contract."""
    by_strategy: dict[str, list[CampaignExperimentSpec]] = {}
    for spec in specs:
        by_strategy.setdefault(spec.strategy_key, []).append(spec)
    for strategy_key, group in by_strategy.items():
        first = group[0]
        for other in group[1:]:
            for name in _NON_PARAMS_COMPONENT_NAMES:
                if first.components[name] != other.components[name]:
                    raise CampaignIdentityError(
                        f"strategy_key {strategy_key!r}: component {name!r} "
                        f"differs between {first.components['params']['config_id']!r} "
                        f"and {other.components['params']['config_id']!r} — only "
                        "'params' may vary within one strategy's rows"
                    )


def build_campaign_experiment_specs(
    rows: list[CampaignConfigRow],
    *,
    sources: dict[str, StrategySourceProvenance],
    dataset_manifest: dict[str, Any],
    dataset_manifest_expected_hash: str,
) -> tuple[CampaignExperimentSpec, ...]:
    """Build all 24 identity specs from injected rows/sources/manifest.

    Fail-closed order: row-shape (count/id pattern) -> strategy-source
    presence/distinctness -> dataset-manifest hash -> per-row component
    assembly. Nothing is written or hashed into an experiment_id here — that
    is the app-side bridge's job, using this module's output as pure data.
    """
    validate_campaign_rows(rows)

    if set(sources) != set(_EXPECTED_SLUGS):
        raise CampaignIdentityError(
            f"expected strategy sources for exactly {sorted(_EXPECTED_SLUGS)}, "
            f"got {sorted(sources)}"
        )
    s1_source, s2_source = sources["S1"], sources["S2"]
    if s1_source.strategy_key == s2_source.strategy_key:
        raise StrategySourceMismatchError("S1 and S2 must have different strategy_key")
    if s1_source.strategy_version == s2_source.strategy_version:
        raise StrategySourceMismatchError(
            "S1 and S2 must have different strategy_version"
        )
    if s1_source.verified_source_sha256() == s2_source.verified_source_sha256():
        raise StrategySourceMismatchError(
            "S1 and S2 must have different source SHA-256 (source collision)"
        )

    dataset_component = build_dataset_manifest_component(
        dataset_manifest, expected_content_hash=dataset_manifest_expected_hash
    )
    universe_component = build_universe_component()
    pit_component = build_pit_component()
    cost_component = build_cost_component()
    policy_component = build_policy_component()
    benchmark_component = build_benchmark_component()
    mdd_component = build_mdd_component()
    frozen_config_by_slug = {
        slug: build_frozen_config_component(slug) for slug in _EXPECTED_SLUGS
    }

    specs = []
    for row in rows:
        slug = row.strategy_slug()
        source = sources[slug]
        specs.append(
            CampaignExperimentSpec(
                strategy_key=source.strategy_key,
                strategy_version=source.strategy_version,
                hypothesis=row.hypothesis,
                components={
                    "strategy": build_strategy_component(slug, source),
                    "code": build_code_component(source),
                    "params": build_params_component(row),
                    "dataset_manifest": dataset_component,
                    "universe": universe_component,
                    "pit": pit_component,
                    "frozen_config": frozen_config_by_slug[slug],
                    "policy": policy_component,
                    "benchmark": benchmark_component,
                    "cost": cost_component,
                    "mdd": mdd_component,
                },
            )
        )

    validate_same_strategy_components_are_identical(specs)
    return tuple(specs)
