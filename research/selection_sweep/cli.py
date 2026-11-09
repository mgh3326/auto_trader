"""Manual commands. No scheduler, DB, broker or credential.

select-us   write the seeded US name list (listing facts only, no prices)
fetch-us    one Yahoo request per listed name into a local cache
explore     exploration window only (decisions and exits <= 2023-12-31)
validate    the one validation look, only for cells carried forward
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from . import data, spec
from .panel import build_panel
from .sweep import carry_forward, cell_metrics, run_market

PKG = Path(__file__).resolve().parent
RESULTS = PKG / "results"


def code_digest() -> str:
    """sha256 over the sorted (name, bytes) of every module of this package."""
    h = hashlib.sha256()
    for path in sorted(PKG.glob("*.py")):
        h.update(path.name.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _results(args) -> Path:
    given = getattr(args, "results_dir", None)
    return Path(given) if given else RESULTS


def _write_once(path: Path, text: str) -> None:
    if path.exists():
        raise FileExistsError(f"{path} exists; refusing to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write-once: an existing ledger or result file is never overwritten."""
    if path.exists():
        raise FileExistsError(
            f"{path} exists; refusing to overwrite recorded trials "
            "(reproduce with --results-dir <fresh directory>)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, sort_keys=True, allow_nan=False) + "\n" for r in rows)
    )


def _clean(obj):
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _panels(args, end: str) -> dict:
    sample = json.loads((PKG / "data" / "us_sample.json").read_text())["symbols"]
    frames, bench, us_info = data.load_us(Path(args.us_cache), sample, end=end)
    panels = {
        "us": (
            build_panel("us", frames, bench, start=spec.HISTORY_START["us"], end=end),
            us_info,
        )
    }
    frames, bench, cr_info = data.load_crypto(
        Path(args.crypto_root), Path(args.crypto_pit), end=end
    )
    panels["crypto"] = (
        build_panel(
            "crypto", frames, bench, start=spec.HISTORY_START["crypto"], end=end
        ),
        cr_info,
    )
    return panels


def _run(args, window: tuple[str, str], end: str, only: set[str] | None):
    rows, info, daily = [], {}, {}
    for market, (panel, market_info) in _panels(args, end).items():
        series = run_market(panel, window)
        terminated = int(panel.terminated.sum())
        info[market] = {
            **market_info,
            "symbols_in_panel": len(panel.symbols),
            "terminated_series": terminated,
            "calendar_sessions": panel.n,
        }
        for cell in spec.grid():
            if cell.market != market or (only is not None and cell.key not in only):
                continue
            metrics = cell_metrics(cell, series[cell.horizon])
            rows.append(
                {
                    "cell": cell.key,
                    "market": cell.market,
                    "ranker": cell.ranker,
                    "horizon": cell.horizon,
                    "side": cell.side,
                    "metrics": _clean(metrics),
                }
            )
        for h, hs in series.items():
            daily[f"{market}_h{h}_dates"] = hs.dates.values.astype("datetime64[D]")
            daily[f"{market}_h{h}_ew"] = hs.ew
            daily[f"{market}_h{h}_bench"] = hs.bench
            daily[f"{market}_h{h}_rand_long"] = hs.rand_long
            daily[f"{market}_h{h}_rand_short"] = hs.rand_short
            for k in spec.RANKERS:
                daily[f"{market}_h{h}_{k}_long"] = hs.long[k]
                daily[f"{market}_h{h}_{k}_short"] = hs.short[k]
    return rows, info, daily


def cmd_select_us(args) -> None:
    symbols = data.select_us_sample(Path(args.pit_csv))
    out = PKG / "data" / "us_sample.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "seed": spec.US_SAMPLE_SEED,
                "size": spec.US_SAMPLE_SIZE,
                "listed_by": spec.US_LISTED_BY,
                "pit_csv_sha256": data.sha256_file(Path(args.pit_csv)),
                "benchmark": spec.BENCHMARK["us"],
                "symbols": symbols,
            },
            indent=1,
        )
        + "\n"
    )
    print(f"{len(symbols)} symbols -> {out}")


def cmd_fetch_us(args) -> None:
    sample = json.loads((PKG / "data" / "us_sample.json").read_text())["symbols"]
    counts = data.fetch_us(sample, Path(args.cache), start=args.start, end=args.end)
    print(json.dumps(counts, sort_keys=True))


def cmd_explore(args) -> None:
    rows, info, daily = _run(args, spec.EXPLORATION, spec.EXPLORATION[1], None)
    carried = carry_forward(rows)
    for row in rows:
        row["phase"] = "exploration"
        row["manifest_version"] = spec.MANIFEST_VERSION
        row["carried"] = row["cell"] in carried
        row["carry_rank"] = carried.index(row["cell"]) + 1 if row["carried"] else None
    extras = [
        {
            "phase": "extra",
            "cell": "data_inputs_exploration",
            "metrics": _clean(info),
            "note": "input coverage of the exploration run, not a grid cell",
        }
    ]
    for row in extras:
        row["manifest_version"] = spec.MANIFEST_VERSION
    superseded = []
    if args.supersede_ledger:  # earlier manifest version's rows stay, flagged
        for line in Path(args.supersede_ledger).read_text().splitlines():
            old = json.loads(line)
            old["phase"] = f"superseded_{old['phase']}"
            old["manifest_version"] = args.supersede_version
            superseded.append(old)
    _write_jsonl(_results(args) / "trials_ledger.jsonl", rows + extras + superseded)
    _write_once(
        _results(args) / "carry_forward.json",
        json.dumps(
            {
                "code_sha256": code_digest(),
                "rule": {
                    "min_newey_west_t": spec.CARRY_MIN_T,
                    "max_random_p": spec.CARRY_MAX_RANDOM_P,
                    "min_positive_years": spec.CARRY_MIN_POSITIVE_YEARS,
                    "cap": spec.CARRY_CAP,
                },
                "grid_cells": len(rows),
                "carried": carried,
            },
            indent=1,
        )
        + "\n",
    )
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / "exploration_daily.npz", **daily)
    print(f"grid={len(rows)} carried={len(carried)} -> {_results(args)}")


def cmd_validate(args) -> None:
    carried = json.loads((_results(args) / "carry_forward.json").read_text())["carried"]
    if not carried:
        print("no cell carried forward: no validation read, no test look")
        return
    rows, info, daily = _run(args, spec.VALIDATION, spec.VALIDATION[1], set(carried))
    m = len(carried)
    for row in rows:
        mm = row["metrics"]
        row["phase"] = "validation"
        row["manifest_version"] = spec.MANIFEST_VERSION
        row["bonferroni_alpha"] = spec.ALPHA / m
        row["pass_primary"] = bool(
            mm["net_primary_mean"] > 0 and mm["random_p"] <= spec.ALPHA / m
        )
    rows.append(
        {
            "phase": "extra",
            "cell": "data_inputs_validation",
            "metrics": _clean(info),
            "note": "input coverage of the validation run, not a grid cell",
        }
    )
    _write_jsonl(_results(args) / "validation_results.jsonl", rows)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / "validation_daily.npz", **daily)
    print(f"validated {m} cells -> {_results(args)}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select-us")
    s.add_argument("--pit-csv", required=True)
    s.set_defaults(fn=cmd_select_us)
    s = sub.add_parser("fetch-us")
    s.add_argument("--cache", required=True)
    s.add_argument("--start", default=spec.HISTORY_START["us"])
    s.add_argument("--end", required=True, help="exclusive end date for Yahoo period2")
    s.set_defaults(fn=cmd_fetch_us)
    for name, fn in (("explore", cmd_explore), ("validate", cmd_validate)):
        s = sub.add_parser(name)
        s.add_argument("--us-cache", required=True)
        s.add_argument("--crypto-root", required=True)
        s.add_argument("--crypto-pit", required=True)
        s.add_argument("--out-dir", required=True)
        s.add_argument("--results-dir", help="default: the committed results directory")
        s.set_defaults(fn=fn)
        if name == "explore":
            s.add_argument("--supersede-ledger")
            s.add_argument("--supersede-version", type=int, default=1)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
