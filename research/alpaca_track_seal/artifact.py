"""ROB-1060 H2 — the top-level sealed artifact: the exactly-16 config domain
+ the 4 sealed execution parameters, combined into ONE immutable record with
a representation-independent semantic hash. H3-H6 read ONLY this artifact
(AC18) — a hardcoded copy of any value here in a downstream module is a bug.

Pure stdlib + ``canonical_hash``. No app/DB/network import. The registration
CLI (``registry_cli.py``) is a SEPARATE module — it imports this one, never
the reverse, and only it is allowed to (lazily) import ``app.*``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import canonical_hash
import configs as cfg
import params as prm
import source_provenance as sp

__all__ = [
    "ArtifactIntegrityError",
    "SEALED_ARTIFACT_SEMANTIC_HASH",
    "SealedArtifact",
    "SealedSources",
    "build_sealed_artifact",
]

# ROB-1060 H2-lock item 1 (root cause): every sealed VALUE in this package was
# independently re-derived and found correct at seal time (2026-07-25) --
# but the semantic hash summarizing all of them was pinned NOWHERE, so a
# fully green 69-test suite let 17 of 37 mutations silently move it. THIS
# constant is the actual lock: `test_semantic_hash_matches_the_pinned_h2_lock_digest`
# fails the instant any sealed config/param/identity value drifts.
#
# Changing this constant is a DELIBERATE RE-SEAL (a new operator decision or
# a new authority document supersedes the 2026-07-25 seal) -- never a routine
# edit to make a failing test pass. If you are editing this value because a
# test failed, STOP and report: that is exactly the post-hoc relaxation
# ROB-1060 exists to prevent.
SEALED_ARTIFACT_SEMANTIC_HASH = (
    "b0456239ba5893208c30f93c3a58a7f2ecb2a28800cfbdefc150124e771508e0"
)


class ArtifactIntegrityError(Exception):
    """A saved ``SealedArtifact`` JSON failed load-time tamper-evidence
    verification (ROB-1060 H2-lock item 8): a config's recomputed
    ``canonical_hash`` does not match its recorded value, or the artifact's
    recomputed semantic digest does not match an expected digest supplied by
    the caller. Fails closed -- ``load()`` never returns a silently-tampered
    artifact."""


@dataclass(frozen=True)
class SealedSources:
    """Every pinned authority SHA-256 this seal is anchored to (AC17)."""

    preregistration_doc_sha256: str
    params_seal_draft_doc_sha256: str
    universe_map_sha256: str
    spread_census_sha256: str
    basis_analysis_full_sha256: str
    fee_probe_sha256: str
    # H1's actual Binance archive corpus manifest hash is deliberately left
    # unset here: it is produced only by a real, operator-approved one-time
    # network collection (H1 AC25) that has not been run in this
    # environment. Fabricating a plausible-looking hash would be worse than
    # an honest None — H3/H4 populate this once a real corpus manifest
    # exists and is committed.
    dataset_manifest_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "preregistration_doc_sha256": self.preregistration_doc_sha256,
            "params_seal_draft_doc_sha256": self.params_seal_draft_doc_sha256,
            "universe_map_sha256": self.universe_map_sha256,
            "spread_census_sha256": self.spread_census_sha256,
            "basis_analysis_full_sha256": self.basis_analysis_full_sha256,
            "fee_probe_sha256": self.fee_probe_sha256,
            "dataset_manifest_hash": self.dataset_manifest_hash,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SealedSources:
        return cls(**d)


def _build_sources() -> SealedSources:
    return SealedSources(
        preregistration_doc_sha256=sp.PREREGISTRATION_DOC_SHA256,
        params_seal_draft_doc_sha256=sp.PARAMS_SEAL_DRAFT_DOC_SHA256,
        universe_map_sha256=sp.UNIVERSE_MAP_SHA256,
        spread_census_sha256=sp.SPREAD_CENSUS_SHA256,
        basis_analysis_full_sha256=sp.BASIS_ANALYSIS_FULL_SHA256,
        fee_probe_sha256=sp.FEE_PROBE_SHA256,
        dataset_manifest_hash=None,
    )


# --------------------------------------------------------------------------- #
# Explicit, type-preserving (de)serialization for params.SealedParams.        #
# GateCondition.value may be int/float/bool/str/tuple[int,int] — a generic    #
# JSON round-trip would silently turn a tuple into a list and change the      #
# canonical hash, so every tuple-valued field is tagged explicitly.           #
# --------------------------------------------------------------------------- #


def _tag_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return {"__tuple__": [_tag_value(v) for v in value]}
    return value


def _untag_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__tuple__"}:
        return tuple(_untag_value(v) for v in value["__tuple__"])
    return value


def _universe_entry_to_dict(e: prm.SymbolUniverseEntry) -> dict[str, Any]:
    return {
        "alpaca_symbol": e.alpaca_symbol,
        "quote_mode": e.quote_mode,
        "alpaca_first_daily": e.alpaca_first_daily,
        "alpaca_first_daily_is_pit_proxy": e.alpaca_first_daily_is_pit_proxy,
    }


def _universe_entry_from_dict(d: dict[str, Any]) -> prm.SymbolUniverseEntry:
    return prm.SymbolUniverseEntry(**d)


def _spread_entry_to_dict(e: prm.SymbolSpreadEntry) -> dict[str, Any]:
    return {"alpaca_symbol": e.alpaca_symbol, "median_bp": e.median_bp}


def _spread_entry_from_dict(d: dict[str, Any]) -> prm.SymbolSpreadEntry:
    return prm.SymbolSpreadEntry(**d)


def _gate_condition_to_dict(c: prm.GateCondition) -> dict[str, Any]:
    return {
        "metric": c.metric,
        "op": c.op,
        "value": _tag_value(c.value),
        "unit": c.unit,
    }


def _gate_condition_from_dict(d: dict[str, Any]) -> prm.GateCondition:
    return prm.GateCondition(
        metric=d["metric"], op=d["op"], value=_untag_value(d["value"]), unit=d["unit"]
    )


def _params_to_dict(p: prm.SealedParams) -> dict[str, Any]:
    u = p.universe
    sc = p.spread_census
    fee = p.paper_fee
    cap = p.frozen_basis_cap
    cs = p.cost_scenarios
    g = p.gate_thresholds
    r = p.run_status
    return {
        "universe": {
            "source_sha256": u.source_sha256,
            "n_raw_today": u.n_raw_today,
            "excluded_symbols": list(u.excluded_symbols),
            "exclusion_reason": u.exclusion_reason,
            "exclusion_authority": u.exclusion_authority,
            "sealed_effective_n": u.sealed_effective_n,
            "raw_symbols": [_universe_entry_to_dict(e) for e in u.raw_symbols],
            "sealed_symbols": [_universe_entry_to_dict(e) for e in u.sealed_symbols],
        },
        "spread_census": {
            "source_sha256": sc.source_sha256,
            "median_of_medians_all_bp": sc.median_of_medians_all_bp,
            "median_of_medians_eligible_bp": sc.median_of_medians_eligible_bp,
            "cost_heterogeneity_symbols": [
                _spread_entry_to_dict(e) for e in sc.cost_heterogeneity_symbols
            ],
            "note": sc.note,
        },
        "paper_fee": {
            "source_sha256": fee.source_sha256,
            "paper_fee_bp": fee.paper_fee_bp,
            "manual_fee_deduction": fee.manual_fee_deduction,
            "confirmed_end_of_day_posting_format": (
                fee.confirmed_end_of_day_posting_format
            ),
            "provenance_note": fee.provenance_note,
        },
        "frozen_basis_cap": {
            "source_sha256": cap.source_sha256,
            "method": cap.method,
            "proxy_note": cap.proxy_note,
            "raw_cap_bp": dict(cap.raw_cap_bp),
            "sealed_cap_bp": dict(cap.sealed_cap_bp),
        },
        "cost_scenarios": {
            "scenarios_bp": dict(cs.scenarios_bp),
            "primary": cs.primary,
            "upward": cs.upward,
        },
        "gate_thresholds": {
            "min_modeled_entries_per_fold": g.min_modeled_entries_per_fold,
            "fixed_tp": g.fixed_tp,
            "future_tp_min_bp": g.future_tp_min_bp,
            "ap_a1": [_gate_condition_to_dict(c) for c in g.ap_a1],
            "ap_a2": [_gate_condition_to_dict(c) for c in g.ap_a2],
            "ap_a2_turnover_band": list(g.ap_a2_turnover_band),
        },
        "run_status": {
            "total_configs": r.total_configs,
            "oos_folds": r.oos_folds,
            "oos_days": r.oos_days,
            "order_type": r.order_type,
            "economic_execution": r.economic_execution,
            "min_broker_order_usd": r.min_broker_order_usd,
            "min_strategy_target_usd": r.min_strategy_target_usd,
            "no_threshold_relaxation": r.no_threshold_relaxation,
            "no_post_pnl_config_addition": r.no_post_pnl_config_addition,
        },
    }


def _params_from_dict(d: dict[str, Any]) -> prm.SealedParams:
    u = d["universe"]
    sc = d["spread_census"]
    fee = d["paper_fee"]
    cap = d["frozen_basis_cap"]
    cs = d["cost_scenarios"]
    g = d["gate_thresholds"]
    r = d["run_status"]
    return prm.SealedParams(
        universe=prm.EligibleUniverseSeal(
            source_sha256=u["source_sha256"],
            n_raw_today=u["n_raw_today"],
            excluded_symbols=tuple(u["excluded_symbols"]),
            exclusion_reason=u["exclusion_reason"],
            exclusion_authority=u["exclusion_authority"],
            sealed_effective_n=u["sealed_effective_n"],
            raw_symbols=tuple(_universe_entry_from_dict(e) for e in u["raw_symbols"]),
            sealed_symbols=tuple(
                _universe_entry_from_dict(e) for e in u["sealed_symbols"]
            ),
        ),
        spread_census=prm.SpreadCensusSeal(
            source_sha256=sc["source_sha256"],
            median_of_medians_all_bp=sc["median_of_medians_all_bp"],
            median_of_medians_eligible_bp=sc["median_of_medians_eligible_bp"],
            cost_heterogeneity_symbols=tuple(
                _spread_entry_from_dict(e) for e in sc["cost_heterogeneity_symbols"]
            ),
            note=sc["note"],
        ),
        paper_fee=prm.PaperFeeSeal(
            source_sha256=fee["source_sha256"],
            paper_fee_bp=fee["paper_fee_bp"],
            manual_fee_deduction=fee["manual_fee_deduction"],
            confirmed_end_of_day_posting_format=(
                fee["confirmed_end_of_day_posting_format"]
            ),
            provenance_note=fee["provenance_note"],
        ),
        frozen_basis_cap=prm.FrozenBasisCapSeal(
            source_sha256=cap["source_sha256"],
            method=cap["method"],
            proxy_note=cap["proxy_note"],
            raw_cap_bp=dict(cap["raw_cap_bp"]),
            sealed_cap_bp=dict(cap["sealed_cap_bp"]),
        ),
        cost_scenarios=prm.CostScenarios(
            scenarios_bp=dict(cs["scenarios_bp"]),
            primary=cs["primary"],
            upward=cs["upward"],
        ),
        gate_thresholds=prm.GateThresholds(
            min_modeled_entries_per_fold=g["min_modeled_entries_per_fold"],
            fixed_tp=g["fixed_tp"],
            future_tp_min_bp=g["future_tp_min_bp"],
            ap_a1=tuple(_gate_condition_from_dict(c) for c in g["ap_a1"]),
            ap_a2=tuple(_gate_condition_from_dict(c) for c in g["ap_a2"]),
            ap_a2_turnover_band=tuple(g["ap_a2_turnover_band"]),
        ),
        run_status=prm.RunStatusBlock(**r),
    )


@dataclass(frozen=True)
class SealedArtifact:
    """The full H2 seal: 16 configs + params + source provenance.

    Immutable (frozen dataclass, no update method). A "correction" is a new
    ``SealedArtifact`` built from new inputs — this module never mutates an
    existing instance, and ``dataclasses.replace`` (used only by tests to
    prove tamper-detection) still yields a DIFFERENT object, never an
    in-place edit of the original.
    """

    configs: tuple[cfg.ConfigSpec, ...]
    params: prm.SealedParams
    sources: SealedSources = field(default_factory=_build_sources)

    def __post_init__(self) -> None:
        cfg.validate_config_domain(self.configs)
        prm.validate_sealed_universe(self.params.universe)
        prm.validate_cost_scenarios(self.params.cost_scenarios.scenarios_bp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "configs": {
                c.config_id: {
                    "family": c.family,
                    "params": dict(c.params),
                    "canonical_hash": c.canonical_hash,
                }
                for c in self.configs
            },
            "params": _params_to_dict(self.params),
            "sources": self.sources.to_dict(),
        }

    def semantic_hash(self) -> str:
        """Representation-independent SHA-256 of the full seal content, via
        the same typed canonical AST authority every config/manifest hash in
        this repo uses. Byte-identical across repeated calls, separate
        process invocations, and PYTHONHASHSEED — configs are keyed by
        config_id (a dict, canonically key-sorted) so top-level ordering
        cannot perturb it."""
        return canonical_hash.canonical_sha256(self.to_dict())

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))

    @classmethod
    def load(
        cls, path: str | Path, *, expected_semantic_hash: str | None = None
    ) -> SealedArtifact:
        """Load a saved seal, fail-closed on any tamper evidence (ROB-1060
        H2-lock item 8).

        Two independent checks, both mandatory-on-detection:

        1. Every config's ``canonical_hash`` is recomputed from its own
           ``params`` and compared to the recorded value -- catches a config
           whose ``params`` were edited in the JSON while its
           ``canonical_hash`` was left stale (or vice versa).
        2. If ``expected_semantic_hash`` is supplied (e.g. this module's
           pinned ``SEALED_ARTIFACT_SEMANTIC_HASH``), the freshly-loaded
           artifact's own ``semantic_hash()`` must match it -- catches ANY
           tampered sealed value (gate threshold, universe membership, cost
           scenario, ...) even when every per-config ``canonical_hash``
           happens to still be internally consistent.
        """
        d = json.loads(Path(path).read_text())
        configs = []
        for config_id, entry in d["configs"].items():
            recomputed_hash = cfg.canonical_config_hash(
                config_id, entry["family"], entry["params"]
            )
            if recomputed_hash != entry["canonical_hash"]:
                raise ArtifactIntegrityError(
                    f"config {config_id!r}: recorded canonical_hash "
                    f"{entry['canonical_hash']!r} does not match recomputed "
                    f"{recomputed_hash!r} -- params were tampered while the "
                    "recorded hash was left stale"
                )
            configs.append(
                cfg.ConfigSpec(
                    config_id=config_id,
                    family=entry["family"],
                    params=entry["params"],
                    canonical_hash=entry["canonical_hash"],
                )
            )
        # Restore canonical (AP-A1 before AP-A2, 00..07 ascending) order —
        # the source dict has no guaranteed order, but the CONFIG DOMAIN
        # itself is order-independent for hashing (dict-keyed); sorting here
        # is purely for a stable, readable .configs tuple.
        configs = tuple(sorted(configs, key=lambda c: c.config_id))
        artifact = cls(
            configs=configs,
            params=_params_from_dict(d["params"]),
            sources=SealedSources.from_dict(d["sources"]),
        )
        if expected_semantic_hash is not None:
            recomputed_semantic = artifact.semantic_hash()
            if recomputed_semantic != expected_semantic_hash:
                raise ArtifactIntegrityError(
                    f"semantic hash mismatch: expected "
                    f"{expected_semantic_hash!r}, recomputed "
                    f"{recomputed_semantic!r} -- the loaded artifact's "
                    "content diverges from the expected sealed record"
                )
        return artifact


def build_sealed_artifact() -> SealedArtifact:
    """Build the full H2 seal. Pure, parameterless, reproducible."""
    return SealedArtifact(
        configs=cfg.build_all_configs(), params=prm.build_sealed_params()
    )
