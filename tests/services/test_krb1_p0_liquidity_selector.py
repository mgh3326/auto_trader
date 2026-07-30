from __future__ import annotations

import ast
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.krb1_completion_manifest import (
    COMPLETION_MANIFEST_STREAM_ID,
    CompletionManifest,
    DbDailyBar,
    RawDailyBar,
    build_completion_manifest,
)
from app.services.krb1_metadata_authority import (
    METADATA_SNAPSHOT_STREAM_ID,
    MetadataAuthoritySnapshot,
    ProviderAuthorityClock,
    SymbolMetadata,
    compute_universe_metadata_hash,
)
from app.services.krb1_p0_liquidity_selector import (
    CandleRow,
    CompletedBarEvidence,
    QuoteTimestampCapture,
    ReferencePriceExceptionRecord,
    SelectorInput,
    UniverseRow,
    select_krb1_p0_liquidity_candidates,
    tick_floor_exact,
)

pytestmark = pytest.mark.unit

AS_OF = dt.date(2026, 7, 29)
TARGET = dt.date(2026, 7, 30)
KST = dt.timezone(dt.timedelta(hours=9))
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
METADATA_PUBLISHED_AT = dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST)
METADATA_RETRIEVED_AT = dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST)
REFERENCE_PUBLISHED_AT = dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST)
REFERENCE_RETRIEVED_AT = dt.datetime(2026, 7, 29, 17, 30, tzinfo=KST)
INGESTED_AT = dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST)
OBSERVED_AT = dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST)
FINALIZED_AT = dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST)
SHA_STUB = "a" * 64
KIS_DAILY_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
KIS_DAILY_TR_ID = "FHKST03010100"


def _universe(symbol: str, market: str) -> UniverseRow:
    return UniverseRow(
        symbol=symbol,
        name=f"name-{symbol}",
        exchange=market,
        is_active=True,
        security_type="STOCK",
        is_common_share=True,
        listing_status="ACTIVE",
        list_date=dt.date(2020, 1, 2),
        krx_trading_suspended=False,
        metadata_source="toss_openapi",
        metadata_as_of=METADATA_RETRIEVED_AT,
    )


def _candle(symbol: str, *, value: int, close: int) -> CandleRow:
    return CandleRow(
        session_date=AS_OF,
        symbol=symbol,
        venue="KRX",
        open=close - 100,
        high=close + 100,
        low=close - 200,
        close=close,
        volume=1_000,
        value=value,
        source="kis",
        ingested_at=INGESTED_AT,
    )


def _reference(symbol: str, *, is_exception: bool = False):
    return ReferencePriceExceptionRecord(
        symbol=symbol,
        effective_session=TARGET,
        is_exception=is_exception,
        source="krx_official_base_price",
        published_at=REFERENCE_PUBLISHED_AT,
        retrieved_at=REFERENCE_RETRIEVED_AT,
        raw_reference_price="10000",
        raw_reason_code="NORMAL",
        raw_payload_sha256=SHA_STUB,
    )


def _completed(candle: CandleRow) -> CompletedBarEvidence:
    return CompletedBarEvidence(
        symbol=candle.symbol,
        endpoint=KIS_DAILY_ENDPOINT,
        tr_id=KIS_DAILY_TR_ID,
        raw_business_date="20260729",
        raw_close=str(candle.close),
        raw_volume=str(candle.volume),
        raw_value=str(candle.value),
        observed_at=OBSERVED_AT,
    )


def _quote(symbol: str) -> QuoteTimestampCapture:
    return QuoteTimestampCapture(
        symbol=symbol,
        endpoint="/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        raw_symbol=symbol,
        raw_business_date="20260729",
        raw_execution_time="153000",
        raw_last_price="10000",
        captured_at=dt.datetime(2026, 7, 29, 15, 40, tzinfo=KST),
    )


def _provider_clock() -> ProviderAuthorityClock:
    """Provider-origin clock. The wired Toss surface sends none (A1); tests inject
    a declared one so the *other* gates stay reachable."""
    return ProviderAuthorityClock(
        published_at=METADATA_PUBLISHED_AT,
        published_at_field="publishedAt",
        published_at_raw=METADATA_PUBLISHED_AT.isoformat(),
        effective_session=AS_OF,
        effective_session_field="effectiveSession",
        effective_session_raw=AS_OF.isoformat(),
    )


def _metadata_snapshot(
    market: str, rows: tuple[UniverseRow, ...]
) -> MetadataAuthoritySnapshot:
    market_rows = tuple(
        SymbolMetadata(
            symbol=row.symbol,
            exchange=row.exchange,
            security_type=row.security_type,
            is_common_share=row.is_common_share,
            listing_status=row.listing_status,
            list_date=row.list_date,
            krx_trading_suspended=row.krx_trading_suspended,
        )
        for row in rows
        if row.exchange == market
    )
    return MetadataAuthoritySnapshot(
        source="toss_openapi",
        market=market,
        universe_metadata_hash=compute_universe_metadata_hash(market, market_rows),
        raw_payload_sha256=SHA_STUB,
        raw_payload_bytes=4_096,
        symbol_count=len(market_rows),
        provider_clock=_provider_clock(),
        retrieved_at=METADATA_RETRIEVED_AT,
        stream_id=METADATA_SNAPSHOT_STREAM_ID,
        chain_index=2,
        chain_hash=SHA_STUB,
    )


def _manifest(
    market: str,
    universe: tuple[UniverseRow, ...],
    candles: tuple[CandleRow, ...],
) -> CompletionManifest:
    market_symbols = [row.symbol for row in universe if row.exchange == market]
    market_candles = [row for row in candles if row.symbol in set(market_symbols)]
    manifest = build_completion_manifest(
        market=market,
        session_date=AS_OF,
        universe_symbols=market_symbols,
        raw_bars=[
            RawDailyBar(
                symbol=row.symbol,
                endpoint=KIS_DAILY_ENDPOINT,
                tr_id=KIS_DAILY_TR_ID,
                raw_business_date="20260729",
                raw_open=str(row.open),
                raw_high=str(row.high),
                raw_low=str(row.low),
                raw_close=str(row.close),
                raw_volume=str(row.volume),
                raw_value=str(row.value),
                observed_at=OBSERVED_AT,
                rt_cd="0",
            )
            for row in market_candles
        ],
        db_bars=[
            DbDailyBar(
                symbol=row.symbol,
                session_date=row.session_date,
                venue=row.venue,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                value=row.value,
            )
            for row in market_candles
        ],
        finalized_at=FINALIZED_AT,
        decision_at=DECISION_AT,
    )
    return replace(
        manifest,
        stream_id=COMPLETION_MANIFEST_STREAM_ID,
        chain_index=2,
        chain_hash=SHA_STUB,
    )


def _with_evidence(selector_input: SelectorInput, **overrides: object) -> SelectorInput:
    """Rebuild the derived metadata snapshot and manifest for changed inputs."""
    updated = replace(selector_input, **overrides)  # type: ignore[arg-type]
    return replace(
        updated,
        metadata_snapshots=tuple(
            _metadata_snapshot(market, updated.universe_rows)
            for market in ("KOSPI", "KOSDAQ")
        ),
        completion_manifests=tuple(
            _manifest(market, updated.universe_rows, updated.candle_rows)
            for market in ("KOSPI", "KOSDAQ")
        ),
    )


def _base_input() -> SelectorInput:
    universe = (
        _universe("000001", "KOSPI"),
        _universe("000002", "KOSPI"),
        _universe("100001", "KOSDAQ"),
        _universe("100002", "KOSDAQ"),
    )
    candles = (
        _candle("000001", value=2_000, close=10_003),
        _candle("000002", value=1_000, close=20_003),
        _candle("100001", value=3_000, close=30_003),
        _candle("100002", value=4_000, close=40_003),
    )
    return SelectorInput(
        as_of_session=AS_OF,
        target_session=TARGET,
        decision_at=DECISION_AT,
        expected_universe_counts={"KOSPI": 2, "KOSDAQ": 2},
        universe_rows=universe,
        candle_rows=candles,
        reference_price_exception_records=tuple(
            _reference(row.symbol) for row in universe
        ),
        completed_bar_evidence=tuple(_completed(row) for row in candles),
        quote_timestamp_evidence=tuple(_quote(row.symbol) for row in universe),
        metadata_snapshots=tuple(
            _metadata_snapshot(market, universe) for market in ("KOSPI", "KOSDAQ")
        ),
        completion_manifests=tuple(
            _manifest(market, universe, candles) for market in ("KOSPI", "KOSDAQ")
        ),
    )


def _selected_by_market(result: dict[str, object]) -> dict[str, dict[str, object]]:
    selected = result["selected_candidates"]
    assert isinstance(selected, list)
    return {str(row["market"]): row for row in selected}


def _assert_fail_closed(result: dict[str, object], reason: str) -> None:
    assert result["status"] == "fail_closed"
    assert result["selected_candidates"] == []
    assert result["fallback_used"] is False
    reasons = result["fail_close_reasons"]
    assert isinstance(reasons, list)
    assert any(item["reason"] == reason for item in reasons), reasons


def test_success_is_deterministic_and_uses_integer_limit_math() -> None:
    selector_input = _base_input()

    first = select_krb1_p0_liquidity_candidates(selector_input)
    second = select_krb1_p0_liquidity_candidates(selector_input)

    assert first == second
    assert first["status"] == "selected", first["fail_close_reasons"]
    selected = _selected_by_market(first)
    assert selected["KOSPI"]["universe_row"]["symbol"] == "000001"
    assert selected["KOSDAQ"]["universe_row"]["symbol"] == "100002"
    calculation = selected["KOSPI"]["limit_price_calculation"]
    assert calculation == {
        "expression": "(85 * completed_close) // 100",
        "completed_close": 10_003,
        "numerator": 850_255,
        "raw_limit_price": 8_502,
        "tick_floor_expression": "(raw // tick) * tick",
        "tick": 10,
        "limit_price": 8_500,
        "integer_arithmetic_only": True,
    }


def test_selected_candidate_carries_gate_proof_provenance() -> None:
    result = select_krb1_p0_liquidity_candidates(_base_input())

    assert result["status"] == "selected", result["fail_close_reasons"]
    candidate = _selected_by_market(result)["KOSPI"]
    snapshot = candidate["metadata_authority_snapshot"]
    manifest = candidate["completion_manifest"]
    assert isinstance(snapshot, dict)
    assert isinstance(manifest, dict)
    assert snapshot["stream_id"] == METADATA_SNAPSHOT_STREAM_ID
    assert manifest["stream_id"] == COMPLETION_MANIFEST_STREAM_ID
    assert manifest["reconciled_count"] == manifest["symbol_count"]
    assert result["decision_at"] == DECISION_AT.isoformat()
    contract = result["evidence_clock_contract"]
    assert isinstance(contract, dict)
    assert contract["late_backfill_is_not_proof_of_state_at_decision_at"] is True


def test_value_tie_breaks_by_symbol_ascending() -> None:
    selector_input = _base_input()
    candles = tuple(
        replace(row, value=9_000) if row.symbol in {"000001", "000002"} else row
        for row in selector_input.candle_rows
    )

    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(
            selector_input,
            candle_rows=candles,
            completed_bar_evidence=tuple(_completed(row) for row in candles),
        )
    )

    assert result["status"] == "selected", result["fail_close_reasons"]
    assert _selected_by_market(result)["KOSPI"]["universe_row"]["symbol"] == "000001"


def test_markets_rank_independently_without_cross_contamination() -> None:
    selector_input = _base_input()
    candles = tuple(
        replace(row, value=999_999_999)
        if row.symbol == "000002"
        else replace(row, value=1)
        if row.symbol == "100002"
        else row
        for row in selector_input.candle_rows
    )

    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(
            selector_input,
            candle_rows=candles,
            completed_bar_evidence=tuple(_completed(row) for row in candles),
        )
    )

    selected = _selected_by_market(result)
    assert selected["KOSPI"]["universe_row"]["symbol"] == "000002"
    assert selected["KOSDAQ"]["universe_row"]["symbol"] == "100001"


def test_full_universe_snapshot_coverage_shortfall_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            expected_universe_counts={"KOSPI": 3, "KOSDAQ": 2},
        )
    )
    _assert_fail_closed(result, "full_universe_snapshot_coverage_mismatch")


def test_completed_session_row_absence_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            candle_rows=tuple(
                row for row in selector_input.candle_rows if row.symbol != "000002"
            ),
        )
    )
    _assert_fail_closed(result, "completed_session_full_universe_coverage_unproven")


def test_nonselected_rows_without_raw_completion_evidence_fail_closed() -> None:
    selector_input = _base_input()
    candles = tuple(
        replace(
            row,
            ingested_at=dt.datetime(2026, 7, 29, 7, 40, tzinfo=KST),
        )
        if row.symbol in {"000002", "100001"}
        else row
        for row in selector_input.candle_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            candle_rows=candles,
            completed_bar_evidence=tuple(
                row
                for row in selector_input.completed_bar_evidence
                if row.symbol in {"000001", "100002"}
            ),
        )
    )
    _assert_fail_closed(result, "full_universe_raw_daily_completion_unproven")


def test_forming_daily_raw_observation_before_cutoff_fails_closed() -> None:
    selector_input = _base_input()
    completed = tuple(
        replace(
            row,
            observed_at=dt.datetime(2026, 7, 29, 14, 0, tzinfo=KST),
        )
        if row.symbol == "000002"
        else row
        for row in selector_input.completed_bar_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completed_bar_evidence=completed)
    )
    _assert_fail_closed(result, "full_universe_raw_daily_completion_unproven")


def test_daily_raw_observation_after_decision_at_fails_closed() -> None:
    """Evidence observed after the decision cannot prove the decision."""
    selector_input = _base_input()
    completed = tuple(
        replace(row, observed_at=dt.datetime(2026, 7, 29, 19, 0, tzinfo=KST))
        for row in selector_input.completed_bar_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completed_bar_evidence=completed)
    )
    _assert_fail_closed(result, "full_universe_raw_daily_completion_unproven")


def test_metadata_null_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, is_common_share=None) if row.symbol == "000001" else row
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "active_universe_market_product_metadata_missing")


def test_stale_metadata_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(
            row,
            metadata_as_of=dt.datetime(2026, 7, 28, 8, 47, tzinfo=KST),
        )
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_not_authoritative_as_of_selection_session")


def test_metadata_as_of_after_decision_at_fails_closed() -> None:
    """🔴 The upper bound: a 07-30 master cannot justify a 07-29 decision."""
    selector_input = _base_input()
    universe = tuple(
        replace(row, metadata_as_of=dt.datetime(2026, 7, 30, 8, 47, tzinfo=KST))
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_as_of_after_decision_at")


def test_naive_metadata_clock_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, metadata_as_of=dt.datetime(2026, 7, 29, 17, 0))
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_as_of_after_decision_at")


def test_missing_metadata_authority_snapshot_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_snapshots=())
    )
    _assert_fail_closed(result, "authoritative_metadata_snapshot_missing")


def test_metadata_snapshot_published_after_decision_at_fails_closed() -> None:
    selector_input = _base_input()
    late = dt.datetime(2026, 7, 30, 9, 0, tzinfo=KST)
    snapshots = tuple(
        replace(
            snapshot,
            provider_clock=replace(
                _provider_clock(), published_at=late, published_at_raw=late.isoformat()
            ),
            retrieved_at=late,
        )
        for snapshot in selector_input.metadata_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_snapshots=snapshots)
    )
    _assert_fail_closed(
        result, "metadata_snapshot_provider_published_after_decision_at"
    )


def test_metadata_snapshot_without_provider_clock_fails_closed() -> None:
    """🔴 A1: our retrieval clock cannot stand in for the provider's."""
    selector_input = _base_input()
    snapshots = tuple(
        replace(snapshot, provider_clock=None)
        for snapshot in selector_input.metadata_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_snapshots=snapshots)
    )
    _assert_fail_closed(result, "metadata_snapshot_provider_authority_clock_missing")


def test_metadata_snapshot_stale_provider_session_fails_closed() -> None:
    selector_input = _base_input()
    snapshots = tuple(
        replace(
            snapshot,
            provider_clock=replace(
                _provider_clock(),
                effective_session=dt.date(2026, 7, 28),
                effective_session_raw="2026-07-28",
            ),
        )
        for snapshot in selector_input.metadata_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_snapshots=snapshots)
    )
    _assert_fail_closed(
        result,
        "metadata_snapshot_provider_effective_session_before_selection_session",
    )


def test_metadata_snapshot_universe_hash_mismatch_fails_closed() -> None:
    """A snapshot cannot be reused after the metadata rows changed underneath it."""
    selector_input = _base_input()
    universe = tuple(
        replace(row, krx_trading_suspended=False, security_type="STOCK")
        if row.symbol != "000001"
        else replace(
            row, name="renamed", listing_status="ACTIVE", list_date=dt.date(2019, 1, 2)
        )
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_snapshot_universe_hash_mismatch")


def test_missing_completion_manifest_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completion_manifests=())
    )
    _assert_fail_closed(result, "completion_manifest_missing")


def test_completion_manifest_without_chain_provenance_fails_closed() -> None:
    selector_input = _base_input()
    manifests = tuple(
        replace(manifest, stream_id=None, chain_index=None, chain_hash=None)
        for manifest in selector_input.completion_manifests
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completion_manifests=manifests)
    )
    _assert_fail_closed(result, "completion_manifest_append_only_provenance_missing")


def test_completion_manifest_finalized_after_decision_at_fails_closed() -> None:
    selector_input = _base_input()
    manifests = tuple(
        replace(manifest, finalized_at=dt.datetime(2026, 7, 29, 23, 0, tzinfo=KST))
        for manifest in selector_input.completion_manifests
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completion_manifests=manifests)
    )
    _assert_fail_closed(result, "completion_manifest_finalized_after_decision_at")


def test_all_market_rows_suspended_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, krx_trading_suspended=True) if row.exchange == "KOSPI" else row
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "no_pre_reference_eligible_standard_common_stock")


def test_all_market_rows_newly_listed_on_target_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, list_date=TARGET) if row.exchange == "KOSPI" else row
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "no_pre_reference_eligible_standard_common_stock")


def test_reference_price_exception_unprovable_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            reference_price_exception_records=tuple(
                row
                for row in selector_input.reference_price_exception_records
                if row.symbol != "000001"
            ),
        )
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


def test_reference_exception_boolean_without_raw_provenance_fails_closed() -> None:
    selector_input = _base_input()
    references = tuple(
        replace(row, raw_reference_price=None, raw_reason_code=None)
        if row.symbol == "000001"
        else row
        for row in selector_input.reference_price_exception_records
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_price_exception_records=references)
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


def test_reference_effective_session_other_than_target_fails_closed() -> None:
    selector_input = _base_input()
    references = tuple(
        replace(row, effective_session=AS_OF)
        for row in selector_input.reference_price_exception_records
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_price_exception_records=references)
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


def test_reference_published_after_decision_at_fails_closed() -> None:
    selector_input = _base_input()
    references = tuple(
        replace(
            row,
            published_at=dt.datetime(2026, 7, 30, 8, 0, tzinfo=KST),
            retrieved_at=dt.datetime(2026, 7, 30, 8, 30, tzinfo=KST),
        )
        for row in selector_input.reference_price_exception_records
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_price_exception_records=references)
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


def test_unwired_reference_source_reason_fails_closed_even_with_records() -> None:
    """An unavailable source blocks regardless of what a caller supplies."""
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            reference_source_unavailable_reason=(
                "authoritative_target_session_reference_exception_source_not_wired"
            ),
        )
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


def test_proven_reference_exception_excludes_symbol_before_rank() -> None:
    selector_input = _base_input()
    references = tuple(
        replace(row, is_exception=True) if row.symbol in {"000001", "100002"} else row
        for row in selector_input.reference_price_exception_records
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_price_exception_records=references)
    )
    selected = _selected_by_market(result)
    assert selected["KOSPI"]["universe_row"]["symbol"] == "000002"
    assert selected["KOSDAQ"]["universe_row"]["symbol"] == "100001"


def test_wrapper_timestamp_only_does_not_prove_raw_timestamp() -> None:
    from app.services.krb1_quote_timestamp_capture import WrapperFreshnessAnnotation

    selector_input = _base_input()
    quotes = tuple(
        replace(
            row,
            raw_business_date=None,
            raw_execution_time=None,
            wrapper=WrapperFreshnessAnnotation(
                price_as_of="2026-07-29T15:30:00+09:00",
                price_freshness="fresh",
                is_stale_price=False,
            ),
        )
        if row.symbol == "000001"
        else row
        for row in selector_input.quote_timestamp_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, quote_timestamp_evidence=quotes)
    )
    _assert_fail_closed(result, "selected_quote_actual_raw_timestamp_unproven")


def test_quote_raw_timestamp_after_decision_at_fails_closed() -> None:
    selector_input = _base_input()
    quotes = tuple(
        replace(row, raw_execution_time="190000")
        for row in selector_input.quote_timestamp_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, quote_timestamp_evidence=quotes)
    )
    _assert_fail_closed(result, "selected_quote_actual_raw_timestamp_unproven")


def test_completed_close_raw_mismatch_fails_closed() -> None:
    selector_input = _base_input()
    evidence = tuple(
        replace(row, raw_close="999999") if row.symbol == "000001" else row
        for row in selector_input.completed_bar_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completed_bar_evidence=evidence)
    )
    _assert_fail_closed(result, "selected_completed_bar_raw_evidence_mismatch")


def test_naive_decision_clock_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, decision_at=dt.datetime(2026, 7, 29, 18, 0))
    )
    _assert_fail_closed(result, "decision_at_must_be_timezone_aware")


def test_decision_before_completed_session_cutoff_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            decision_at=dt.datetime(2026, 7, 29, 14, 0, tzinfo=KST),
        )
    )
    _assert_fail_closed(result, "decision_at_before_completed_session_cutoff")


def test_decision_after_target_session_open_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            decision_at=dt.datetime(2026, 7, 30, 9, 30, tzinfo=KST),
        )
    )
    _assert_fail_closed(result, "decision_at_not_before_target_session_open")


def test_tick_tables_match_canonical_fixture_and_boundaries() -> None:
    fixture = json.loads(
        Path(
            "tests/fixtures/krb1_c_stress/p0_1_standard_stock_tick_tables.json"
        ).read_text()
    )
    for market in ("KOSPI", "KOSDAQ"):
        for band in fixture["markets"][market]["bands"]:
            lower = band["lower"]
            upper = band["upper_exclusive"]
            tick = band["tick"]
            assert tick_floor_exact(lower, market) == (lower // tick) * tick
            if upper is not None:
                price = upper - 1
                assert tick_floor_exact(price, market) == (price // tick) * tick


def test_selector_source_has_no_float_literals_or_true_division() -> None:
    source = Path("app/services/krb1_p0_liquidity_selector.py").read_text()
    tree = ast.parse(source)

    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    ]


@pytest.mark.parametrize("bad_price", [1.0, -1, True])
def test_tick_floor_rejects_float_negative_and_bool(bad_price: object) -> None:
    with pytest.raises(ValueError):
        tick_floor_exact(bad_price, "KOSPI")  # type: ignore[arg-type]
