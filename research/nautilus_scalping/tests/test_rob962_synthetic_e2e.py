"""ROB-962 synthetic H1-persistence through H5 scorecard integration smoke.

The deliberately sparse rows are persisted with the real H1 raw/shard writers,
described by a truthful (non-gap-free) manifest, saved/reloaded, and verified by
the real offline loader.  The loader's decoded rows -- not the source objects --
feed the rest of the smoke.  Sparse data cannot truthfully satisfy production
PBO's full-window empty-gap precondition, so only that metadata fact uses the
documented synthetic corpus-input seam: the test injects empty gap tuples and
does not present them as manifest authority.  From there every component is the
real frozen implementation -- aggregation, H3 generators, H4 walk-forward and
OOS capture, full-window PBO, H6 evidence conversion, H5 scorecard, and the
atomic scorecard writer.  No generator, engine, metric, or verdict callback is
faked.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import canonical_hash
import rob941_frozen_scope as frozen_scope
import rob941_gaps
import rob941_kline_schema
import rob941_offline_loader
import rob941_persistence
from funding_oi_archive import FundingRow
from rob940_bars_agg import Bar1m, aggregate_complete
from rob940_signal_manifest import FROZEN_S1_CONFIGS, FROZEN_S2_CONFIGS
from rob940_signal_s1 import generate_s1_signals
from rob940_signal_s2 import generate_s2_signals
from rob941_frozen_scope import UNIVERSE, WINDOW_END_MS, WINDOW_START_MS
from rob941_funding_sidecar import FundingSidecar
from rob941_manifest import (
    ArchiveProvenance,
    CorpusManifest,
    SymbolEligibility,
    SymbolFundingManifest,
    SymbolKlineManifest,
)
from rob944_folds import generate_frozen_fold_schedule
from rob944_frozen_campaign import (
    CANONICAL_ROW_ORDER,
    PRODUCTION_S1_STRATEGY_KEY,
    PRODUCTION_S2_STRATEGY_KEY,
    build_production_frozen_campaign_envelope,
)
from rob944_walkforward import (
    ConfigSpec,
    GeneratedSignalBatch,
    run_walkforward,
    summarize_config_attempts_for_h6,
)
from rob945_accounting_seal import derive_campaign_run_id
from rob945_canonical_payload import to_canonical_payload
from rob945_capture import (
    OosSignalCaptureSink,
    expected_oos_calls_from_walkforward_result,
    wrap_config_specs_for_oos_capture,
)
from rob945_scorecard import build_scorecard, render_markdown
from rob945_signal_concurrency import compute_signal_concurrency
from rob960_scorecard_writer import publish_staged_scorecard, stage_scorecard_files
from rob960_strategy_evidence import build_strategy_evidence
from run_rob944_campaign import (
    _s2_rejections_to_no_trade_records,
    _summary_to_attempt_evidence,
)

from app.schemas.research_campaign_bridge import CampaignCompletenessReport
from research_contracts.canonical_hash import (
    canonical_json,
    canonical_sha256,
)

_MINUTE_MS = 60_000
_ACTIVE_SYMBOLS = ("XRPUSDT", "DOGEUSDT")
_SCENARIOS = ("base", "primary_stress", "upward_stress")
_STRATEGY_KEYS = {
    "S1": PRODUCTION_S1_STRATEGY_KEY,
    "S2": PRODUCTION_S2_STRATEGY_KEY,
}


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _s1_cluster(start_ms: int) -> tuple[Bar1m, ...]:
    """One real S1-03/S1-10 signal plus an immediate real engine TP."""
    rows: list[Bar1m] = []
    # Twenty-four complete flat 15m buckets: ATR=0.3, U=100.15, D=99.85.
    for bucket_index in range(24):
        bucket_start = start_ms + bucket_index * 15 * _MINUTE_MS
        rows.extend(
            Bar1m(
                ts=bucket_start + minute * _MINUTE_MS,
                open=100.0,
                high=100.15,
                low=99.85,
                close=100.0,
                volume=10.0,
            )
            for minute in range(15)
        )

    # Bucket 24: q=165/150=1.10, ATR=0.3075, chase=0.487805.  Only the
    # q_min=1.0 rows S1-03/S1-10 pass; every q_min>=1.25 row stays silent.
    breakout_start = start_ms + 24 * 15 * _MINUTE_MS
    rows.extend(
        Bar1m(
            ts=breakout_start + minute * _MINUTE_MS,
            open=100.0,
            high=100.30,
            low=99.85,
            close=100.30 if minute == 14 else 100.0,
            volume=11.0,
        )
        for minute in range(15)
    )

    # The aggregate close boundary is the real next-1m entry timestamp.
    # High touches the clipped 45bp * 1.8 = 81bp TP on the entry bar.
    rows.append(
        Bar1m(
            ts=breakout_start + 15 * _MINUTE_MS,
            open=100.30,
            high=101.20,
            low=100.30,
            close=101.15,
            volume=10.0,
        )
    )
    assert len(rows) == 376
    return tuple(rows)


def _s2_cluster(start_ms: int) -> tuple[Bar1m, ...]:
    """One real S2-01/S2-09 signal plus an immediate real engine TP."""
    noise_return = 0.00155
    shock_return = 2.85 * 1.4826 * noise_return

    closes = [100.0]
    # 288 prior returns alternate +/-a: median~=0, MAD~=a, and low ER48.
    for index in range(1, 289):
        signed_return = noise_return if index % 2 else -noise_return
        closes.append(closes[-1] * math.exp(signed_return))
    target = closes[-1]
    closes.extend(
        (
            target * math.exp(-shock_return),
            target * math.exp(-shock_return + 0.001),
        )
    )

    rows: list[Bar1m] = []
    for bucket_index, close in enumerate(closes):
        previous = closes[bucket_index - 1] if bucket_index else close
        bucket_start = start_ms + bucket_index * 5 * _MINUTE_MS
        volume = 22.5 if bucket_index == 289 else 10.0
        # Broad pre-shock wicks keep S1 silent in these long S2 warm-up
        # segments.  Shock/confirmation use their actual directional range.
        high = (
            max(102.0, previous, close) if bucket_index < 289 else max(previous, close)
        )
        low = min(98.0, previous, close) if bucket_index < 289 else min(previous, close)
        for minute in range(5):
            minute_close = (
                close
                if minute == 4
                else previous + (close - previous) * (minute + 1) / 5
            )
            minute_open = previous if minute == 0 else rows[-1].close
            rows.append(
                Bar1m(
                    ts=bucket_start + minute * _MINUTE_MS,
                    open=minute_open,
                    high=max(high, minute_open, minute_close),
                    low=min(low, minute_open, minute_close),
                    close=minute_close,
                    volume=volume,
                )
            )

    # Observed frozen gates: z=-2.85, v=2.25, ER48~=0.1017.  Thus only
    # S2-01/S2-09 pass.  T/E-1 is exactly 90bp and long-direction-valid.
    entry = target / 1.009
    rows.append(
        Bar1m(
            ts=start_ms + len(closes) * 5 * _MINUTE_MS,
            open=entry,
            high=target,
            low=entry,
            close=target,
            volume=10.0,
        )
    )
    assert len(rows) == 1_456
    return tuple(rows)


def _approved_sparse_source_inputs():
    """Build sparse source rows which the real H1 persistence path will store."""
    s1_starts = [
        _ms(f"2025-07-{day:02d}T00:00:00Z") for day in (4, 8, 12, 16, 20, 24)
    ] + [_ms("2025-11-02T00:00:00Z")]
    s2_starts = [
        _ms(f"2025-07-{day:02d}T00:00:00Z") for day in (2, 6, 10, 14, 18, 22)
    ] + [_ms("2025-10-30T00:00:00Z")]

    active_rows = tuple(
        sorted(
            [bar for start in s1_starts for bar in _s1_cluster(start)]
            + [bar for start in s2_starts for bar in _s2_cluster(start)],
            key=lambda bar: bar.ts,
        )
    )
    assert len(active_rows) == 12_824
    assert len({bar.ts for bar in active_rows}) == len(active_rows)

    # Capture classifies an empty slice as non-OOS, while finalization rightly
    # expects all four frozen-symbol calls.  One incomplete-bucket bar inside
    # fold-00 OOS makes each inactive call observable without making a signal.
    inert_oos_rows = (
        Bar1m(
            ts=_ms("2025-10-29T03:00:00Z"),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=1.0,
        ),
    )
    bars_1m = {
        symbol: active_rows if symbol in _ACTIVE_SYMBOLS else inert_oos_rows
        for symbol in UNIVERSE
    }
    funding_sidecars = {
        symbol: FundingSidecar.from_rows(
            symbol,
            (
                FundingRow(
                    calc_time=WINDOW_START_MS,
                    funding_interval_hours=8,
                    last_funding_rate=0.0,
                ),
            ),
        )
        for symbol in UNIVERSE
    }
    return bars_1m, funding_sidecars


def _persist_archive(
    artifact_root: Path, *, symbol: str, kind: str
) -> ArchiveProvenance:
    """Persist a checksum-pinned synthetic upstream archive for H1 verification."""
    content = f"ROB-962 synthetic {kind} archive for {symbol}".encode()
    local_path = rob941_persistence.write_raw_archive(
        artifact_root,
        symbol,
        kind,
        2025,
        7,
        content,
    )
    checksum = hashlib.sha256(content).hexdigest()
    if kind == "klines":
        stem = f"klines/{symbol}/1m/{symbol}-1m-2025-07.zip"
    else:
        stem = f"fundingRate/{symbol}/{symbol}-fundingRate-2025-07.zip"
    url = f"https://data.binance.vision/data/futures/um/monthly/{stem}"
    return ArchiveProvenance(
        url=url,
        checksum_url=f"{url}.CHECKSUM",
        checksum_sha256=checksum,
        local_path=local_path,
    )


def _persist_manifest_and_offline_load(
    artifact_root: Path,
    source_bars: dict[str, tuple[Bar1m, ...]],
    source_funding: dict[str, FundingSidecar],
):
    """Round-trip sparse rows through actual H1 writers/manifest/offline loader."""
    kline_manifests = []
    funding_manifests = []
    source_klines = {}

    for symbol in UNIVERSE:
        normalized = [
            rob941_kline_schema.NormalizedKline(
                symbol=symbol,
                open_time_ms=bar.ts,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                base_volume=bar.volume,
                close_time_ms=bar.ts + _MINUTE_MS - 1,
                quote_volume=bar.volume * bar.close,
                trade_count=1,
                taker_buy_volume=0.0,
                taker_buy_quote_volume=0.0,
            )
            for bar in source_bars[symbol]
        ]
        source_klines[symbol] = normalized
        kline_path, kline_file_sha = rob941_persistence.write_kline_shard(
            artifact_root, symbol, normalized
        )
        truthful_gaps = tuple(
            rob941_gaps.detect_gap_ranges(
                [row.open_time_ms for row in normalized],
                WINDOW_START_MS,
                WINDOW_END_MS,
            )
        )
        kline_manifests.append(
            SymbolKlineManifest(
                symbol=symbol,
                interval="1m",
                archives=(
                    _persist_archive(artifact_root, symbol=symbol, kind="klines"),
                ),
                normalized_shard_sha256=canonical_hash.canonical_sha256(
                    [row.__dict__ for row in normalized]
                ),
                shard_path=kline_path,
                shard_file_sha256=kline_file_sha,
                row_count=len(normalized),
                min_open_time_ms=normalized[0].open_time_ms,
                max_open_time_ms=normalized[-1].open_time_ms,
                gap_ranges=truthful_gaps,
            )
        )

        funding_rows = list(source_funding[symbol].rows)
        funding_path, funding_file_sha = rob941_persistence.write_funding_shard(
            artifact_root, symbol, funding_rows
        )
        funding_manifests.append(
            SymbolFundingManifest(
                symbol=symbol,
                archives=(
                    _persist_archive(artifact_root, symbol=symbol, kind="fundingRate"),
                ),
                normalized_shard_sha256=canonical_hash.canonical_sha256(
                    [row.__dict__ for row in funding_rows]
                ),
                shard_path=funding_path,
                shard_file_sha256=funding_file_sha,
                row_count=len(funding_rows),
                min_calc_time_ms=funding_rows[0].calc_time,
                max_calc_time_ms=funding_rows[-1].calc_time,
            )
        )

    manifest = CorpusManifest(
        window_start_iso=frozen_scope.WINDOW_START_ISO,
        window_end_iso=frozen_scope.WINDOW_END_ISO,
        universe=UNIVERSE,
        eligibility=tuple(
            SymbolEligibility(symbol=symbol, **frozen_scope.eligibility(symbol))
            for symbol in UNIVERSE
        ),
        klines=tuple(kline_manifests),
        funding=tuple(funding_manifests),
    )
    assert all(row.gap_ranges for row in manifest.klines)

    manifest_path = artifact_root / "rob962-synthetic-manifest.json"
    manifest.save(manifest_path)
    reloaded_manifest = CorpusManifest.load(manifest_path)
    assert reloaded_manifest.content_hash() == manifest.content_hash()
    loaded = rob941_offline_loader.load_corpus(reloaded_manifest, artifact_root)
    assert loaded["klines"] == source_klines
    assert loaded["funding"] == {
        symbol: list(source_funding[symbol].rows) for symbol in UNIVERSE
    }

    bars_1m = {
        symbol: tuple(
            Bar1m(
                ts=row.open_time_ms,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.base_volume,
            )
            for row in loaded["klines"][symbol]
        )
        for symbol in UNIVERSE
    }
    funding_sidecars = {
        symbol: FundingSidecar.from_rows(symbol, loaded["funding"][symbol])
        for symbol in UNIVERSE
    }
    # This is the one synthetic-only metadata injection.  The persisted H1
    # manifest above retains its truthful nonempty gaps; production PBO accepts
    # only a proven gap-free real corpus and must never infer this empty value.
    gap_ranges = dict.fromkeys(UNIVERSE, ())
    return bars_1m, funding_sidecars, gap_ranges


def _s1_gen_factory(config):
    def _generate(symbol, bars_slice, fold_id):
        return generate_s1_signals(
            aggregate_complete(bars_slice, bucket_minutes=15),
            config,
            symbol=symbol,
            fold_id=fold_id,
        )

    return _generate


def _s2_gen_factory(config):
    def _generate(symbol, bars_slice, fold_id):
        generated = generate_s2_signals(
            aggregate_complete(bars_slice, bucket_minutes=5),
            bars_slice,
            config,
            symbol=symbol,
            fold_id=fold_id,
        )
        return GeneratedSignalBatch(
            signals=generated.signals,
            rejections=_s2_rejections_to_no_trade_records(generated.rejections),
        )

    return _generate


def _config_specs(strategy: str) -> tuple[ConfigSpec, ...]:
    configs, factory = (
        (FROZEN_S1_CONFIGS, _s1_gen_factory)
        if strategy == "S1"
        else (FROZEN_S2_CONFIGS, _s2_gen_factory)
    )
    return tuple(
        ConfigSpec(config_id=config.config_id, generate_signals=factory(config))
        for config in configs
    )


def _canonical_result_bytes(result) -> bytes:
    return canonical_json(to_canonical_payload(result)).encode("utf-8")


def _campaign_report_from_attempts(
    attempts, *, campaign_run_id: str, expected_experiment_ids: tuple[str, ...]
) -> CampaignCompletenessReport:
    """Derive the in-memory H6 completeness DTO from the actual 24 attempts."""
    expected = set(expected_experiment_ids)
    by_experiment: dict[str, list[int]] = defaultdict(list)
    status_counts = dict.fromkeys(("completed", "rejected", "crashed", "timeout"), 0)
    primary_attempts = 0
    retry_attempts = 0
    for attempt in attempts:
        key = attempt.attempt_key
        by_experiment[key.experiment_id].append(key.retry_index)
        status_counts[attempt.status] += 1
        primary_attempts += int(key.retry_index == 0)
        retry_attempts += int(key.retry_index > 0)

    observed = set(by_experiment)
    missing = sorted(
        experiment_id
        for experiment_id in expected
        if 0 not in by_experiment.get(experiment_id, ())
    )
    extra = sorted(observed - expected)
    duplicate_or_gap = sorted(
        experiment_id
        for experiment_id, retry_indices in by_experiment.items()
        if sorted(retry_indices) != list(range(max(retry_indices, default=-1) + 1))
    )
    complete = not (missing or extra or duplicate_or_gap)
    return CampaignCompletenessReport(
        campaign_run_id=campaign_run_id,
        expected_total=len(expected),
        actual_registrations=len(observed),
        primary_attempts=primary_attempts,
        total_attempts=len(attempts),
        retry_attempts=retry_attempts,
        status_counts=status_counts,
        missing_experiment_ids=missing,
        extra_experiment_ids=extra,
        mismatch_experiment_ids=[],
        duplicate_or_gap_experiment_ids=duplicate_or_gap,
        verdict="complete" if complete else "incomplete",
    )


def test_sparse_real_pipeline_materializes_scorecard_with_exact_float_funding(
    tmp_path: Path,
):
    source_bars, source_funding = _approved_sparse_source_inputs()
    bars_1m, funding_sidecars, gap_ranges = _persist_manifest_and_offline_load(
        tmp_path / "synthetic-corpus",
        source_bars,
        source_funding,
    )
    fold_schedule = generate_frozen_fold_schedule(WINDOW_START_MS, WINDOW_END_MS)
    assert len(fold_schedule) == 8

    plain_results = {}
    wrapped_results = {}
    capture_sinks = {}
    captured_signals = {}

    for strategy in ("S1", "S2"):
        plain_specs = _config_specs(strategy)
        plain_result = run_walkforward(
            strategy=strategy,
            configs=plain_specs,
            bars_1m=bars_1m,
            funding_sidecars=funding_sidecars,
            gap_ranges=gap_ranges,
            fold_schedule=fold_schedule,
        )

        sink = OosSignalCaptureSink()
        wrapped_specs = wrap_config_specs_for_oos_capture(
            plain_specs,
            strategy=strategy,
            fold_schedule=fold_schedule,
            sink=sink,
        )
        wrapped_result = run_walkforward(
            strategy=strategy,
            configs=wrapped_specs,
            bars_1m=bars_1m,
            funding_sidecars=funding_sidecars,
            gap_ranges=gap_ranges,
            fold_schedule=fold_schedule,
        )
        sink.finalize(expected_oos_calls_from_walkforward_result(wrapped_result))
        snapshot = sink.snapshot()

        # Observer effect zero: exact canonical all-field equality, bytes,
        # and SHA all agree independently.  Raw dataclass ``==`` is not a
        # valid whole-WFR equality authority because rejected selection rows
        # legitimately carry NaN PF and IEEE NaN is unequal even to itself.
        plain_payload = to_canonical_payload(plain_result)
        wrapped_payload = to_canonical_payload(wrapped_result)
        assert plain_payload == wrapped_payload
        assert _canonical_result_bytes(plain_result) == _canonical_result_bytes(
            wrapped_result
        )
        assert canonical_sha256(plain_payload) == canonical_sha256(wrapped_payload)
        assert len(expected_oos_calls_from_walkforward_result(wrapped_result)) == 4
        assert len(snapshot) == 2
        assert {signal.symbol for signal in snapshot} == set(_ACTIVE_SYMBOLS)

        eligible_fold_zero = tuple(
            candidate.config_id
            for candidate in wrapped_result.folds[0].selection_trace.candidates
            if not candidate.rejected
        )
        expected_eligible = (
            ("S1-03", "S1-10") if strategy == "S1" else ("S2-01", "S2-09")
        )
        assert eligible_fold_zero == expected_eligible
        assert tuple(
            fold.selection_trace.selected_config_id for fold in wrapped_result.folds
        ) == ((expected_eligible[0],) + (None,) * 7)
        assert all(
            len(wrapped_result.concatenated_oos_ledgers[scenario]) == 2
            for scenario in _SCENARIOS
        )

        plain_results[strategy] = plain_result
        wrapped_results[strategy] = wrapped_result
        capture_sinks[strategy] = sink
        captured_signals[strategy] = snapshot

    all_oos_trades = tuple(
        trade
        for result in wrapped_results.values()
        for scenario in _SCENARIOS
        for trade in result.concatenated_oos_ledgers[scenario]
    )
    assert len(all_oos_trades) == 12
    # This is the ROB-962 regression: equality with 0.0 is insufficient.
    # The actual engine records consumed by H5 must carry exact float zero.
    assert all(type(trade.funding_bps) is float for trade in all_oos_trades)
    assert all(trade.funding_bps.hex() == (0.0).hex() for trade in all_oos_trades)

    concurrency = compute_signal_concurrency(captured_signals)
    for strategy in ("S1", "S2"):
        row = concurrency.per_strategy_by_name[strategy]
        assert (row.numerator, row.denominator, row.rate) == (1, 1, 1.0)
        assert row.distinct_symbol_count_histogram == {1: 0, 2: 1, 3: 0, 4: 0}

    # Actual full-window 12-config x 365-day PBO is built inside each call.
    strategies_evidence = {
        strategy: build_strategy_evidence(
            strategy=strategy,
            walkforward_result=wrapped_results[strategy],
            capture_sink=capture_sinks[strategy],
            signal_concurrency_evidence=concurrency.per_strategy_by_name[strategy],
            bars_1m=bars_1m,
            funding_sidecars=funding_sidecars,
            gap_ranges=gap_ranges,
        )
        for strategy in ("S1", "S2")
    }
    assert set(strategies_evidence) == {"S1", "S2"}
    for evidence in strategies_evidence.values():
        assert evidence["capture_valid"] is True
        assert evidence["scenarios"]["primary_stress"].trade_count == 2
        assert evidence["pbo"].config_count == 12
        assert evidence["pbo"].day_count == 365
        assert evidence["pbo"].reason_codes == ("ambiguous_pbo_ranking",)

    frozen_envelope = build_production_frozen_campaign_envelope()
    full_campaign_payload = frozen_envelope.to_dict()
    full_campaign_hash = frozen_envelope.full_campaign_hash()
    campaign_run_id = derive_campaign_run_id(full_campaign_hash)
    frozen_experiment_ids = tuple(full_campaign_payload["experiment_ids"])
    experiment_id_by_config = dict(
        zip(CANONICAL_ROW_ORDER, frozen_experiment_ids, strict=True)
    )

    attempt_evidence = []
    for strategy in ("S1", "S2"):
        for summary in summarize_config_attempts_for_h6(wrapped_results[strategy]):
            attempt_evidence.append(
                _summary_to_attempt_evidence(
                    summary,
                    strategy_key=_STRATEGY_KEYS[strategy],
                    experiment_id=experiment_id_by_config[summary.config_id],
                    full_campaign_hash=full_campaign_hash,
                    campaign_run_id=campaign_run_id,
                )
            )
    assert len(attempt_evidence) == 24
    assert len({row.attempt_key.experiment_id for row in attempt_evidence}) == 24

    accounting_report = _campaign_report_from_attempts(
        attempt_evidence,
        campaign_run_id=campaign_run_id,
        expected_experiment_ids=frozen_experiment_ids,
    )
    assert accounting_report.verdict == "complete"
    assert accounting_report.status_counts == {
        "completed": 4,
        "rejected": 20,
        "crashed": 0,
        "timeout": 0,
    }

    scorecard = build_scorecard(
        full_campaign_hash=full_campaign_hash,
        full_campaign_payload=full_campaign_payload,
        campaign_run_id=campaign_run_id,
        dataset_manifest_hash=frozen_envelope.dataset_manifest_hash,
        signal_manifest_hash=frozen_envelope.signal_manifest_hash,
        accounting_report=accounting_report.model_dump(),
        attempt_evidence=[row.model_dump() for row in attempt_evidence],
        walkforward_results=wrapped_results,
        strategies=strategies_evidence,
    )
    payload = scorecard["scorecard_payload"]
    assert set(payload["strategies"]) == {"S1", "S2"}
    assert payload["campaign_verdict"] == "incomplete"
    assert payload["lineage"]["accounting_complete"] is True
    assert payload["lineage"]["accounting_performance_usable"] is False
    assert scorecard["scorecard_artifact_hash"] == canonical_sha256(payload)
    json.dumps(scorecard, allow_nan=False)

    markdown = render_markdown(scorecard)
    output_dir = tmp_path / "published-scorecard"
    staging_dir = stage_scorecard_files(scorecard, markdown, output_dir)
    staged_json = (staging_dir / "scorecard.json").read_bytes()
    staged_markdown = (staging_dir / "scorecard.md").read_bytes()
    json_path, markdown_path = publish_staged_scorecard(staging_dir, output_dir)

    assert (json_path, markdown_path) == (
        output_dir / "scorecard.json",
        output_dir / "scorecard.md",
    )
    assert not staging_dir.exists()
    assert json_path.read_bytes() == staged_json
    assert markdown_path.read_bytes() == staged_markdown
    assert json.loads(json_path.read_text(encoding="utf-8")) == scorecard
    assert markdown_path.read_text(encoding="utf-8") == markdown.rstrip("\n") + "\n"
    assert scorecard["scorecard_artifact_hash"] in markdown
