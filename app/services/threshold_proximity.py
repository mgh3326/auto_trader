"""Near-miss tagging for discovery gate rejections (ROB-1315 §7-3).

Recording only. Nothing in this module changes a gate verdict: a candidate
that failed still failed, and the tag is attached *after* the comparison the
gate already made. The point is that a reject at 45.03 against a 45 ceiling
and a reject at 78 against the same ceiling are currently indistinguishable
in the negative-class cohort, so the "we rejected correctly" claim cannot be
tested at the margin.

Two live cases motivated it (2026-08-21 US session):

* CIEN — RSI 45.03 against ``screen.rsi_max`` 45 (subsequent MFE +19.39%)
* RDDT — honest upside 39.93% against a 40% floor (subsequent MFE +20.09%)

Band semantics
--------------
The retro says "within ±1% of the threshold". Both cited gates are measured
in percent-like units (RSI points, percentage points of upside), so the band
is applied as **±1.0 in the gate's own unit** — the reading that admits both
cited cases. The relative distance is recorded alongside it
(``miss_pct_of_threshold``) so a scorer can re-filter on the stricter
relative reading without re-deriving anything.

Pure: stdlib only. No DB, no network, no broker, no clock, no policy read.
"""

from __future__ import annotations

from typing import Any, Literal

# ±1.0 in the gate's own unit. A constant, not a tunable: widening it would
# change which rejects enter the cohort mid-collection.
PROXIMITY_BAND = 1.0

# ``max``   — the gate wanted observed <= threshold (e.g. RSI <= 45)
# ``min``   — the gate wanted observed >= threshold (e.g. upside >= 40)
Comparison = Literal["max", "min"]

TAG = "threshold_proximity"


def _miss(observed: float, threshold: float, comparison: Comparison) -> float:
    """How far the observation fell on the failing side. Never negative here."""

    if comparison == "max":
        return observed - threshold
    return threshold - observed


def evaluate(
    *,
    gate: str,
    metric: str,
    observed: float | None,
    threshold: float,
    comparison: Comparison,
    unit: str,
) -> dict[str, Any] | None:
    """Describe a failed numeric comparison, or ``None`` when it is untaggable.

    ``None`` is returned when the observation is missing (a missing value is
    not a near miss — it is an absent measurement) or when the observation did
    not actually fail the comparison. Both cases are silence by construction:
    this function never invents a value to tag.
    """

    if observed is None:
        return None
    try:
        observed_value = float(observed)
        threshold_value = float(threshold)
    except (TypeError, ValueError):
        return None
    miss = _miss(observed_value, threshold_value, comparison)
    if miss <= 0:
        # It passed. A passing candidate has no rejection to tag.
        return None
    relative = (miss / abs(threshold_value) * 100) if threshold_value else None
    return {
        "gate": gate,
        "metric": metric,
        "comparison": comparison,
        "threshold": threshold_value,
        "observed": round(observed_value, 6),
        "miss": round(miss, 6),
        "miss_unit": unit,
        "miss_pct_of_threshold": None if relative is None else round(relative, 6),
        "band": PROXIMITY_BAND,
        "band_unit": unit,
        "band_semantics": "absolute_units_of_the_gate_metric",
        "within_band": miss <= PROXIMITY_BAND,
        "verdict_changed": False,
    }


def near_miss(**kwargs: Any) -> dict[str, Any] | None:
    """``evaluate`` filtered to the ±band cohort. Returns ``None`` otherwise."""

    tag = evaluate(**kwargs)
    if tag is None or not tag["within_band"]:
        return None
    return tag


def build_forecast_tag(
    tags: list[dict[str, Any]],
    *,
    market: str,
    symbol: str,
) -> dict[str, Any] | None:
    """Build the ``forecast_target`` fragment for a negative-class record.

    The caller merges this into its own ``forecast_save(...)`` call with
    ``decision_bucket='deferred_no_action'`` (ROB-1283). This module does not
    write; returning ``None`` means there is nothing worth recording.
    """

    within = [tag for tag in tags if tag.get("within_band")]
    if not within:
        return None
    closest = min(within, key=lambda tag: float(tag["miss"]))
    return {
        TAG: {
            "experiment": "rob-1315-threshold-proximity",
            "market": market,
            "symbol": symbol,
            "band": PROXIMITY_BAND,
            "band_semantics": "absolute_units_of_the_gate_metric",
            "closest_gate": closest["gate"],
            "closest_miss": closest["miss"],
            "gates": within,
            "gate_verdict_changed": False,
            "promote": False,
            "live_gate_impact": False,
        },
        "decision_bucket_hint": "deferred_no_action",
    }


__all__ = [
    "PROXIMITY_BAND",
    "TAG",
    "build_forecast_tag",
    "evaluate",
    "near_miss",
]
