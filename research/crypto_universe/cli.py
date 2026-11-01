"""Manual CLI for crypto-universe-v1 (no scheduler, public data only).

    # 1) today's exchange-info snapshot (append-only, idempotent per UTC day)
    uv run python -m research.crypto_universe.cli snapshot --root DIR

    # 2) rebuild candles + PIT universe from the archives (uses the latest snapshot)
    uv run python -m research.crypto_universe.cli build --root DIR [--cache-dir DIR]

    # 3) query the PIT universe on a day
    uv run python -m research.crypto_universe.cli pit --root DIR \\
        --venue binance_usdt_spot --date 2021-06-01
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from . import build as b
from . import upbit_public as up
from .public_http import make_urllib_fetch, with_retries
from .snapshot import take_snapshot
from .universe import VENUES, OutsideCoverageError, PitUniverse


def _log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%H:%M:%SZ")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


def cmd_snapshot(args: argparse.Namespace) -> int:
    for outcome in take_snapshot(Path(args.root), with_retries(make_urllib_fetch())):
        print(f"{outcome.action}\t{outcome.source}\t{outcome.sha256}\t{outcome.path}")
    return 0


def _upbit_probe_candidates(
    args: argparse.Namespace, results: dict[str, b.VenueResult]
) -> list[str]:
    """Explicit list, else the known delisted markets plus ``KRW-<base>`` for
    every Binance base asset (ASCII alphanumeric) in this build."""
    if args.upbit_delisted_probes:
        return args.upbit_delisted_probes.split(",")
    candidates = set(up.DEFAULT_DELISTED_PROBES)
    for listing in results.get("binance_usdt_spot", b.EMPTY_RESULT).listings:
        base = listing.base_asset
        if base.isascii() and base.isalnum():
            candidates.add(f"KRW-{base}")
    return sorted(candidates)


def cmd_build(args: argparse.Namespace) -> int:
    root = Path(args.root)
    fetch = with_retries(make_urllib_fetch())
    venues = args.venues.split(",")
    unknown = set(venues) - set(VENUES)
    if unknown:
        raise SystemExit(f"unknown venues {sorted(unknown)}")
    cache = Path(args.cache_dir) if args.cache_dir else root / "cache"
    outputs = ("candles", "universe")
    if any((root / name).exists() for name in outputs) and not args.replace:
        raise SystemExit(
            "candles/ or universe/ already exist under --root; pass --replace "
            "to rebuild them (cache/ and snapshots/ are kept)"
        )
    # Build into a staging folder; the old outputs are replaced only after
    # the whole build succeeded, so a failed rebuild keeps the last good one.
    staging = root / ".build-staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    results: dict[str, b.VenueResult] = {}
    inputs: dict[str, object] = {}
    if "binance_usdt_spot" in venues:
        info, ref = b.resolve_snapshot(root, "binance_spot_exchange_info")
        inputs["binance_spot_exchange_info"] = ref
        results["binance_usdt_spot"] = b.build_binance(
            fetch,
            staging,
            info,
            snapshot_date=date.fromisoformat(ref["snapshot_date"]),
            cache_dir=cache,
            workers=args.workers,
            symbols=args.symbols.split(",") if args.symbols else None,
            progress=_log,
        )
    if "upbit_krw" in venues:
        markets, ref = b.resolve_snapshot(root, "upbit_market_all")
        inputs["upbit_market_all"] = ref
        end = (
            date.fromisoformat(args.upbit_coverage_end)
            if args.upbit_coverage_end
            else results["binance_usdt_spot"].coverage_end
            if "binance_usdt_spot" in results
            else None
        )
        if end is None:
            raise SystemExit("--upbit-coverage-end is required without binance")
        results["upbit_krw"] = b.build_upbit(
            fetch,
            staging,
            markets,
            coverage_end=end,
            delisted_candidates=_upbit_probe_candidates(args, results),
            markets=args.upbit_markets.split(",") if args.upbit_markets else None,
            progress=_log,
        )
    coverage = b.write_outputs(staging, results, inputs)
    promote_staging(root, staging, outputs)
    print(b.render_coverage_md(coverage))
    return 0


def promote_staging(root: Path, staging: Path, names: tuple[str, ...]) -> None:
    """Swap the staged outputs in; the previous ones are removed only after."""
    previous = root / ".build-previous"
    shutil.rmtree(previous, ignore_errors=True)
    previous.mkdir()
    for name in names:
        if (root / name).exists():
            (root / name).rename(previous / name)
    for name in names:
        if (staging / name).exists():
            (staging / name).rename(root / name)
    shutil.rmtree(previous)
    shutil.rmtree(staging)


def cmd_pit(args: argparse.Namespace) -> int:
    root = Path(args.root)
    manifest = json.loads((root / "universe" / "manifest.json").read_text())
    table = manifest["tables"].get(args.venue)
    if table is None:
        print(f"refused: no {args.venue} table in manifest", file=sys.stderr)
        return 2
    text = (root / table["path"]).read_text(encoding="utf-8")
    if hashlib.sha256(text.encode()).hexdigest() != table["sha256"]:
        print(
            f"refused: {table['path']} does not match manifest sha256", file=sys.stderr
        )
        return 2
    universe = PitUniverse.load(
        root / table["path"],
        date.fromisoformat(table["coverage_start"]),
        date.fromisoformat(table["coverage_end"]),
    )
    try:
        symbols = universe.universe_as_of(date.fromisoformat(args.date))
    except OutsideCoverageError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for symbol in sorted(symbols):
        print(symbol)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crypto_universe")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="write today's exchange-info snapshot")
    snap.add_argument("--root", required=True)
    snap.set_defaults(func=cmd_snapshot)
    build = sub.add_parser("build", help="rebuild candles and the PIT universe")
    build.add_argument("--root", required=True)
    build.add_argument("--cache-dir")
    build.add_argument("--workers", type=int, default=8)
    build.add_argument(
        "--replace", action="store_true", help="delete old candles/ and universe/"
    )
    build.add_argument("--venues", default="binance_usdt_spot,upbit_krw")
    build.add_argument("--symbols", help="comma list (smoke runs); default all")
    build.add_argument("--upbit-markets", help="comma list (smoke runs); default all")
    build.add_argument("--upbit-delisted-probes", help="comma list of KRW-* markets")
    build.add_argument("--upbit-coverage-end", help="YYYY-MM-DD")
    build.set_defaults(func=cmd_build)
    pit = sub.add_parser("pit", help="list the universe on a UTC day")
    pit.add_argument("--root", required=True)
    pit.add_argument("--venue", required=True, choices=VENUES)
    pit.add_argument("--date", required=True)
    pit.set_defaults(func=cmd_pit)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
