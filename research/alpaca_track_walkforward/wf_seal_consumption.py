"""ROB-1062 H4 — the ONLY module in this package allowed to import
``alpaca_track_seal`` (H2) directly. Every other H4 module reaches sealed
values (cost scenarios, the AP-A2 turnover band, the walk-forward policy
shape, per-family annual stress cost caps, the sealed 16 configs) through the
accessors here — never by importing ``configs``/``params``/``artifact``/
``identity`` directly, and never by re-typing a sealed literal (the SAME
AC18-style discipline ``alpaca_track_signals.seal_consumption`` documents and
enforces for H3).

Config access (the sealed 16 ``ConfigSpec`` rows) and the $2,000/$62.50
equity/base-slot literals are already exposed read-only by H3's OWN
``seal_consumption`` module — this module does not re-expose those (importing
``alpaca_track_signals.seal_consumption`` from elsewhere in H4 for that
purpose is normal H3-consumption, not a violation of the "one gateway" rule,
which is specifically about NOT importing ``alpaca_track_seal.*`` a second
way). This module exposes ONLY the walk-forward-policy-shaped sealed values
H3 has no reason to expose: fold/window day-counts, cost scenario bp values,
the AP-A2 turnover band, the paper fee bp (kept for evidence/provenance
binding ONLY — never as a second, additional deduction; see
``pnl_views.py``'s explicit no-double-fee test), and the per-family annual
stress cost cap percentage.
"""

from __future__ import annotations

import artifact as art
import configs as cfg
import identity as ident
import params as prm

__all__ = [
    "SealDriftError",
    "ap_a2_turnover_band",
    "assert_policy_matches_schedule_constants",
    "cost_scenarios_bp",
    "embargo_days",
    "min_modeled_entries_per_fold",
    "oos_days",
    "oos_folds",
    "paper_fee_bp",
    "primary_cost_scenario",
    "roll_days",
    "stress_annual_cost_cap_pct",
    "train_days",
    "upward_cost_scenario",
]


class SealDriftError(RuntimeError):
    """The freshly-rebuilt H2 seal no longer matches the pinned semantic
    hash — refuse to consume a drifted seal (mirrors
    ``alpaca_track_signals.seal_consumption.SealDriftError``)."""


def _load() -> prm.SealedParams:
    sealed = art.build_sealed_artifact()
    actual = sealed.semantic_hash()
    if actual != art.SEALED_ARTIFACT_SEMANTIC_HASH:
        raise SealDriftError(
            f"H2 seal semantic hash {actual!r} does not match the pinned "
            f"{art.SEALED_ARTIFACT_SEMANTIC_HASH!r} — refusing to consume a "
            "drifted seal"
        )
    return sealed.params


def _policy_component(family: str) -> dict:
    """The ``policy`` ROB-846 identity component for the first sealed config
    of ``family`` — carries the walk-forward window shape
    (``oos_folds``/``oos_days``/``train_days``/``embargo_days``/
    ``roll_days``) and ``min_modeled_entries_per_fold``. Identical across
    both families by ``identity.validate_same_family_components_are_
    identical`` (H2-lock invariant); this reads AP-A1's row only, since both
    are guaranteed equal."""
    params = _load()
    for config in cfg.build_ap_a1_configs():
        return ident.build_components_for_config(config, params)["policy"]
    raise AssertionError("build_ap_a1_configs() must never be empty")


def _frozen_config_component(family: str) -> dict:
    params = _load()
    configs = (
        cfg.build_ap_a1_configs() if family == "AP-A1" else cfg.build_ap_a2_configs()
    )
    for config in configs:
        return ident.build_components_for_config(config, params)["frozen_config"]
    raise AssertionError(f"no sealed config with family={family!r}")


def oos_folds() -> int:
    return int(_policy_component("AP-A1")["walk_forward"]["oos_folds"])


def oos_days() -> int:
    return int(_policy_component("AP-A1")["walk_forward"]["oos_days"])


def train_days() -> int:
    return int(_policy_component("AP-A1")["walk_forward"]["train_days"])


def embargo_days() -> int:
    return int(_policy_component("AP-A1")["walk_forward"]["embargo_days"])


def roll_days() -> int:
    return int(_policy_component("AP-A1")["walk_forward"]["roll_days"])


def min_modeled_entries_per_fold() -> int:
    return int(_policy_component("AP-A1")["min_modeled_entries_per_fold"])


def cost_scenarios_bp() -> dict[str, int]:
    """``{"C50": 50, "C100": 100, "C120": 120, "C150": 150}`` — the FULL,
    already-round-trip-inclusive cost scenario bp values (never re-typed;
    read fresh from the H2 seal every call)."""
    params = _load()
    return dict(params.cost_scenarios.scenarios_bp)


def primary_cost_scenario() -> str:
    return _load().cost_scenarios.primary


def upward_cost_scenario() -> str:
    return _load().cost_scenarios.upward


def ap_a2_turnover_band() -> tuple[float, float]:
    return tuple(_load().gate_thresholds.ap_a2_turnover_band)  # type: ignore[return-value]


def paper_fee_bp() -> float:
    """The sealed measured coin-side round-trip paper fee (25.0bp coin-side;
    ~50bp round trip per the Run A ``50 fee`` component baked into every
    cost scenario bp value). Exposed ONLY for evidence/provenance binding —
    never subtracted a second time from a trade's PnL (the cost scenario bp
    values already include it; see ``pnl_views.py``)."""
    return float(_load().paper_fee.paper_fee_bp)


def stress_annual_cost_cap_pct(family: str) -> float:
    """AP-A1 = 6% NAV, AP-A2 = 18% NAV (Run A SS5/SS11.7/SS12.7), read from
    the sealed ``frozen_config`` identity component — never re-typed."""
    if family not in ("AP-A1", "AP-A2"):
        raise ValueError(f"unknown family {family!r}")
    return float(_frozen_config_component(family)["annual_stress_cost_cap_pct"])


def assert_policy_matches_schedule_constants(
    *,
    oos_folds_const: int,
    oos_days_const: int,
    train_days_const: int,
    embargo_days_const: int,
    roll_days_const: int,
) -> None:
    """Fail closed unless ``fold_schedule.py``'s own module-level constants
    (duplicated there so the schedule generator has no runtime H2 dependency)
    are byte-identical to the SAME shape as sealed in H2's ``policy``
    identity component. Run once at import/test time — a caller mutating
    either side without the other now surfaces immediately instead of the
    two silently drifting apart."""
    sealed = {
        "oos_folds": oos_folds(),
        "oos_days": oos_days(),
        "train_days": train_days(),
        "embargo_days": embargo_days(),
        "roll_days": roll_days(),
    }
    provided = {
        "oos_folds": oos_folds_const,
        "oos_days": oos_days_const,
        "train_days": train_days_const,
        "embargo_days": embargo_days_const,
        "roll_days": roll_days_const,
    }
    if sealed != provided:
        raise SealDriftError(
            f"fold_schedule constants {provided} diverge from the sealed "
            f"policy component {sealed}"
        )
