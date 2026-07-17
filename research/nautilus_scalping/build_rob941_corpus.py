#!/usr/bin/env python3
"""ROB-941 — build the frozen 4-symbol historical corpus + immutable manifest.

Read-only PUBLIC data only (``data.binance.vision/data/futures/um``). No keys,
no auth, no order endpoints, no broker/scheduler wiring, no production DB
writes. The network RUN is operator-gated behind ``--run``; CI/tests exercise
only the pure helpers (``rob941_*``) with a fake in-memory opener.

ROB-941 R1 I1 remediation: raw archives and the normalized kline/funding rows
ARE now persisted (``rob941_persistence``), as checksum-pinned ``.zip`` files
and schema-pinned Parquet shards, under ``artifact_paths.pit_data_root()``
(gitignored — never committed, never containing OHLCV values in the COMMITTED
manifest itself). Only the manifest (metadata: upstream URLs, verified
checksums, normalized-shard SHA-256, physical shard-file SHA-256,
artifact-root-relative shard paths, row counts, min/max timestamps, gap ranges
— still no raw OHLCV values) goes to the COMMITTED ``data_manifests/`` path
(same convention as ``pit_universe.v1.json``), never ``crypto_candles_1m``,
never a production table.

``ultrathink`` (captain review, atomicity correction + determinism follow-up):
every raw archive and Parquet shard is written by ``rob941_persistence`` to a
CONTENT-ADDRESSED path — the archive's own verified checksum, or the shard's
semantic ``normalized_shard_sha256``, is embedded in the path itself. Two
builds only ever write to the SAME path when their content is byte-identical
(a safe, skipped no-op re-write); genuinely different content always lands at
a genuinely different path. This makes a rebuild that fails partway (checksum
mismatch on symbol 3 of 4, say) structurally incapable of corrupting or
partially overwriting the bytes a still-published prior manifest references —
no generation directory, temp-write, or rename is needed for the per-shard
artifacts, and (unlike a random/time-based generation id) an identical rerun
against identical upstream/fixture bytes reproduces the exact same relative
paths and therefore the exact same manifest ``content_hash()`` — there is no
circular "hash depends on its own path" dependency, since every path is
derived from a checksum/content-hash computed BEFORE that path is ever built.
The only remaining atomic-publish step is the single COMMITTED MANIFEST FILE
(a same-directory temp-write + rename, publishing "which shard hashes are
current" last, only after every symbol's shard has round-tripped through the
same offline verification chain H4/H6 will use) — a mid-build failure leaves
any prior committed manifest, and everything it references, byte-for-byte
untouched.

Usage (operator):
    cd research/nautilus_scalping
    uv run --no-project python build_rob941_corpus.py --run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import artifact_paths
import canonical_hash
import rob941_archive_fetch as af
import rob941_corpus_builder as cb
import rob941_frozen_scope as frozen
import rob941_offline_loader as loader
import rob941_persistence as persist
from rob941_manifest import (
    CorpusManifest,
    SymbolEligibility,
    SymbolFundingManifest,
    SymbolKlineManifest,
)


def _kline_manifest_for(
    symbol: str, artifact_root: Path, opener: af.Opener = af.urllib_opener
) -> SymbolKlineManifest:
    def raw_sink(sym: str, kind: str, year: int, month: int, zip_bytes: bytes) -> str:
        return persist.write_raw_archive(
            artifact_root, sym, kind, year, month, zip_bytes
        )

    rows, provenance, gap_ranges = cb.build_symbol_kline_shard(
        symbol, opener=opener, raw_archive_sink=raw_sink
    )
    shard_hash = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    shard_path, shard_file_sha256 = persist.write_kline_shard(
        artifact_root, symbol, rows
    )
    return SymbolKlineManifest(
        symbol=symbol,
        interval="1m",
        archives=tuple(provenance),
        normalized_shard_sha256=shard_hash,
        row_count=len(rows),
        min_open_time_ms=rows[0].open_time_ms if rows else frozen.WINDOW_START_MS,
        max_open_time_ms=rows[-1].open_time_ms if rows else frozen.WINDOW_START_MS,
        gap_ranges=tuple(gap_ranges),
        shard_path=shard_path,
        shard_file_sha256=shard_file_sha256,
    )


def _funding_manifest_for(
    symbol: str, artifact_root: Path, opener: af.Opener = af.urllib_opener
) -> SymbolFundingManifest:
    def raw_sink(sym: str, kind: str, year: int, month: int, zip_bytes: bytes) -> str:
        return persist.write_raw_archive(
            artifact_root, sym, kind, year, month, zip_bytes
        )

    rows, provenance = cb.build_symbol_funding_shard(
        symbol, opener=opener, raw_archive_sink=raw_sink
    )
    shard_hash = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    shard_path, shard_file_sha256 = persist.write_funding_shard(
        artifact_root, symbol, rows
    )
    return SymbolFundingManifest(
        symbol=symbol,
        archives=tuple(provenance),
        normalized_shard_sha256=shard_hash,
        row_count=len(rows),
        min_calc_time_ms=rows[0].calc_time if rows else None,
        max_calc_time_ms=rows[-1].calc_time if rows else None,
        shard_path=shard_path,
        shard_file_sha256=shard_file_sha256,
    )


def build_corpus(
    artifact_root: Path, opener: af.Opener = af.urllib_opener
) -> CorpusManifest:
    """Build the full 4-symbol corpus manifest, materializing raw archives +
    Parquet shards at their content-addressed paths under ``artifact_root``
    (see the module docstring's ``ultrathink`` note). Fail-closed: any symbol's
    checksum/OHLCV/duplicate violation aborts the whole build (no partial
    corpus is ever persisted as if it were complete), and because every path is
    content-addressed, a failed build never overwrites/corrupts the bytes a
    prior, still-published manifest points at. The returned manifest is ALWAYS
    re-verified via a full offline ``load_corpus`` pass (network-0, the exact
    chain H4/H6 will use) against ``artifact_root`` before this function
    returns, so a materialization bug here can never silently produce an
    unusable "complete" manifest.
    """
    eligibility = tuple(
        SymbolEligibility(symbol=s, **frozen.eligibility(s)) for s in frozen.UNIVERSE
    )
    klines = [_kline_manifest_for(s, artifact_root, opener) for s in frozen.UNIVERSE]
    funding = [_funding_manifest_for(s, artifact_root, opener) for s in frozen.UNIVERSE]

    manifest = CorpusManifest(
        window_start_iso=frozen.WINDOW_START_ISO,
        window_end_iso=frozen.WINDOW_END_ISO,
        universe=frozen.UNIVERSE,
        eligibility=eligibility,
        klines=tuple(klines),
        funding=tuple(funding),
    )
    manifest.validate_frozen_scope()
    loader.load_corpus(manifest, artifact_root)  # atomic-publish gate (ROB-941 R1 I1)
    return manifest


def _atomic_save(manifest: CorpusManifest, out_path: Path) -> None:
    """Write ``manifest`` then atomically replace ``out_path`` -- readers never
    observe a partially-written committed manifest file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    manifest.save(tmp_path)
    tmp_path.replace(out_path)


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

    artifact_root = artifact_paths.pit_data_root()
    manifest = build_corpus(artifact_root=artifact_root)
    default_out = (
        Path(__file__).resolve().parent
        / "data_manifests"
        / "rob941_corpus_manifest.v1.json"
    )
    out_path = Path(args.out) if args.out else default_out
    _atomic_save(manifest, out_path)
    print(
        json.dumps(
            {
                "manifest_path": str(out_path),
                "artifact_root": str(artifact_root),
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
