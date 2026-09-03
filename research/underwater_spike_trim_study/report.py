"""Aggregate scanned observations into the tables the answer file carries.

Reads only ``observations.jsonl`` written by ``run.py``; it never touches a
corpus, so a table can be re-cut without re-scanning.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .simulate import cost_basis_view, deltas_vs_hold, normalised, option_values
from .spec import BASES, COST_BASIS_GRID, HORIZONS


def load_observations(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _pct(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "median": None, "mean": None, "p25": None, "p75": None}
    ordered = sorted(values)
    return {
        "n": len(values),
        "median": statistics.median(ordered),
        "mean": statistics.fmean(ordered),
        "p25": ordered[max(0, int(0.25 * (len(ordered) - 1)))],
        "p75": ordered[min(len(ordered) - 1, int(0.75 * (len(ordered) - 1)))],
    }


def _share(flags: Sequence[bool]) -> float | None:
    return None if not flags else sum(1 for flag in flags if flag) / len(flags)


@dataclass(frozen=True)
class Arm:
    """One fully specified way of reading the same scan."""

    resistance_rule: str  # "named" | "strict"
    rebid_strength: str  # "strong" | "moderate_plus"
    basis: str
    horizon: int
    cohort: str  # "event" | "control"


def _passes_resistance(observation: dict[str, Any], rule: str) -> bool:
    if rule == "unfiltered":
        # Controls drawn without matching on overhead structure — the cleaner
        # answer to "is the spike day special?".  For events this is identical
        # to "named", because the scan already gated on that rule.
        return True
    if rule == "named":
        return observation["named_resistance_count"] == 0
    if rule == "strict":
        return observation["resistance_count"] == 0
    raise ValueError(f"unsupported resistance rule {rule!r}")


def _rebuy_price(observation: dict[str, Any], strength: str) -> float | None:
    if strength == "strong":
        return observation["rebuy_price"]
    if strength == "moderate_plus":
        return observation["rebuy_price_moderate_plus"]
    raise ValueError(f"unsupported rebid strength {strength!r}")


def summarise(observations: Iterable[dict[str, Any]], arm: Arm) -> dict[str, Any]:
    """Compute one arm's option comparison, plus the cost-basis grid."""
    hold_returns: list[float] = []
    trim_deltas: list[float] = []
    rebid_deltas: list[float] = []
    rebid_vs_trim: list[float] = []
    fill_flags: list[bool] = []
    available_flags: list[bool] = []
    locked_fill_flags: list[bool] = []
    excluded_not_executable = 0
    considered = 0
    grid: dict[str, dict[str, list[float]]] = {
        f"{int(premium * 100)}pct": {
            "hold_pnl_vs_cost": [],
            "trim_pnl_vs_cost": [],
            "rebid_pnl_vs_cost": [],
            "hold_still_underwater": [],
            "trim_still_underwater": [],
            "rebid_still_underwater": [],
        }
        for premium in COST_BASIS_GRID
    }

    for observation in observations:
        if observation["kind"] != arm.cohort:
            continue
        if not _passes_resistance(observation, arm.resistance_rule):
            continue
        block = observation["forward"].get(f"{arm.basis}:{arm.horizon}")
        if block is None:
            continue
        considered += 1
        if not block["trim_executable"]:
            # KR limit-up lock on the bar the trim would have used.
            excluded_not_executable += 1
            continue

        p0 = block["p0"]
        rebuy_price = _rebuy_price(observation, arm.rebid_strength)
        if rebuy_price is not None and rebuy_price >= p0:
            # The level view is anchored on close[i]; under the next-open basis
            # p0 can gap below a support that was underfoot at the close.  The
            # rebid is then not a rebid at all, so option (3) is unavailable.
            rebuy_price = None
        values = option_values(
            p0=p0,
            pt=block["exit_price"],
            rebuy_price=rebuy_price,
            window_low=block["window_low"],
        )
        returns = normalised(values, p0)
        deltas = deltas_vs_hold(values, p0)
        hold_returns.append(returns["hold"])
        trim_deltas.append(deltas["trim"])
        available_flags.append(values.rebid is not None)
        if values.rebid is not None:
            rebid_deltas.append(deltas["rebid"])
            rebid_vs_trim.append((values.rebid - values.trim) / p0)
            fill_flags.append(values.rebuy_filled)
            key = (
                "fill_used_locked_bar"
                if arm.rebid_strength == "strong"
                else "fill_used_locked_bar_moderate_plus"
            )
            locked_fill_flags.append(bool(block.get(key)))

        for premium in COST_BASIS_GRID:
            view = cost_basis_view(values, p0=p0, cost_premium=premium)
            bucket = grid[f"{int(premium * 100)}pct"]
            for metric in (
                "hold_pnl_vs_cost",
                "trim_pnl_vs_cost",
                "hold_still_underwater",
                "trim_still_underwater",
            ):
                bucket[metric].append(view[metric])
            if view["rebid_pnl_vs_cost"] is not None:
                bucket["rebid_pnl_vs_cost"].append(view["rebid_pnl_vs_cost"])
                bucket["rebid_still_underwater"].append(view["rebid_still_underwater"])

    return {
        "arm": arm.__dict__,
        "considered": considered,
        "excluded_trim_not_executable": excluded_not_executable,
        "n": len(hold_returns),
        "hold_return": _pct(hold_returns),
        "trim_minus_hold": _pct(trim_deltas),
        "trim_beats_hold_rate": _share([d > 0 for d in trim_deltas]),
        "rebid_available_rate": _share(available_flags),
        "rebid_fill_rate": _share(fill_flags),
        "rebid_fill_needed_locked_bar_rate": _share(locked_fill_flags),
        "rebid_minus_hold": _pct(rebid_deltas),
        "rebid_beats_hold_rate": _share([d > 0 for d in rebid_deltas]),
        "rebid_minus_trim": _pct(rebid_vs_trim),
        "cost_basis_grid": {
            premium: {
                "median_hold_pnl_vs_cost": _pct(bucket["hold_pnl_vs_cost"])["median"],
                "median_trim_pnl_vs_cost": _pct(bucket["trim_pnl_vs_cost"])["median"],
                "median_rebid_pnl_vs_cost": _pct(bucket["rebid_pnl_vs_cost"])["median"],
                "hold_still_underwater_rate": _share(
                    [bool(v) for v in bucket["hold_still_underwater"]]
                ),
                "trim_still_underwater_rate": _share(
                    [bool(v) for v in bucket["trim_still_underwater"]]
                ),
                "rebid_still_underwater_rate": _share(
                    [bool(v) for v in bucket["rebid_still_underwater"]]
                ),
            }
            for premium, bucket in grid.items()
        },
    }


def full_report(
    observations: list[dict[str, Any]],
    *,
    bases: Sequence[str] = BASES,
    rules: Sequence[str] = ("named", "strict", "unfiltered"),
    strengths: Sequence[str] = ("strong", "moderate_plus"),
) -> dict[str, Any]:
    out: dict[str, Any] = {"arms": []}
    for rule in rules:
        for strength in strengths:
            for basis in bases:
                for horizon in HORIZONS:
                    for cohort in ("event", "control"):
                        arm = Arm(rule, strength, basis, horizon, cohort)
                        out["arms"].append(summarise(observations, arm))
    return out


def descriptive(observations: list[dict[str, Any]]) -> dict[str, Any]:
    events = [o for o in observations if o["kind"] == "event"]
    controls = [o for o in observations if o["kind"] == "control"]
    strict = [o for o in events if o["resistance_count"] == 0]
    gaps = [o["gap_next_open"] for o in events if o["gap_next_open"] is not None]
    return {
        "events_named_rule": len(events),
        "events_strict_rule": len(strict),
        "controls": len(controls),
        "distinct_event_symbols": len({o["symbol"] for o in events}),
        "event_years": sorted({o["session"][:4] for o in events}),
        "top_symbols": sorted(
            (
                (s, sum(1 for o in events if o["symbol"] == s))
                for s in {o["symbol"] for o in events}
            ),
            key=lambda item: (-item[1], item[0]),
        )[:10],
        "event_limit_locked_up": sum(1 for o in events if o["limit_locked"] == 1),
        "event_next_open_limit_locked_up": sum(
            1 for o in events if o["next_open_limit_locked"] == 1
        ),
        "gap_next_open": _pct(gaps),
        "gap_nonzero_rate": _share([abs(g) > 1e-9 for g in gaps]),
        "median_ret_24h": _pct([o["ret_24h"] for o in events])["median"],
        "median_rsi": _pct([o["rsi"] for o in events])["median"],
        "strong_support_available_rate": _share(
            [o["rebuy_price"] is not None for o in events]
        ),
        "moderate_plus_support_available_rate": _share(
            [o["rebuy_price_moderate_plus"] is not None for o in events]
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    observations = load_observations(args.observations)
    payload = {
        "source": str(args.observations),
        "descriptive": descriptive(observations),
        **full_report(observations),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(payload["descriptive"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
