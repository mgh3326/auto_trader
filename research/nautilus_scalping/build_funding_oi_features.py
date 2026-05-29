#!/usr/bin/env python3
"""ROB-356 — Binance USD-M funding + OI PIT feature builder (operator-gated).

Builds the deterministic, point-in-time, survivorship-safe funding+OI feature artifact
described in ROB-356, on top of the ROB-349/353 PIT universe manifest. This is feature
construction + coverage validation ONLY — not a strategy, not a backtest.

Read-only PUBLIC data only (``data.binance.vision/futures/um`` monthly fundingRate +
daily metrics). The network RUN is operator-gated behind ``--run``; CI exercises only
the pure helpers (parsers in ``funding_oi_archive``, features in ``funding_oi_features``,
and ``classify_feature_readiness`` below). ``--out`` gates ALL disk writes; with it, the
per-symbol feature tables and the coverage summary are written under the gitignored
``results/`` artifact root (``AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`` if set) — never raw
market data. No keys, no orders, no Demo, no live/mainnet trading endpoint, no scheduler, no DB.

See the durable report at ``docs/runbooks/rob-356-funding-oi-pit-features.md``.

Usage (operator):
    cd research/nautilus_scalping
    # representative coverage verdict in minutes (stratified across delisted+active):
    uv run --no-project python build_funding_oi_features.py --run --stratified 40 --out
    # resume a crashed/partial RUN (skips already-built symbols):
    uv run --no-project python build_funding_oi_features.py --run --stratified 40 --out --resume
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

import funding_oi_features as fof
import pit_universe as pu
from funding_oi_archive import parse_funding_csv, parse_metrics_csv

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BASE = "https://data.binance.vision"

_ctx = ssl.create_default_context()


# --------------------------------------------------------------------------- #
# Deterministic readiness verdict (pure; unit-tested)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReadinessThresholds:
    """Explicit gate for "is this artifact ready for a bounded event backtest?"."""

    min_usable_symbols: int = 20
    min_delisted_usable: int = 3
    min_oi_window_rows: int = 500
    max_missingness: float = 0.05


@dataclass(frozen=True)
class ReadinessInputs:
    """Aggregate coverage signals measured by the RUN (or supplied by a test)."""

    usable_symbols: int
    delisted_usable: int
    all_delisted_survivorship_ok: bool
    min_oi_window_rows: int  # smallest per-symbol usable OI row count
    max_missingness: float  # worst per-symbol day-level gap fraction (1 - oi_coverage)


def classify_feature_readiness(
    inp: ReadinessInputs, thr: ReadinessThresholds | None = None
) -> tuple[str, list[str]]:
    """``("ready", [])`` iff every threshold is met and delisted survivorship is proven;
    otherwise ``("needs_more_data", [reasons...])``. Below threshold -> do NOT open a
    backtest issue."""
    thr = thr or ReadinessThresholds()
    reasons: list[str] = []
    if inp.usable_symbols < thr.min_usable_symbols:
        reasons.append(
            f"usable_symbols {inp.usable_symbols} < {thr.min_usable_symbols}"
        )
    if not inp.all_delisted_survivorship_ok:
        reasons.append("delisted survivorship not proven (archive ends before delist)")
    if inp.delisted_usable < thr.min_delisted_usable:
        reasons.append(
            f"delisted_usable {inp.delisted_usable} < {thr.min_delisted_usable}"
        )
    if inp.min_oi_window_rows < thr.min_oi_window_rows:
        reasons.append(
            f"min oi_window rows {inp.min_oi_window_rows} < {thr.min_oi_window_rows}"
        )
    if inp.max_missingness > thr.max_missingness:
        reasons.append(
            f"missingness {inp.max_missingness:.3f} > {thr.max_missingness:.3f}"
        )
    return ("ready" if not reasons else "needs_more_data"), reasons


# --------------------------------------------------------------------------- #
# Coverage helpers (pure)
# --------------------------------------------------------------------------- #
def expected_days(first: str | None, last: str | None) -> int:
    """Inclusive day span between two ``YYYY-MM-DD`` tokens (0 if either missing)."""
    if not first or not last:
        return 0
    d0 = datetime.strptime(first, "%Y-%m-%d").replace(tzinfo=UTC)
    d1 = datetime.strptime(last, "%Y-%m-%d").replace(tzinfo=UTC)
    return (d1 - d0).days + 1


def _day_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat()


def survivorship_ok(last_day: str | None, delisted_at: int | None) -> bool:
    """A delisted symbol's metrics archive must reach its delisting day."""
    if delisted_at is None:
        return last_day is not None
    if last_day is None:
        return False
    return last_day >= _day_str(delisted_at - 1)  # delisted_at is exclusive


# --------------------------------------------------------------------------- #
# ROB-362 — stratified subset + resumable progress checkpoint (pure; unit-tested)
#   The full 552-symbol RUN is ~4.6h, non-resumable. A stratified subset returns a
#   representative coverage verdict in minutes; the progress jsonl lets a crashed RUN
#   resume by skipping already-built symbols. Both are pure/path-based for testability.
# --------------------------------------------------------------------------- #
def _evenly_spaced(seq: list, k: int) -> list:
    """``k`` distinct, evenly-spread elements of ``seq`` (deterministic). Spreading
    beats first-``k`` truncation, which alphabetically clusters short-lived symbols."""
    if k <= 0:
        return []
    if k >= len(seq):
        return list(seq)
    return [seq[i * len(seq) // k] for i in range(k)]


def stratified_sample(listings: list, n: int) -> list:
    """Pick ~``n`` listings stratified across delisted (survivorship-critical, scarce)
    and active strata, evenly spread within each. ``n<=0`` or ``n>=total`` -> all.
    The scarce delisted stratum gets at least half of ``n``; active backfills the rest."""
    if n <= 0 or n >= len(listings):
        return list(listings)
    delisted = sorted(
        (x for x in listings if x.delisted_at is not None), key=lambda x: x.symbol
    )
    active = sorted(
        (x for x in listings if x.delisted_at is None), key=lambda x: x.symbol
    )
    n_del = min(len(delisted), max(1, n // 2)) if delisted else 0
    n_act = min(len(active), n - n_del)
    n_del = min(len(delisted), n - n_act)  # backfill delisted if active ran short
    return _evenly_spaced(delisted, n_del) + _evenly_spaced(active, n_act)


def load_progress(path) -> list[dict]:
    """Per-symbol coverage stats persisted by a prior RUN (one JSON object per line).
    Tolerates a torn final line from a crash mid-write (that symbol is simply re-run)."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # partial trailing line from an interrupted write -> skip
    return out


def append_progress(path, record: dict) -> None:
    """Atomically append one completed symbol's stats as a JSON line (resume checkpoint)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def completed_symbols(records: list[dict]) -> set:
    """Symbols already built (skip their network fetch on resume)."""
    return {r["symbol"] for r in records if "symbol" in r}


# --------------------------------------------------------------------------- #
# Network helpers (operator RUN only)
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": "rob356-feature-builder/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:  # noqa: S310
        return r.read()


def list_periods(prefix: str, pat: str) -> list[str]:
    out: list[str] = []
    marker = ""
    while True:
        q: dict[str, str] = {"prefix": prefix}
        if marker:
            q["marker"] = marker
        xml = _get(S3 + "?" + urllib.parse.urlencode(q)).decode()
        out.extend(re.findall(pat, xml))
        keys = re.findall(r"<Key>([^<]+)</Key>", xml)
        if "<IsTruncated>true</IsTruncated>" in xml and keys:
            marker = keys[-1]
        else:
            break
    return sorted(set(out))


def _csv_in_zip(url: str) -> str:
    z = zipfile.ZipFile(io.BytesIO(_get(url)))
    return z.read(z.namelist()[0]).decode()


def fetch_funding(sym: str) -> list:
    months = list_periods(
        f"data/futures/um/monthly/fundingRate/{sym}/",
        re.escape(sym) + r"-fundingRate-(\d{4}-\d{2})\.zip",
    )
    rows: list = []
    for m in months:
        rows += parse_funding_csv(
            _csv_in_zip(
                f"{BASE}/data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{m}.zip"
            )
        )
    return sorted(rows, key=lambda r: r.calc_time)


def fetch_metrics(sym: str) -> tuple[list, list[str]]:
    days = list_periods(
        f"data/futures/um/daily/metrics/{sym}/",
        re.escape(sym) + r"-metrics-(\d{4}-\d{2}-\d{2})\.zip",
    )
    rows: list = []
    for d in days:
        rows += parse_metrics_csv(
            _csv_in_zip(
                f"{BASE}/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{d}.zip"
            )
        )
    return sorted(rows, key=lambda r: r.create_time), days


def _write_feature_csv(path, feats: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [f.name for f in fields(fof.FeatureRow)]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for row in feats:
            w.writerow(asdict(row))


def build_symbol(listing: pu.SymbolListing) -> dict:
    """Operator RUN per symbol: fetch, build PIT features, return coverage stats."""
    sym = listing.symbol
    funding = fetch_funding(sym)
    metrics, metric_days = fetch_metrics(sym)
    feats = fof.build_features(sym, funding, metrics, delisted_at=listing.delisted_at)
    first_day = metric_days[0] if metric_days else None
    last_day = metric_days[-1] if metric_days else None
    exp = expected_days(first_day, last_day)
    oi_cov = round(len(metric_days) / exp, 3) if exp else 0.0
    return {
        "symbol": sym,
        "status": listing.status,
        "delisted": listing.delisted_at is not None,
        "feature_rows": len(feats),
        "oi_first_day": first_day,
        "oi_last_day": last_day,
        "oi_days_present": len(metric_days),
        "oi_coverage": oi_cov,
        "missingness": round(1.0 - oi_cov, 3),
        "survivorship_ok": survivorship_ok(last_day, listing.delisted_at),
        "_feats": feats,
    }


def summarize(stats: list[dict], thr: ReadinessThresholds) -> dict:
    usable = [s for s in stats if s["feature_rows"] >= thr.min_oi_window_rows]
    delisted_attempted = [s for s in stats if s["delisted"]]
    inp = ReadinessInputs(
        usable_symbols=len(usable),
        delisted_usable=sum(1 for s in usable if s["delisted"]),
        all_delisted_survivorship_ok=all(
            s["survivorship_ok"] for s in delisted_attempted
        ),
        min_oi_window_rows=min((s["feature_rows"] for s in usable), default=0),
        max_missingness=max((s["missingness"] for s in usable), default=1.0),
    )
    verdict, reasons = classify_feature_readiness(inp, thr)
    return {
        "source": "data.binance.vision/futures/um",
        "schema_version": "funding_oi_coverage.v1",
        "symbols_attempted": len(stats),
        "active_attempted": sum(1 for s in stats if not s["delisted"]),
        "delisted_attempted": len(delisted_attempted),
        "thresholds": asdict(thr),
        "readiness_inputs": asdict(inp),
        "verdict": verdict,
        "verdict_reasons": reasons,
        "per_symbol": [{k: v for k, v in s.items() if k != "_feats"} for s in stats],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ROB-356 funding+OI PIT feature builder (read-only)."
    )
    parser.add_argument(
        "--run", action="store_true", help="Perform the network RUN (operator-gated)."
    )
    parser.add_argument(
        "--manifest",
        default="data_manifests/pit_universe.v1.json",
        help="PIT universe manifest path.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap symbols to the first N of the manifest (0 = all). See --stratified.",
    )
    parser.add_argument(
        "--stratified",
        type=int,
        default=0,
        help=(
            "Pick N symbols stratified across the delisted + active strata (a representative "
            "coverage verdict in minutes instead of the ~4.6h full RUN). Takes precedence "
            "over --limit. 0 = off."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "With --out: skip symbols already in the progress checkpoint and append new "
            "ones (resume a crashed/partial RUN). Without it the RUN starts a fresh slate."
        ),
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=10,
        help=(
            "With --out: rewrite the coverage summary every N processed symbols so a crash "
            "leaves an inspectable partial verdict (0 = only at completion)."
        ),
    )
    parser.add_argument(
        "--out",
        action="store_true",
        help=(
            "Write all artifacts to the gitignored results/ root: per-symbol feature CSVs "
            "+ the coverage summary JSON. Without --out the RUN only probes and prints the "
            "verdict (writes nothing)."
        ),
    )
    args = parser.parse_args(argv)

    manifest = pu.PITManifest.load(args.manifest).strict_usdt_perp()
    syms = list(manifest.listings)
    if args.stratified:
        syms = stratified_sample(syms, args.stratified)
    elif args.limit:
        syms = syms[: args.limit]

    if not args.run:
        print(
            f"DRY: would build funding+OI PIT features for {len(syms)} perp symbols "
            f"from {args.manifest}.\nAdd --run to perform the read-only network RUN (prints "
            f"the readiness verdict, writes nothing). Add --out to also write per-symbol "
            f"feature CSVs + the coverage summary JSON to the gitignored results/ root."
        )
        return 0

    from artifact_paths import resolve_artifact_path

    thr = ReadinessThresholds()
    feat_dir = resolve_artifact_path("discovery", "rob356", "features")
    summary_path = resolve_artifact_path(
        "discovery", "rob356", "funding_oi_coverage.v1.json"
    )
    progress_path = resolve_artifact_path("discovery", "rob356", "_progress.jsonl")

    def _write_summary(s: dict) -> None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(s, indent=2))

    # Resume: skip symbols a prior RUN already built; otherwise start a fresh slate.
    done: dict[str, dict] = {}
    if args.out and args.resume:
        done = {r["symbol"]: r for r in load_progress(progress_path)}
        if done:
            print(f"resume: {len(done)} symbols already built; skipping their fetch")
    elif args.out:
        Path(progress_path).unlink(missing_ok=True)

    stats: list[dict] = []
    for i, listing in enumerate(syms, 1):
        if listing.symbol in done:
            stats.append(done[listing.symbol])
            print(f"  [{i}/{len(syms)}] {listing.symbol:14} resumed (cached)")
            continue
        try:
            s = build_symbol(listing)
        except Exception as exc:  # noqa: BLE001 — coverage probe must be resilient per-symbol
            print(f"  [{i}/{len(syms)}] {listing.symbol}: SKIP ({type(exc).__name__})")
            continue
        feats = s.pop("_feats")
        if args.out:  # --out gates ALL disk writes (feature CSVs + progress + summary)
            _write_feature_csv(feat_dir / f"{s['symbol']}.csv", feats)
            append_progress(progress_path, s)  # checkpoint this symbol for resume
        stats.append(s)
        print(
            f"  [{i}/{len(syms)}] {s['symbol']:14} rows={s['feature_rows']:6} "
            f"oi={s['oi_first_day']}..{s['oi_last_day']} cov={s['oi_coverage']} "
            f"surv={int(s['survivorship_ok'])}"
        )
        if args.out and args.checkpoint_every > 0 and i % args.checkpoint_every == 0:
            _write_summary(summarize(stats, thr))  # partial verdict survives a crash

    summary = summarize(stats, thr)
    print("\nverdict:", summary["verdict"])
    for r in summary["verdict_reasons"]:
        print("  -", r)

    if args.out:
        _write_summary(summary)
        print(
            "\nsummary + per-symbol feature CSVs written (gitignored): "
            f"{summary_path.parent}"
        )
    else:
        print("\n(no --out: nothing written to disk)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
