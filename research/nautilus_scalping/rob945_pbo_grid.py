"""ROB-945 (H5) -- PBO/CSCV auxiliary evidence.

Frozen by the second Fable ruling (orch-fable-answer-rob945b-20260718.md,
Q2=A, 2026-07-18): each strategy's frozen 12 configs, each independently
@17-evaluated over the exact frozen full window
``[2025-07-01T00:00Z, 2026-07-01T00:00Z)``, produce one identical UTC-day
net-bps return grid (4-symbol sum per config per day, no-trade day = plain
0.0). This module validates that SEALED grid -- exact 12 configs, one
identical day-key set, all-finite -- and delegates the actual CSCV/PBO
computation to ``research_contracts.honest_offline_gate`` (reused, never
re-implemented) with the frozen ``slices=4``. Any grid defect fails closed
via ``PboGridError``; the caller (scorecard assembly) turns that into
campaign ``incomplete`` with a stable reason. This is auxiliary evidence
only -- never a historical pass/fail gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from rob945_canonical_payload import to_canonical_payload

from research_contracts.canonical_hash import canonical_sha256
from research_contracts.honest_offline_gate import probability_backtest_overfitting

EXPECTED_CONFIG_COUNT = 12
FROZEN_PBO_SLICES = 4
FROZEN_STRATEGIES: tuple[str, ...] = ("S1", "S2")

# Frozen auxiliary-evaluation provenance (Fable Q2=A,
# orch-fable-answer-rob945b-20260718.md): each of the 12 configs is
# independently @17 (primary_stress) evaluated over the exact frozen full
# window, 4-symbol sum per day -- never selected-only/rolling/linear-
# revalue. The caller must supply these values verbatim (fail-closed
# mismatch), and they are bound directly into the auxiliary artifact hash
# so this declaration cannot silently drift from the frozen contract.
FROZEN_PBO_SCENARIO_NAME = "primary_stress"
FROZEN_PBO_COST_BPS = 17.0
FROZEN_PBO_WINDOW_START_ISO = "2025-07-01T00:00:00Z"
FROZEN_PBO_WINDOW_END_ISO = "2026-07-01T00:00:00Z"
FROZEN_PBO_EVALUATION_METHOD = "independent_full_window_per_config_four_symbol_sum"

# Frozen full-window auxiliary evaluation range (Fable Q2=A,
# orch-fable-answer-rob945b-20260718.md): the same
# [2025-07-01T00:00Z, 2026-07-01T00:00Z) window as the primary campaign,
# expressed as the exact 365 UTC exit-day keys every config's grid must
# cover -- no leap day falls inside this range (neither 2025 nor 2026 is a
# leap year), so the count is exactly 365, never 364/366.
_FROZEN_WINDOW_START_DATE = date(2025, 7, 1)
_FROZEN_WINDOW_END_DATE_EXCLUSIVE = date(2026, 7, 1)


def _frozen_day_keys() -> tuple[str, ...]:
    days = []
    current = _FROZEN_WINDOW_START_DATE
    while current < _FROZEN_WINDOW_END_DATE_EXCLUSIVE:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(days)


FROZEN_DAY_KEYS: tuple[str, ...] = _frozen_day_keys()
FROZEN_DAY_COUNT = len(FROZEN_DAY_KEYS)  # 365


def _frozen_config_ids(strategy: str) -> frozenset[str]:
    return frozenset(f"{strategy}-{i:02d}" for i in range(EXPECTED_CONFIG_COUNT))


class PboGridError(ValueError):
    """The sealed 12-config × UTC-day return grid failed validation --
    wrong config count, mismatched day-key sets across configs, or a
    non-finite return. Always fail-closed; never silently padded/dropped."""


@dataclass(frozen=True)
class PboAuxiliaryEvidence:
    strategy: str
    value: float | None
    reason_codes: tuple[str, ...]
    slices: int
    config_count: int
    day_count: int
    artifact_hash: str


def compute_pbo_auxiliary_evidence(
    *,
    strategy: str,
    daily_net_bps_by_config: Mapping[str, Mapping[str, float]],
    slices: int = FROZEN_PBO_SLICES,
    scenario_name: str = FROZEN_PBO_SCENARIO_NAME,
    cost_bps: float = FROZEN_PBO_COST_BPS,
    window_start_iso: str = FROZEN_PBO_WINDOW_START_ISO,
    window_end_iso: str = FROZEN_PBO_WINDOW_END_ISO,
    gap_invalid_days_by_config: Mapping[str, frozenset[str]] | None = None,
) -> PboAuxiliaryEvidence:
    if strategy not in FROZEN_STRATEGIES:
        raise PboGridError(
            f"pbo_grid_strategy: expected strategy in {FROZEN_STRATEGIES!r}, got {strategy!r}"
        )
    if slices != FROZEN_PBO_SLICES:
        raise PboGridError(
            f"pbo_grid_slices: slices is pinned to {FROZEN_PBO_SLICES} exactly, got {slices!r}"
        )
    if (
        scenario_name != FROZEN_PBO_SCENARIO_NAME
        or cost_bps != FROZEN_PBO_COST_BPS
        or window_start_iso != FROZEN_PBO_WINDOW_START_ISO
        or window_end_iso != FROZEN_PBO_WINDOW_END_ISO
    ):
        raise PboGridError(
            "pbo_grid_provenance_mismatch: PBO auxiliary evaluation is frozen to "
            f"scenario={FROZEN_PBO_SCENARIO_NAME!r}, cost_bps={FROZEN_PBO_COST_BPS!r}, "
            f"window=[{FROZEN_PBO_WINDOW_START_ISO!r}, {FROZEN_PBO_WINDOW_END_ISO!r}); "
            f"got scenario={scenario_name!r}, cost_bps={cost_bps!r}, "
            f"window=[{window_start_iso!r}, {window_end_iso!r})"
        )

    # Deep-snapshot the caller-owned nested mappings BEFORE any validation
    # or use -- what gets validated is exactly, and only, what gets used.
    daily_net_bps_by_config = {
        config_id: dict(grid) for config_id, grid in daily_net_bps_by_config.items()
    }
    gap_invalid_days_by_config = {
        config_id: frozenset(days)
        for config_id, days in (gap_invalid_days_by_config or {}).items()
    }

    config_ids = tuple(daily_net_bps_by_config.keys())
    expected_config_ids = _frozen_config_ids(strategy)
    if set(config_ids) != expected_config_ids or len(set(config_ids)) != len(
        config_ids
    ):
        raise PboGridError(
            f"pbo_grid_config_ids: expected exactly the frozen canonical config set "
            f"{sorted(expected_config_ids)!r}, got {sorted(set(config_ids))!r}"
        )

    for config_id, grid in daily_net_bps_by_config.items():
        day_keys = tuple(grid.keys())
        if frozenset(day_keys) != frozenset(FROZEN_DAY_KEYS):
            extra = sorted(frozenset(day_keys) - frozenset(FROZEN_DAY_KEYS))
            missing = sorted(frozenset(FROZEN_DAY_KEYS) - frozenset(day_keys))
            raise PboGridError(
                f"pbo_grid_mismatch: {config_id!r} day-key set does not equal the frozen "
                f"365-day [2025-07-01, 2026-07-01) grid (extra={extra[:3]}, "
                f"missing={missing[:3]})"
            )
        if day_keys != FROZEN_DAY_KEYS:
            raise PboGridError(
                f"pbo_grid_day_order: {config_id!r} day-key mapping order does not match "
                "the frozen canonical chronological order -- a reordered (e.g. reversed) "
                "day mapping is never silently normalized"
            )

    for config_id in gap_invalid_days_by_config:
        if config_id not in expected_config_ids:
            raise PboGridError(
                f"pbo_grid_gap_invalid_unknown_config: {config_id!r} is not one of the "
                f"frozen canonical configs {sorted(expected_config_ids)!r}"
            )

    sorted_days = FROZEN_DAY_KEYS
    # internal alignment is ALWAYS canonical Sx-00..11 order -- never the
    # caller's mapping insertion order, so the statistical computation
    # itself (not just this module's own hash) is order-independent.
    canonical_config_order = sorted(expected_config_ids)
    aligned: dict[str, tuple[float, ...]] = {}
    for config_id in canonical_config_order:
        grid = daily_net_bps_by_config[config_id]
        gap_invalid_days = gap_invalid_days_by_config.get(config_id, frozenset())
        values: list[float] = []
        for day in sorted_days:
            if day in gap_invalid_days:
                raise PboGridError(
                    f"pbo_grid_gap_invalid_day: {config_id!r}/{day} is explicitly marked "
                    "gap-invalid -- a numeric value for this day (even a plausible 0.0) "
                    "is never trusted as evidence"
                )
            raw_value = grid[day]
            if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
                raise PboGridError(
                    f"pbo_grid_non_numeric_return: {config_id!r}/{day} is not a "
                    "plain int/float return"
                )
            value = float(raw_value)
            if value != value or value in (float("inf"), float("-inf")):  # NaN/Inf
                raise PboGridError(
                    f"pbo_grid_non_finite_return: {config_id!r}/{day} is non-finite"
                )
            values.append(value)
        aligned[config_id] = tuple(values)

    result = probability_backtest_overfitting(aligned, slices=slices)

    payload = {
        "strategy": strategy,
        "slices": slices,
        "scenario_name": scenario_name,
        "cost_bps": cost_bps,
        "window_start_iso": window_start_iso,
        "window_end_iso": window_end_iso,
        "evaluation_method": FROZEN_PBO_EVALUATION_METHOD,
        "config_count": len(config_ids),
        "day_count": len(sorted_days),
        "day_keys": list(sorted_days),
        # the RAW sealed evidence itself, not just the derived statistic --
        # two different input grids that happen to yield the same PBO
        # value/reason must still hash differently (mutation-sensitivity).
        "returns_by_config": {
            config_id: list(aligned[config_id]) for config_id in canonical_config_order
        },
        "value": result.value,
        "reason_codes": list(result.reason_codes),
    }
    artifact_hash = canonical_sha256(to_canonical_payload(payload))

    return PboAuxiliaryEvidence(
        strategy=strategy,
        value=result.value,
        reason_codes=result.reason_codes,
        slices=slices,
        config_count=len(config_ids),
        day_count=len(sorted_days),
        artifact_hash=artifact_hash,
    )
