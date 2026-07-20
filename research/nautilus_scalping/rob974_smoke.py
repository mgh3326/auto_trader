"""Synthetic, fake-free persistence -> offline loader -> ROB-974 H1 smoke."""

from __future__ import annotations

import hashlib
from pathlib import Path

import canonical_hash
import rob941_frozen_scope as frozen
import rob941_gaps as gaps
import rob941_kline_schema as schema
import rob941_offline_loader as loader
import rob941_persistence as persistence
from funding_oi_archive import FundingRow
from rob941_manifest import (
    ArchiveProvenance,
    CorpusManifest,
    SymbolEligibility,
    SymbolFundingManifest,
    SymbolKlineManifest,
)
from rob974_features import MINUTE_MS, MinuteBar, synchronized_features


def _archive(root: Path, symbol: str, kind: str) -> ArchiveProvenance:
    data = f"ROB974 synthetic {symbol} {kind}".encode()
    local = persistence.write_raw_archive(root, symbol, kind, 2025, 7, data)
    digest = hashlib.sha256(data).hexdigest()
    return ArchiveProvenance(
        "https://example.invalid/" + local,
        "https://example.invalid/checksum",
        digest,
        local,
    )


def run_fake_free_smoke(root: Path) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    klines = []
    funding = []
    for symbol_index, symbol in enumerate(frozen.UNIVERSE):
        source = [
            schema.NormalizedKline(
                symbol,
                frozen.WINDOW_START_MS + i * MINUTE_MS,
                float(10 + symbol_index + i / 1000),
                float(11 + symbol_index + i / 1000),
                float(9 + symbol_index + i / 1000),
                float(10.5 + symbol_index + i / 1000),
                1.0,
                frozen.WINDOW_START_MS + (i + 1) * MINUTE_MS - 1,
                10.5,
                1,
                0.0,
                0.0,
            )
            for i in range(1680)
        ]
        rel, physical = persistence.write_kline_shard(root, symbol, source)
        klines.append(
            SymbolKlineManifest(
                symbol,
                "1m",
                (_archive(root, symbol, "klines"),),
                canonical_hash.canonical_sha256([r.__dict__ for r in source]),
                len(source),
                source[0].open_time_ms,
                source[-1].open_time_ms,
                tuple(
                    gaps.detect_gap_ranges(
                        [r.open_time_ms for r in source],
                        frozen.WINDOW_START_MS,
                        frozen.WINDOW_END_MS,
                    )
                ),
                shard_path=rel,
                shard_file_sha256=physical,
            )
        )
        rates = [FundingRow(frozen.WINDOW_START_MS, 8, 0.0)]
        relf, physicalf = persistence.write_funding_shard(root, symbol, rates)
        funding.append(
            SymbolFundingManifest(
                symbol,
                (_archive(root, symbol, "fundingRate"),),
                canonical_hash.canonical_sha256([r.__dict__ for r in rates]),
                1,
                rates[0].calc_time,
                rates[-1].calc_time,
                shard_path=relf,
                shard_file_sha256=physicalf,
            )
        )
    manifest = CorpusManifest(
        frozen.WINDOW_START_ISO,
        frozen.WINDOW_END_ISO,
        frozen.UNIVERSE,
        tuple(
            SymbolEligibility(symbol, **frozen.eligibility(symbol))
            for symbol in frozen.UNIVERSE
        ),
        tuple(klines),
        tuple(funding),
    )
    manifest_path = root / "rob974-smoke-manifest.json"
    manifest.save(manifest_path)
    loaded = loader.load_corpus(CorpusManifest.load(manifest_path), root)
    selected = {
        symbol: tuple(
            MinuteBar(r.open_time_ms, r.open, r.high, r.low, r.close, r.base_volume)
            for r in loaded["klines"][symbol]
        )
        for symbol in ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
    }
    snapshots = synchronized_features(selected)
    missing = {symbol: bars[:100] + bars[101:] for symbol, bars in selected.items()}
    sealed = [
        {**x.__dict__, "features": [f.__dict__ for f in x.features]} for x in snapshots
    ]
    return {
        "valid_snapshots": len(snapshots),
        "missing_minute_snapshots": len(synchronized_features(missing)),
        "feature_hash": canonical_hash.canonical_sha256(sealed),
    }
