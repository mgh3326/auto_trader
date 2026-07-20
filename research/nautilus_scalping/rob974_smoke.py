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
from rob974_features import (
    FOUR_HOUR_MS,
    MINUTE_MS,
    MinuteBar,
    symbol_features,
    synchronized_features,
)
from rob974_lineage import DerivedManifest

_SMOKE_MINUTES = 202 * 240  # ATR20 + 180 prior A values + 21 complete UTC days


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
            for i in range(_SMOKE_MINUTES)
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
    sealed = [
        {**x.__dict__, "features": [f.__dict__ for f in x.features]} for x in snapshots
    ]
    feature_hash = canonical_hash.canonical_sha256(sealed)
    funding_coverage = tuple(
        (
            symbol,
            len(loaded["funding"][symbol]),
            loaded["funding"][symbol][0].calc_time,
            loaded["funding"][symbol][-1].calc_time,
        )
        for symbol in ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
    )
    derived = DerivedManifest.create(
        rows=selected,
        context_start=frozen.WINDOW_START_MS,
        context_end=frozen.WINDOW_START_MS + _SMOKE_MINUTES * MINUTE_MS,
        funding_coverage=funding_coverage,
        funding_source_sha256=canonical_hash.canonical_sha256(
            {
                symbol: [row.__dict__ for row in loaded["funding"][symbol]]
                for symbol in ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
            }
        ),
        feature_hash=feature_hash,
    )
    damaged = dict(selected)
    gap_ts = frozen.WINDOW_START_MS + 100 * MINUTE_MS
    damaged["DOGEUSDT"] = tuple(row for row in selected["DOGEUSDT"] if row.ts != gap_ts)
    damaged_snapshots = synchronized_features(damaged)
    damaged_closes = {snapshot.decision_ts for snapshot in damaged_snapshots}
    gap_close = frozen.WINDOW_START_MS + FOUR_HOUR_MS
    recovery_close = frozen.WINDOW_START_MS + 8 * FOUR_HOUR_MS
    return {
        "valid_snapshots": len(snapshots),
        "non_null_counts": {
            name: sum(
                getattr(feature, name) is not None
                for snapshot in snapshots
                for feature in snapshot.features
            )
            for name in (
                "r",
                "tr",
                "atr20",
                "a",
                "vwap12",
                "vwap24",
                "percentile_30d",
                "range24",
            )
        },
        "missing_symbol_close_absent": gap_close not in damaged_closes,
        "other_symbol_close_present": any(
            feature.decision_ts == gap_close
            for feature in symbol_features("XRPUSDT", selected["XRPUSDT"])
        ),
        "recovery_close_present": recovery_close in damaged_closes,
        "feature_hash": feature_hash,
        "lineage_hash": derived.hash,
    }
