"""Persisted, callback-free ROB-984 H1-through-H4 test workload.

Only the synthetic 1m source rows are test-owned.  Persistence, manifest
reload, H1 features, H3 generators, H2 engines, H4 selection/paths/PBO, and
H6-A attempt assembly all execute through production modules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path

import canonical_hash
import rob941_frozen_scope as frozen
import rob941_gaps as gaps
import rob941_kline_schema as schema
import rob941_offline_loader as loader
import rob941_persistence as persistence
import rob974_h3_h2_adapter as h3_h2_adapter
import rob974_h3_smoke as h3_smoke
from funding_oi_archive import FundingRow
from rob941_manifest import (
    CorpusManifest,
    SymbolEligibility,
    SymbolFundingManifest,
    SymbolKlineManifest,
)
from rob974_features import MINUTE_MS, MinuteBar
from rob974_h3_manifest import get_config
from rob974_h3_s3 import EmitWindow, generate_s3_global
from rob974_h3_s4 import generate_s4_global
from rob974_h4_contracts import exact_h4_folds

from app.services.rob974_h6b_materializer import (
    ActualH4CampaignResult,
    ActualH4InputData,
    ActualMergedH4Runner,
    ProductionIdentityPlan,
    build_production_identity_plan,
)

_FOUR_HOUR_MS = 4 * 60 * 60 * 1000
_BASES = {
    "BTCUSDT": 60_000.0,
    "XRPUSDT": 0.50,
    "DOGEUSDT": 0.20,
    "SOLUSDT": 150.0,
}
_BASE_LOADINGS = {
    "BTCUSDT": 0.0,
    "XRPUSDT": 1.0,
    "DOGEUSDT": 0.0,
    "SOLUSDT": -1.0,
}
_ROTATED_LOADINGS = (
    {"XRPUSDT": 1.0, "DOGEUSDT": 0.0, "SOLUSDT": -1.0},
    {"XRPUSDT": -1.0, "DOGEUSDT": 1.0, "SOLUSDT": 0.0},
    {"XRPUSDT": 0.0, "DOGEUSDT": -1.0, "SOLUSDT": 1.0},
)


@dataclass(frozen=True, slots=True)
class FakeFreeInputBundle:
    identity: ProductionIdentityPlan
    input_data: ActualH4InputData
    runner: ActualMergedH4Runner
    manifest_hash: str
    feature_hash: str
    gap_close: int
    recovery_close: int
    thesis_signal_ts: int
    stall_signal_ts: int


@dataclass(frozen=True, slots=True)
class FakeFreeCampaignBundle:
    identity: ProductionIdentityPlan
    input_data: ActualH4InputData
    runner: ActualMergedH4Runner
    result: ActualH4CampaignResult
    manifest_hash: str
    feature_hash: str
    gap_close: int
    recovery_close: int
    thesis_signal_ts: int
    stall_signal_ts: int


def _market_and_residual() -> tuple[tuple[float, ...], tuple[float, ...]]:
    base_market = h3_smoke._market_returns()
    base_residual = h3_smoke._residual_states()
    tail_market = base_market[190:202]
    tail_residual = base_residual[190:202]
    # The first two rows are the already-reviewed real THESIS_EXIT extension
    # used by ROB-980.  Thereafter each 12-bar signal cycle has three flat
    # bars, and its idiosyncratic loading rotates across all three symbols.
    market = base_market + (-0.20, 0.0) + (tail_market + (tail_market[-1],) * 3) * 45
    residual = (
        base_residual + (0.24, 0.24) + (tail_residual + (tail_residual[-1],) * 3) * 45
    )
    return market, residual


def _target_closes(
    symbol: str,
    *,
    market: tuple[float, ...],
    residual: tuple[float, ...],
) -> tuple[float, ...]:
    market_level = 0.0
    closes: list[float] = []
    for index, (market_return, residual_state) in enumerate(
        zip(market, residual, strict=True)
    ):
        market_level += market_return
        if index < 204:
            loading = _BASE_LOADINGS[symbol]
        else:
            loading = _ROTATED_LOADINGS[((index - 204) // 15) % 3].get(symbol, 0.0)
        closes.append(
            _BASES[symbol] * math.exp(market_level + loading * residual_state)
        )
    return tuple(closes)


def _normalized_rows(
    symbol: str,
    symbol_index: int,
    *,
    market: tuple[float, ...],
    residual: tuple[float, ...],
) -> tuple[schema.NormalizedKline, ...]:
    targets = _target_closes(symbol, market=market, residual=residual)
    previous = targets[0] / math.exp(market[0])
    rows: list[schema.NormalizedKline] = []
    for bar_index, target in enumerate(targets):
        log_delta = math.log(target / previous)
        wick = (
            0.040
            if (bar_index <= 180 and bar_index % 12 == 0) or bar_index == 190
            else (
                0.014 if bar_index == 201 else 0.0018 + 0.00035 * ((bar_index * 7) % 5)
            )
        )
        prior_close = previous
        for minute in range(240):
            close = previous * math.exp(log_delta * (minute + 1) / 240.0)
            open_value = prior_close
            high = max(open_value, close) * (1.0 + wick)
            low = min(open_value, close) * (1.0 - wick)
            volume = 1.0 + symbol_index / 10.0
            open_time = frozen.WINDOW_START_MS + (bar_index * 240 + minute) * MINUTE_MS
            rows.append(
                schema.NormalizedKline(
                    symbol=symbol,
                    open_time_ms=open_time,
                    open=open_value,
                    high=high,
                    low=low,
                    close=close,
                    base_volume=volume,
                    close_time_ms=open_time + MINUTE_MS - 1,
                    quote_volume=close * volume,
                    trade_count=1,
                    taker_buy_volume=0.0,
                    taker_buy_quote_volume=0.0,
                )
            )
            prior_close = close
        previous = target
    if symbol == "SOLUSDT":
        rows.pop(h3_smoke._GAP_BAR_INDEX * 240 + h3_smoke._GAP_MINUTE_OFFSET)
    return tuple(rows)


def _to_h1(
    rows: dict[str, tuple[schema.NormalizedKline, ...]],
) -> dict[str, tuple[MinuteBar, ...]]:
    return {
        symbol: tuple(
            MinuteBar(
                row.open_time_ms,
                row.open,
                row.high,
                row.low,
                row.close,
                row.base_volume,
            )
            for row in rows[symbol]
        )
        for symbol in ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
    }


def _stall_patch(
    rows: dict[str, tuple[schema.NormalizedKline, ...]],
) -> tuple[dict[str, tuple[schema.NormalizedKline, ...]], int]:
    selected = _to_h1(rows)
    context, _feature_hash = h3_smoke._context(selected)
    fold = exact_h4_folds()[0]
    window = EmitWindow(fold.oos_start_ms, fold.oos_end_ms)
    s3_output = generate_s3_global(context, window, get_config("S3-05"))
    s4_output = generate_s4_global(context, window, get_config("S4-01"))
    corpus_end_ts = max(values[-1].ts for values in selected.values()) + MINUTE_MS
    preview = h3_h2_adapter.run_h2_integration(
        s3_output,
        s4_output,
        selected,
        context,
        fold_id=fold.fold_id,
        corpus_end_ts=corpus_end_ts,
        horizon_end_ts=fold.oos_end_ms,
    )
    chosen = next(
        trade
        for trade in reversed(preview.s4_engine_result.trades)
        if trade.signal_ts + 8 * 60 * 60 * 1000 < fold.oos_end_ms
    )
    patched = dict(rows)
    for symbol in chosen.pair:
        by_timestamp = {row.open_time_ms: row for row in rows[symbol]}
        entry_price = by_timestamp[chosen.signal_ts].open
        patched[symbol] = tuple(
            replace(
                row,
                open=entry_price,
                high=entry_price,
                low=entry_price,
                close=entry_price,
                quote_volume=entry_price * row.base_volume,
            )
            if chosen.signal_ts
            <= row.open_time_ms
            <= chosen.signal_ts + 8 * 60 * 60 * 1000
            else row
            for row in rows[symbol]
        )
    return patched, chosen.signal_ts


def _thesis_patch(
    rows: dict[str, tuple[schema.NormalizedKline, ...]],
) -> tuple[dict[str, tuple[schema.NormalizedKline, ...]], int]:
    """Make one selected S3-20 position exit through the real thesis rule.

    The signal and entry are first derived through the actual H3 generator
    and H2 engine.  Only minutes strictly at/after that point-in-time signal
    are changed.  A 1.5% four-hour pullback stays inside the entry-frozen 2%
    stop, avoids the 4% target, and makes the completed H1 close fall below
    its actual rolling VWAP24.  H2 consequently emits ``THESIS_EXIT`` at the
    first real four-hour boundary.
    """

    selected = _to_h1(rows)
    context, _feature_hash = h3_smoke._context(selected)
    fold = exact_h4_folds()[0]
    window = EmitWindow(fold.oos_start_ms, fold.oos_end_ms)
    s3_output = generate_s3_global(context, window, get_config("S3-20"))
    s4_output = generate_s4_global(context, window, get_config("S4-23"))
    corpus_end_ts = max(values[-1].ts for values in selected.values()) + MINUTE_MS
    preview = h3_h2_adapter.run_h2_integration(
        s3_output,
        s4_output,
        selected,
        context,
        fold_id=fold.fold_id,
        corpus_end_ts=corpus_end_ts,
        horizon_end_ts=fold.oos_end_ms,
    )
    chosen = next(
        trade
        for trade in preview.s3_engine_result.trades
        if trade.config_id == "S3-20"
        and trade.signal_ts + _FOUR_HOUR_MS < fold.oos_end_ms
    )
    boundary = chosen.signal_ts + _FOUR_HOUR_MS
    patched = dict(rows)
    adjusted: list[schema.NormalizedKline] = []
    for row in rows[chosen.symbol]:
        if chosen.signal_ts <= row.open_time_ms <= boundary:
            minute = (row.open_time_ms - chosen.signal_ts) // MINUTE_MS
            open_fraction = min(minute, 240) / 240.0
            close_fraction = min(minute + 1, 240) / 240.0
            open_value = chosen.entry_price * (1.0 - 0.015 * open_fraction)
            close_value = chosen.entry_price * (1.0 - 0.015 * close_fraction)
            adjusted.append(
                replace(
                    row,
                    open=open_value,
                    high=max(open_value, close_value) * 1.00005,
                    low=min(open_value, close_value) * 0.99995,
                    close=close_value,
                    quote_volume=close_value * row.base_volume,
                )
            )
        else:
            adjusted.append(row)
    patched[chosen.symbol] = tuple(adjusted)
    return patched, chosen.signal_ts


def _persist(
    root: Path,
    rows: dict[str, tuple[schema.NormalizedKline, ...]],
) -> CorpusManifest:
    klines: list[SymbolKlineManifest] = []
    funding: list[SymbolFundingManifest] = []
    for symbol in frozen.UNIVERSE:
        source = rows[symbol]
        relative, physical = persistence.write_kline_shard(root, symbol, source)
        klines.append(
            SymbolKlineManifest(
                symbol,
                "1m",
                (h3_smoke._archive(root, symbol, "klines"),),
                canonical_hash.canonical_sha256([row.__dict__ for row in source]),
                len(source),
                source[0].open_time_ms,
                source[-1].open_time_ms,
                tuple(
                    gaps.detect_gap_ranges(
                        [row.open_time_ms for row in source],
                        frozen.WINDOW_START_MS,
                        frozen.WINDOW_END_MS,
                    )
                ),
                shard_path=relative,
                shard_file_sha256=physical,
            )
        )
        funding_rows = (FundingRow(frozen.WINDOW_START_MS, 8, 0.0),)
        funding_relative, funding_physical = persistence.write_funding_shard(
            root, symbol, funding_rows
        )
        funding.append(
            SymbolFundingManifest(
                symbol,
                (h3_smoke._archive(root, symbol, "fundingRate"),),
                canonical_hash.canonical_sha256([row.__dict__ for row in funding_rows]),
                len(funding_rows),
                funding_rows[0].calc_time,
                funding_rows[-1].calc_time,
                shard_path=funding_relative,
                shard_file_sha256=funding_physical,
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
    manifest.save(root / "rob984-fake-free-manifest.json")
    return manifest


def prepare_fake_free_input(root: Path) -> FakeFreeInputBundle:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    market, residual = _market_and_residual()
    raw = {
        symbol: _normalized_rows(
            symbol,
            index,
            market=market,
            residual=residual,
        )
        for index, symbol in enumerate(frozen.UNIVERSE)
    }
    stalled, stall_signal_ts = _stall_patch(raw)
    patched, thesis_signal_ts = _thesis_patch(stalled)
    manifest = _persist(root, patched)
    manifest_path = root / "rob984-fake-free-manifest.json"
    reloaded = CorpusManifest.load(manifest_path)
    if manifest.content_hash() != reloaded.content_hash():
        raise AssertionError("persisted synthetic manifest changed on reload")
    loaded = loader.load_corpus(reloaded, root)
    selected = h3_smoke._selected_minutes(loaded)
    _context, feature_hash = h3_smoke._context(selected)
    corpus_end_ts = max(rows[-1].ts for rows in selected.values()) + MINUTE_MS
    input_data = ActualH4InputData.from_mapping(
        selected,
        corpus_end_ts=corpus_end_ts,
        persisted_corpus_hash=manifest.content_hash(),
        persisted_feature_hash=feature_hash,
    )
    identity = build_production_identity_plan()
    runner = ActualMergedH4Runner(input_data)
    gap_close = frozen.WINDOW_START_MS + (h3_smoke._GAP_BAR_INDEX + 1) * _FOUR_HOUR_MS
    return FakeFreeInputBundle(
        identity=identity,
        input_data=input_data,
        runner=runner,
        manifest_hash=manifest.content_hash(),
        feature_hash=feature_hash,
        gap_close=gap_close,
        recovery_close=gap_close + 7 * _FOUR_HOUR_MS,
        thesis_signal_ts=thesis_signal_ts,
        stall_signal_ts=stall_signal_ts,
    )


async def build_fake_free_campaign(root: Path) -> FakeFreeCampaignBundle:
    prepared = prepare_fake_free_input(root)
    result = await prepared.runner.run(prepared.identity)
    return FakeFreeCampaignBundle(
        identity=prepared.identity,
        input_data=prepared.input_data,
        runner=prepared.runner,
        result=result,
        manifest_hash=prepared.manifest_hash,
        feature_hash=prepared.feature_hash,
        gap_close=prepared.gap_close,
        recovery_close=prepared.recovery_close,
        thesis_signal_ts=prepared.thesis_signal_ts,
        stall_signal_ts=prepared.stall_signal_ts,
    )
