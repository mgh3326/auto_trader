"""Deterministic 3-source symbol assignment. This table IS the provenance.

`kr_candles_1m` has no provider source column and adding one is forbidden, so
after Stage B there is no way to ask the database which broker a row came from.
This file is the only record. It must therefore be deterministic, reproducible
from inputs alone, and preserved permanently.

Assignment uses sequential Sainte-Lague apportionment over the target counts.
Two properties matter:

* exact counts (246/165/89) — the balanced-makespan split, and
* liquidity interleaving — consecutive ranks go to different sources, so no
  source ends up owning one end of the liquidity distribution. Without this a
  provider-specific quirk would be perfectly confounded with liquidity tier.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

try:
    from .dual_surface import SURFACES, Surface, SurfaceContractError, validate_surface
except ImportError:  # pragma: no cover - direct CLI execution
    from dual_surface import SURFACES, Surface, SurfaceContractError, validate_surface

#: Balanced-makespan split. Derived, not measured — see PROJECTED_* in the
#: Stage A report. Recomputed at gate time from measured page size and pacing.
TARGET_COUNTS: dict[str, int] = {"toss": 246, "kiwoom": 165, "kis": 89}

#: Per-source fetch envelope (spec/code-derived; gate re-measures).
SOURCE_SPEC: dict[str, dict] = {
    "toss": {
        "rows_per_call": 200,
        "pace_seconds": 0.3,
        "rate_basis": "MARKET_DATA_CHART 5 TPS (code)",
    },
    "kiwoom": {
        "rows_per_call": 900,
        "pace_seconds": 2.0,
        "rate_basis": "measured 2026-08-03 (mock)",
    },
    "kis": {
        "rows_per_call": 120,
        "pace_seconds": 0.5,
        "rate_basis": "orch-mock spec estimate",
    },
}

# Kiwoom's measured live pacing is 4x the mock pacing (0.5 s vs 2.0 s).  The
# default dual-pipe split therefore gives live four times as many symbols so
# the two lanes have a comparable projected wall clock.  The generated CSV,
# not this constant, is the immutable run-time input.
DUAL_SURFACE_TARGET_COUNTS: dict[Surface, int] = {"mock": 100, "live": 400}


def assign_surfaces(
    ranks: list[str],
    *,
    target_counts: dict[Surface, int] | None = None,
) -> dict[str, Surface]:
    """Deterministically assign each bulk symbol to exactly one surface."""

    if len(set(ranks)) != len(ranks):
        raise SurfaceContractError("bulk symbol list contains duplicates")
    if not ranks:
        raise SurfaceContractError("bulk symbol list is empty")
    requested = target_counts or DUAL_SURFACE_TARGET_COUNTS
    if set(requested) != set(SURFACES) or any(v < 0 for v in requested.values()):
        raise SurfaceContractError(f"invalid surface target counts: {requested!r}")
    total = sum(requested.values())
    if target_counts is not None and total != len(ranks):
        raise SurfaceContractError(
            f"surface target counts total {total} != symbol count {len(ranks)}"
        )

    if target_counts is None:
        live_count = round(len(ranks) * DUAL_SURFACE_TARGET_COUNTS["live"] / 500)
        counts = {"live": live_count, "mock": len(ranks) - live_count}
    else:
        counts = dict(requested)

    out: dict[str, Surface] = {}
    # Rank order is stable.  Keeping each surface in a contiguous, explicit
    # slice makes accidental reallocation during a run easy to detect in the
    # artifact and avoids a hidden hash/random assignment.
    cursor = 0
    for surface in SURFACES:
        for symbol in ranks[cursor : cursor + counts[surface]]:
            out[symbol] = validate_surface(surface)
        cursor += counts[surface]
    if cursor != len(ranks) or len(out) != len(ranks):
        raise AssertionError("surface assignment did not cover every symbol")
    return out


def write_dual_surface_assignment(
    *,
    top500: Path,
    out_dir: Path,
    overlap_symbols: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    """Write the immutable bulk+overlap split and its manifest.

    Overlap symbols are removed from bulk before assignment and re-added as two
    explicit ``assignment_kind=overlap`` rows.  They are never silently
    assigned twice by the ordinary bulk rule.
    """

    rows = list(csv.DictReader(top500.open(newline="", encoding="utf-8")))
    rows.sort(key=lambda r: int(r["rank"]))
    overlap = tuple(dict.fromkeys(s.strip() for s in overlap_symbols if s.strip()))
    known = {str(r["ticker"]).strip() for r in rows}
    unknown = set(overlap) - known
    if unknown:
        raise SurfaceContractError(
            f"overlap symbols absent from top500: {sorted(unknown)}"
        )
    bulk_rows = [r for r in rows if str(r["ticker"]).strip() not in overlap]
    mapping = assign_surfaces([str(r["ticker"]).strip() for r in bulk_rows])

    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / "kiwoom_surface_split.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["rank", "ticker", "market", "assignment_kind", "surface"],
        )
        writer.writeheader()
        for row in bulk_rows:
            ticker = str(row["ticker"]).strip()
            writer.writerow(
                {
                    "rank": row["rank"],
                    "ticker": ticker,
                    "market": row["market"],
                    "assignment_kind": "bulk",
                    "surface": mapping[ticker],
                }
            )
        for row in rows:
            ticker = str(row["ticker"]).strip()
            if ticker in overlap:
                for surface in SURFACES:
                    writer.writerow(
                        {
                            "rank": row["rank"],
                            "ticker": ticker,
                            "market": row["market"],
                            "assignment_kind": "overlap",
                            "surface": surface,
                        }
                    )

    # Import here to keep the legacy three-source CLI independent of the
    # surface module's JSON/manifest helpers.
    try:
        from .dual_surface import assignment_sha256, validate_surface_rows
    except ImportError:  # pragma: no cover - direct CLI execution
        from dual_surface import assignment_sha256, validate_surface_rows

    split_rows = list(csv.DictReader(out_csv.open(newline="", encoding="utf-8")))
    validate_surface_rows(split_rows)
    manifest = {
        "schema_version": "kiwoom_dual_surface_split.v1",
        "job": "kr-backfill-phase1",
        "split_unit": "symbol",
        "surfaces": list(SURFACES),
        "bulk_assignment_counts": {
            surface: sum(1 for value in mapping.values() if value == surface)
            for surface in SURFACES
        },
        "overlap_symbols": list(overlap),
        "overlap_rows_are_explicit_exception": True,
        "cross_surface_split_prevented": True,
        "assignment_rule": (
            "rank-ordered contiguous split using 1:4 mock:live pacing ratio; "
            "only assignment_kind=overlap may name both surfaces"
        ),
        "split_assignment_csv": str(out_csv),
        "split_assignment_sha256": assignment_sha256(out_csv),
        "top500_sha256": hashlib.sha256(top500.read_bytes()).hexdigest(),
        "assignment_script_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
    }
    out_manifest = out_dir / "kiwoom_surface_split_manifest.json"
    out_manifest.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return out_csv, out_manifest


def assign(ranks: list[str]) -> dict[str, str]:
    counts = dict.fromkeys(TARGET_COUNTS, 0)
    out: dict[str, str] = {}
    for ticker in ranks:
        best = max(
            TARGET_COUNTS,
            # Sainte-Lague divisor; ties broken by source name for determinism.
            key=lambda s: (TARGET_COUNTS[s] / (2 * counts[s] + 1), s),
        )
        out[ticker] = best
        counts[best] += 1
    if counts != TARGET_COUNTS:
        raise AssertionError(f"apportionment mismatch: {counts} != {TARGET_COUNTS}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top500", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument(
        "--dual-surfaces",
        action="store_true",
        help="write the Kiwoom mock/live symbol split instead of the legacy 3-source split",
    )
    ap.add_argument(
        "--overlap-symbol",
        action="append",
        default=[],
        help="explicit cross-surface equality sample symbol; repeat at most twice",
    )
    args = ap.parse_args()

    if args.dual_surfaces:
        if len(args.overlap_symbol) not in {1, 2}:
            ap.error("--dual-surfaces requires one or two --overlap-symbol values")
        csv_path, manifest_path = write_dual_surface_assignment(
            top500=args.top500,
            out_dir=args.out_dir,
            overlap_symbols=tuple(args.overlap_symbol),
        )
        print(
            json.dumps(
                {
                    "split_assignment_csv": str(csv_path),
                    "split_manifest": str(manifest_path),
                },
                indent=2,
            )
        )
        return 0

    rows = list(csv.DictReader(args.top500.open()))
    rows.sort(key=lambda r: int(r["rank"]))
    mapping = assign([r["ticker"] for r in rows])

    out_csv = args.out_dir / "split_assignment.csv"
    with out_csv.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["rank", "ticker", "market", "source"])
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "rank": r["rank"],
                    "ticker": r["ticker"],
                    "market": r["market"],
                    "source": mapping[r["ticker"]],
                }
            )

    counts: dict[str, int] = {}
    tier_mix: dict[str, dict[str, int]] = {}
    for r in rows:
        s = mapping[r["ticker"]]
        counts[s] = counts.get(s, 0) + 1
        tier = f"rank_{((int(r['rank']) - 1) // 100) * 100 + 1}-{((int(r['rank']) - 1) // 100 + 1) * 100}"
        tier_mix.setdefault(s, {}).setdefault(tier, 0)
        tier_mix[s][tier] += 1

    manifest = {
        "job": "kr-backfill-phase1",
        "scope": "1Y",
        "scope_decision": {
            "chosen": "1-year (2025-08-04..2026-08-03 KST)",
            "decided_by": "orch-mock 2026-08-03 ~17:48 KST",
            "alternative_proposed_by_worker": "90-day scope aligned to the live retention policy",
            "alternative_rejected": True,
            "rejection_reason_verbatim": (
                "운영자 기승인(1년×상위500)의 하향이고, 3사 분담으로 1년이 ~10h 라 축소 실익이 없다."
            ),
            "worker_note_on_rejection": (
                "The recorded rejection reason addresses wall-clock cost. The worker's 90-day "
                "proposal was not motivated by wall-clock but by an active TimescaleDB retention "
                "policy on kr_candles_1m (job 1001, drop_after=90 days, scheduled=true), under "
                "which roughly 75% of a 1-year backfill is dropped within ~24h of insertion. "
                "Recorded here per instruction; scope decision belongs to orch-mock."
            ),
        },
        "target_counts": TARGET_COUNTS,
        "assigned_counts": counts,
        "source_spec": SOURCE_SPEC,
        "assignment_rule": (
            "sequential Sainte-Lague apportionment over rank-ordered top500; "
            "ties broken by source name; interleaves liquidity tiers by construction"
        ),
        "liquidity_tier_mix": tier_mix,
        "provenance_warning": (
            "kr_candles_1m has no provider source column and adding one is forbidden. "
            "This file is the ONLY record of which broker served which symbol. "
            "Split is per-symbol, so each symbol's series is internally single-source; "
            "cross-symbol comparisons may mix providers."
        ),
        "split_assignment_csv": str(out_csv),
        "split_assignment_sha256": hashlib.sha256(out_csv.read_bytes()).hexdigest(),
        "top500_sha256": hashlib.sha256(args.top500.read_bytes()).hexdigest(),
        "assignment_script_sha256": hashlib.sha256(
            Path(__file__).resolve().read_bytes()
        ).hexdigest(),
    }
    mpath = args.out_dir / "split_manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in (
                    "assigned_counts",
                    "liquidity_tier_mix",
                    "split_assignment_sha256",
                )
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
