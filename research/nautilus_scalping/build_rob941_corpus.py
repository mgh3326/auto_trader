#!/usr/bin/env python3
"""ROB-941 — build the frozen 4-symbol historical corpus + immutable manifest.

Read-only PUBLIC data only (``data.binance.vision/data/futures/um``). No keys,
no auth, no order endpoints, no broker/scheduler wiring, no production DB
writes. The network RUN is operator-gated behind ``--run``; CI/tests exercise
only the pure helpers (``rob941_*``) with a fake in-memory opener.

Raw archives and the normalized row shards are NEVER written to disk by this
script — everything is streamed through memory (fetch -> checksum -> extract ->
normalize -> hash -> discard). Only the manifest (metadata: upstream URLs,
verified checksums, normalized-shard SHA-256, row counts, min/max timestamps,
gap ranges — no OHLCV values) is persisted, and it goes to the COMMITTED
``data_manifests/`` path (same convention as ``pit_universe.v1.json``), never
``crypto_candles_1m``, never a production table.

Usage (operator):
    cd research/nautilus_scalping
    uv run --no-project python build_rob941_corpus.py --run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import canonical_hash
import rob941_corpus_builder as cb
import rob941_frozen_scope as frozen
from rob941_manifest import (
    CorpusManifest,
    SymbolEligibility,
    SymbolFundingManifest,
    SymbolKlineManifest,
)


def _kline_manifest_for(symbol: str) -> tuple[SymbolKlineManifest, list]:
    rows, provenance, gap_ranges = cb.build_symbol_kline_shard(symbol)
    shard_hash = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    manifest = SymbolKlineManifest(
        symbol=symbol,
        interval="1m",
        archives=tuple(provenance),
        normalized_shard_sha256=shard_hash,
        row_count=len(rows),
        min_open_time_ms=rows[0].open_time_ms if rows else frozen.WINDOW_START_MS,
        max_open_time_ms=rows[-1].open_time_ms if rows else frozen.WINDOW_START_MS,
        gap_ranges=tuple(gap_ranges),
    )
    return manifest, rows


def _funding_manifest_for(symbol: str) -> SymbolFundingManifest:
    rows, provenance = cb.build_symbol_funding_shard(symbol)
    shard_hash = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    return SymbolFundingManifest(
        symbol=symbol,
        archives=tuple(provenance),
        normalized_shard_sha256=shard_hash,
        row_count=len(rows),
        min_calc_time_ms=rows[0].calc_time if rows else None,
        max_calc_time_ms=rows[-1].calc_time if rows else None,
    )


def build_corpus() -> CorpusManifest:
    """Build the full 4-symbol corpus manifest. Fail-closed: any symbol's
    checksum/OHLCV/duplicate violation aborts the whole build (no partial
    corpus is ever persisted as if it were complete)."""
    eligibility = tuple(
        SymbolEligibility(symbol=s, **frozen.eligibility(s)) for s in frozen.UNIVERSE
    )
    klines = []
    funding = []
    for symbol in frozen.UNIVERSE:
        k_manifest, _rows = _kline_manifest_for(symbol)
        klines.append(k_manifest)
        funding.append(_funding_manifest_for(symbol))

    manifest = CorpusManifest(
        window_start_iso=frozen.WINDOW_START_ISO,
        window_end_iso=frozen.WINDOW_END_ISO,
        universe=frozen.UNIVERSE,
        eligibility=eligibility,
        klines=tuple(klines),
        funding=tuple(funding),
    )
    manifest.validate_frozen_scope()
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the ROB-941 frozen 4-symbol historical corpus manifest"
    )
    ap.add_argument(
        "--run",
        action="store_true",
        help="Actually perform the network fetch (default: no-op, prints usage)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help=(
            "Manifest output path (default: "
            "data_manifests/rob941_corpus_manifest.v1.json, committed like "
            "pit_universe.v1.json — metadata only, no raw OHLCV)"
        ),
    )
    args = ap.parse_args(argv)

    if not args.run:
        print("no-op: pass --run to perform the network build (operator-gated)")
        return 0

    manifest = build_corpus()
    default_out = (
        Path(__file__).resolve().parent
        / "data_manifests"
        / "rob941_corpus_manifest.v1.json"
    )
    out_path = Path(args.out) if args.out else default_out
    manifest.save(out_path)
    print(
        json.dumps(
            {
                "manifest_path": str(out_path),
                "content_hash": manifest.content_hash(),
                "symbols": list(manifest.universe),
                "kline_row_counts": {k.symbol: k.row_count for k in manifest.klines},
                "funding_row_counts": {f.symbol: f.row_count for f in manifest.funding},
                "gap_range_counts": {
                    k.symbol: len(k.gap_ranges) for k in manifest.klines
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
