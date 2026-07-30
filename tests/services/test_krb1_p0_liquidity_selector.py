from __future__ import annotations

import ast
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.krb1_p0_liquidity_selector import (
    CandleRow,
    CompletedBarEvidence,
    ExternalUniverseDenominator,
    MetadataAuthoritySnapshot,
    QuoteTimestampEvidence,
    ReferenceExceptionEvidence,
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
METADATA_AS_OF = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
REFERENCE_AS_OF = dt.datetime(2026, 7, 30, 8, 0, tzinfo=KST)
REFERENCE_PUBLISHED_AT = dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST)
REFERENCE_RETRIEVED_AT = dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST)
INGESTED_AT = dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST)
CAPTURED_AT = dt.datetime(2026, 7, 29, 15, 35, tzinfo=KST)
SNAPSHOT_RETRIEVED_AT = dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST)


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
        db_sync_source="toss_openapi",
        db_sync_observed_at=METADATA_AS_OF,
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
    return ReferenceExceptionEvidence(
        symbol=symbol,
        effective_session=TARGET,
        is_exception=is_exception,
        source="krx_official_base_price",
        source_as_of=REFERENCE_AS_OF,
        published_at=REFERENCE_PUBLISHED_AT,
        retrieved_at=REFERENCE_RETRIEVED_AT,
        raw_reference_price="10000",
        raw_reason_code="NORMAL",
    )


def _completed(candle: CandleRow) -> CompletedBarEvidence:
    return CompletedBarEvidence(
        symbol=candle.symbol,
        endpoint="/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        tr_id="FHKST03010100",
        raw_symbol=candle.symbol,
        raw_business_date="20260729",
        raw_close=str(candle.close),
        raw_volume=str(candle.volume),
        raw_value=str(candle.value),
        observed_at=INGESTED_AT,
    )


def _quote(symbol: str) -> QuoteTimestampEvidence:
    return QuoteTimestampEvidence(
        symbol=symbol,
        endpoint="/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        raw_symbol=symbol,
        raw_business_date="20260729",
        raw_execution_time="153000",
        raw_last_price="10000",
        captured_at=CAPTURED_AT,
    )


def _metadata_snapshot(market: str, count: int) -> MetadataAuthoritySnapshot:
    return MetadataAuthoritySnapshot(
        source="toss_openapi",
        market=market,
        symbol_count=count,
        provider_published_at=dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST),
        provider_effective_session=AS_OF,
        retrieved_at=SNAPSHOT_RETRIEVED_AT,
        raw_payload_sha256="a" * 64,
    )


def _external_denominator(market: str, count: int) -> ExternalUniverseDenominator:
    return ExternalUniverseDenominator(
        market=market,
        session_date=AS_OF,
        source="krx_official_listed_count",
        listed_count=count,
        published_at=dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST),
        retrieved_at=dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST),
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
        reference_exception_evidence=tuple(_reference(row.symbol) for row in universe),
        completed_bar_evidence=tuple(_completed(row) for row in candles),
        quote_timestamp_evidence=tuple(_quote(row.symbol) for row in universe),
        metadata_authority_snapshots=tuple(
            _metadata_snapshot(market, 2) for market in ("KOSPI", "KOSDAQ")
        ),
        external_universe_denominators=tuple(
            _external_denominator(market, 2) for market in ("KOSPI", "KOSDAQ")
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
    assert any(item["reason"] == reason for item in reasons)


def test_success_is_deterministic_and_uses_integer_limit_math() -> None:
    selector_input = _base_input()

    first = select_krb1_p0_liquidity_candidates(selector_input)
    second = select_krb1_p0_liquidity_candidates(selector_input)

    assert first == second
    assert first["status"] == "selected"
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


def test_value_tie_breaks_by_symbol_ascending() -> None:
    selector_input = _base_input()
    candles = tuple(
        replace(row, value=9_000) if row.symbol in {"000001", "000002"} else row
        for row in selector_input.candle_rows
    )
    completed = tuple(_completed(row) for row in candles)

    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            candle_rows=candles,
            completed_bar_evidence=completed,
        )
    )

    assert result["status"] == "selected"
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
        replace(
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
    _assert_fail_closed(result, "universe_denominator_disagrees_with_external_basis")


def test_f04_self_consistent_truncated_universe_fails_closed() -> None:
    """One row per market with matching count must not prove coverage."""
    selector_input = _base_input()
    truncated = tuple(
        row
        for row in selector_input.universe_rows
        if row.symbol in {"000001", "100001"}
    )
    candles = tuple(
        row for row in selector_input.candle_rows if row.symbol in {"000001", "100001"}
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            universe_rows=truncated,
            candle_rows=candles,
            expected_universe_counts={"KOSPI": 1, "KOSDAQ": 1},
            completed_bar_evidence=tuple(_completed(row) for row in candles),
            quote_timestamp_evidence=tuple(_quote(row.symbol) for row in truncated),
            reference_exception_evidence=tuple(
                _reference(row.symbol) for row in truncated
            ),
        )
    )

    _assert_fail_closed(result, "universe_denominator_disagrees_with_external_basis")
    gates = result["market_results"]["KOSPI"]["gates"]
    evidence = gates["universe_snapshot_coverage"]["evidence"]
    assert evidence["actual_count"] == 1
    assert evidence["external_count"] == 2
    assert evidence["same_transaction_count_is_not_independent_evidence"] is True


def test_no_external_denominator_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, external_universe_denominators=())
    )
    _assert_fail_closed(result, "universe_denominator_external_basis_unproven")


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
    _assert_fail_closed(result, "full_universe_raw_daily_local_match_unproven")


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
    _assert_fail_closed(result, "full_universe_raw_daily_local_match_unproven")


def test_daily_raw_observation_after_decision_at_fails_closed() -> None:
    selector_input = _base_input()
    completed = tuple(
        replace(row, observed_at=dt.datetime(2026, 7, 29, 19, 0, tzinfo=KST))
        for row in selector_input.completed_bar_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completed_bar_evidence=completed)
    )
    _assert_fail_closed(result, "full_universe_raw_daily_local_match_unproven")


def test_metadata_null_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, is_common_share=None) if row.symbol == "000001" else row
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "active_universe_market_product_metadata_missing")


def test_stale_db_sync_clock_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(
            row,
            db_sync_observed_at=dt.datetime(2026, 7, 28, 8, 47, tzinfo=KST),
        )
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_row_sync_clock_stale_for_selection_session")


def test_db_sync_clock_after_decision_at_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, db_sync_observed_at=dt.datetime(2026, 7, 30, 8, 47, tzinfo=KST))
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_row_sync_clock_after_decision_at")


def test_naive_db_sync_clock_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, db_sync_observed_at=dt.datetime(2026, 7, 29, 17, 0))
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_row_sync_clock_after_decision_at")


def test_row_sync_provenance_missing_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, db_sync_source=None) if row.symbol == "000001" else row
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_row_sync_provenance_missing")


def test_metadata_snapshot_source_not_authoritative_fails_closed() -> None:
    selector_input = _base_input()
    snapshots = tuple(
        replace(row, source="caller_claim")
        for row in selector_input.metadata_authority_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_authority_snapshots=snapshots)
    )
    _assert_fail_closed(result, "metadata_snapshot_source_not_authoritative")


def test_metadata_snapshot_provider_clock_missing_fails_closed() -> None:
    selector_input = _base_input()
    snapshots = tuple(
        replace(row, provider_published_at=None)
        for row in selector_input.metadata_authority_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_authority_snapshots=snapshots)
    )
    _assert_fail_closed(result, "metadata_snapshot_provider_authority_clock_missing")


def test_metadata_snapshot_published_after_decision_at_fails_closed() -> None:
    selector_input = _base_input()
    late = dt.datetime(2026, 7, 30, 8, 0, tzinfo=KST)
    snapshots = tuple(
        replace(row, provider_published_at=late, provider_effective_session=TARGET)
        for row in selector_input.metadata_authority_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_authority_snapshots=snapshots)
    )
    _assert_fail_closed(
        result, "metadata_snapshot_provider_published_after_decision_at"
    )


def test_metadata_snapshot_stale_provider_session_fails_closed() -> None:
    selector_input = _base_input()
    snapshots = tuple(
        replace(row, provider_effective_session=dt.date(2026, 7, 28))
        for row in selector_input.metadata_authority_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_authority_snapshots=snapshots)
    )
    _assert_fail_closed(
        result,
        "metadata_snapshot_provider_effective_session_before_selection_session",
    )


def test_metadata_snapshot_symbol_count_mismatch_fails_closed() -> None:
    selector_input = _base_input()
    snapshots = tuple(
        replace(row, symbol_count=99)
        for row in selector_input.metadata_authority_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_authority_snapshots=snapshots)
    )
    _assert_fail_closed(result, "metadata_snapshot_symbol_count_mismatch")


def test_all_market_rows_suspended_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, krx_trading_suspended=True) if row.exchange == "KOSPI" else row
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "no_pre_reference_eligible_standard_common_stock")


def test_all_market_rows_newly_listed_on_target_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, list_date=TARGET) if row.exchange == "KOSPI" else row
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "no_pre_reference_eligible_standard_common_stock")


def test_reference_price_exception_unprovable_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            reference_exception_evidence=tuple(
                row
                for row in selector_input.reference_exception_evidence
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
        for row in selector_input.reference_exception_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_exception_evidence=references)
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


def test_proven_reference_exception_excludes_symbol_before_rank() -> None:
    selector_input = _base_input()
    references = tuple(
        replace(row, is_exception=True) if row.symbol in {"000001", "100002"} else row
        for row in selector_input.reference_exception_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_exception_evidence=references)
    )
    selected = _selected_by_market(result)
    assert selected["KOSPI"]["universe_row"]["symbol"] == "000002"
    assert selected["KOSDAQ"]["universe_row"]["symbol"] == "100001"


def test_wrapper_timestamp_only_does_not_prove_raw_timestamp() -> None:
    selector_input = _base_input()
    quotes = tuple(
        replace(
            row,
            raw_business_date=None,
            raw_execution_time=None,
            wrapper_price_as_of="2026-07-29T15:30:00+09:00",
            wrapper_price_freshness="fresh",
        )
        if row.symbol == "000001"
        else row
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


def test_f02_request_context_symbol_cannot_stand_in_for_provider_identity() -> None:
    """Completion evidence without provider-origin symbol fails closed."""
    selector_input = _base_input()
    evidence = tuple(
        replace(row, raw_symbol=None) for row in selector_input.completed_bar_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completed_bar_evidence=evidence)
    )

    _assert_fail_closed(result, "full_universe_raw_daily_local_match_unproven")
    gates = result["market_results"]["KOSPI"]["gates"]
    assert (
        gates["completed_session_raw_completion"]["evidence"][
            "request_context_symbol_is_not_identity"
        ]
        is True
    )


def test_f02_mismatched_provider_identity_fails_closed() -> None:
    selector_input = _base_input()
    evidence = tuple(
        replace(row, raw_symbol="999999")
        for row in selector_input.completed_bar_evidence
    )
    _assert_fail_closed(
        select_krb1_p0_liquidity_candidates(
            replace(selector_input, completed_bar_evidence=evidence)
        ),
        "full_universe_raw_daily_local_match_unproven",
    )


def test_quote_raw_timestamp_after_decision_at_fails_closed() -> None:
    selector_input = _base_input()
    quotes = tuple(
        replace(row, captured_at=dt.datetime(2026, 7, 30, 8, 0, tzinfo=KST))
        for row in selector_input.quote_timestamp_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, quote_timestamp_evidence=quotes)
    )
    _assert_fail_closed(result, "selected_quote_actual_raw_timestamp_unproven")


def test_reference_published_after_decision_at_fails_closed() -> None:
    selector_input = _base_input()
    references = tuple(
        replace(
            row,
            published_at=dt.datetime(2026, 7, 30, 8, 0, tzinfo=KST),
            retrieved_at=dt.datetime(2026, 7, 30, 8, 30, tzinfo=KST),
        )
        for row in selector_input.reference_exception_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_exception_evidence=references)
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


def test_m05_session_order_failure_is_anchored() -> None:
    """M05: target_session must follow as_of_session."""
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, target_session=AS_OF)
    )
    _assert_fail_closed(result, "target_session_must_follow_as_of_session")

    earlier = select_krb1_p0_liquidity_candidates(
        replace(selector_input, target_session=dt.date(2026, 7, 28))
    )
    _assert_fail_closed(earlier, "target_session_must_follow_as_of_session")


def test_m06_duplicate_evidence_rows_are_anchored() -> None:
    """M06: duplicate symbol evidence must fail closed, per evidence family."""
    selector_input = _base_input()

    dup_candles = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            candle_rows=selector_input.candle_rows + (selector_input.candle_rows[0],),
        )
    )
    _assert_fail_closed(dup_candles, "duplicate_symbol_evidence_rows")

    dup_universe = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            universe_rows=selector_input.universe_rows
            + (selector_input.universe_rows[0],),
        )
    )
    _assert_fail_closed(dup_universe, "duplicate_symbol_evidence_rows")

    dup_quotes = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            quote_timestamp_evidence=selector_input.quote_timestamp_evidence
            + (selector_input.quote_timestamp_evidence[0],),
        )
    )
    _assert_fail_closed(dup_quotes, "duplicate_symbol_evidence_rows")


@pytest.mark.parametrize(
    "mutation",
    [
        {"close": 0},
        {"open": -1},
        {"high": 0},
        {"low": -5},
        {"volume": -1},
        {"value": -1},
        {"source": ""},
    ],
)
def test_m12_candle_row_integrity_is_anchored(mutation: dict[str, object]) -> None:
    """M12: a structurally invalid completed row must fail closed."""
    selector_input = _base_input()
    candles = tuple(
        replace(row, **mutation) if row.symbol == "000002" else row
        for row in selector_input.candle_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            candle_rows=candles,
            completed_bar_evidence=tuple(_completed(row) for row in candles),
        )
    )

    assert result["status"] == "fail_closed"
    reasons = {str(item["reason"]) for item in result["fail_close_reasons"]}
    assert reasons & {
        "completed_session_row_integrity_unproven",
        "full_universe_raw_daily_local_match_unproven",
    }, reasons


def test_m18_two_market_guard_is_anchored() -> None:
    """M18: one market alone can never produce a selection."""
    selector_input = _base_input()
    kospi_only = tuple(
        row for row in selector_input.universe_rows if row.exchange == "KOSPI"
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            universe_rows=kospi_only,
            candle_rows=tuple(
                row
                for row in selector_input.candle_rows
                if row.symbol in {item.symbol for item in kospi_only}
            ),
            expected_universe_counts={"KOSPI": 2, "KOSDAQ": 0},
            quote_timestamp_evidence=tuple(_quote(row.symbol) for row in kospi_only),
            completed_bar_evidence=tuple(
                _completed(row)
                for row in selector_input.candle_rows
                if row.symbol in {item.symbol for item in kospi_only}
            ),
            reference_exception_evidence=tuple(
                _reference(row.symbol) for row in kospi_only
            ),
            metadata_authority_snapshots=(_metadata_snapshot("KOSPI", 2),),
            external_universe_denominators=(_external_denominator("KOSPI", 2),),
        )
    )

    assert result["status"] == "fail_closed"
    assert result["selected_candidates"] == []
    reasons = {str(item["reason"]) for item in result["fail_close_reasons"]}
    assert "both_markets_must_be_fully_proven" in reasons


def test_m20_quote_provider_identity_check_is_anchored() -> None:
    """M20: the quote's raw symbol must match the selected symbol."""
    selector_input = _base_input()
    quotes = tuple(
        replace(row, raw_symbol="999999") if row.symbol == "000001" else row
        for row in selector_input.quote_timestamp_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, quote_timestamp_evidence=quotes)
    )
    _assert_fail_closed(result, "selected_quote_actual_raw_timestamp_unproven")

    absent = tuple(
        replace(row, raw_symbol=None) if row.symbol == "000001" else row
        for row in selector_input.quote_timestamp_evidence
    )
    _assert_fail_closed(
        select_krb1_p0_liquidity_candidates(
            replace(selector_input, quote_timestamp_evidence=absent)
        ),
        "selected_quote_actual_raw_timestamp_unproven",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"endpoint": "/uapi/domestic-stock/v1/quotations/inquire-price"},
        {"endpoint": ""},
        {"tr_id": "FHKST01010100"},
        {"tr_id": ""},
    ],
)
def test_m21_completed_endpoint_and_tr_checks_are_anchored(
    mutation: dict[str, object],
) -> None:
    """M21: completion evidence from the wrong endpoint/TR must not count."""
    selector_input = _base_input()
    evidence = tuple(
        replace(row, **mutation) for row in selector_input.completed_bar_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completed_bar_evidence=evidence)
    )
    _assert_fail_closed(result, "full_universe_raw_daily_local_match_unproven")


def test_m22_reference_authoritative_source_check_is_anchored() -> None:
    """M22: a non-authoritative reference source must fail closed."""
    selector_input = _base_input()
    for source in ("generic_screener", "operator_assertion", "", "toss_openapi"):
        records = tuple(
            replace(row, source=source)
            for row in selector_input.reference_exception_evidence
        )
        result = select_krb1_p0_liquidity_candidates(
            replace(selector_input, reference_exception_evidence=records)
        )
        _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


@pytest.mark.parametrize(
    "mutation",
    [
        {"endpoint": "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"},
        {"endpoint": ""},
        {"tr_id": "FHKST03010230"},
        {"tr_id": ""},
    ],
)
def test_m24_quote_endpoint_and_tr_checks_are_anchored(
    mutation: dict[str, object],
) -> None:
    """M24: quote evidence from the wrong endpoint/TR must not count."""
    selector_input = _base_input()
    quotes = tuple(
        replace(row, **mutation) for row in selector_input.quote_timestamp_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, quote_timestamp_evidence=quotes)
    )
    _assert_fail_closed(result, "selected_quote_actual_raw_timestamp_unproven")


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


def test_f03_reference_availability_clocks_absent_fails_closed() -> None:
    selector_input = _base_input()
    refs = tuple(
        replace(row, published_at=None, retrieved_at=None)
        for row in selector_input.reference_exception_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_exception_evidence=refs)
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


def test_f03_reference_published_after_decision_fails_closed() -> None:
    selector_input = _base_input()
    late_clock = dt.datetime(2026, 8, 30, 12, 0, tzinfo=KST)
    refs = tuple(
        replace(row, published_at=late_clock)
        for row in selector_input.reference_exception_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_exception_evidence=refs)
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


def test_f03_reference_published_after_retrieved_fails_closed() -> None:
    selector_input = _base_input()
    refs = tuple(
        replace(
            row,
            published_at=dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST),
            retrieved_at=dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST),
        )
        for row in selector_input.reference_exception_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_exception_evidence=refs)
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


def test_f03_quote_capture_absent_fails_closed() -> None:
    selector_input = _base_input()
    quotes = tuple(
        replace(row, captured_at=None)
        for row in selector_input.quote_timestamp_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, quote_timestamp_evidence=quotes)
    )
    _assert_fail_closed(result, "selected_quote_actual_raw_timestamp_unproven")


def test_f03_quote_raw_time_after_decision_fails_closed() -> None:
    selector_input = _base_input()
    quotes = tuple(
        replace(row, raw_execution_time="235959")
        for row in selector_input.quote_timestamp_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, quote_timestamp_evidence=quotes)
    )
    _assert_fail_closed(result, "selected_quote_actual_raw_timestamp_unproven")


def test_f03_quote_raw_time_after_captured_fails_closed() -> None:
    selector_input = _base_input()
    quotes = tuple(
        replace(
            row,
            raw_execution_time="173000",
            captured_at=dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST),
        )
        for row in selector_input.quote_timestamp_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, quote_timestamp_evidence=quotes)
    )
    _assert_fail_closed(result, "selected_quote_actual_raw_timestamp_unproven")


def test_f03_metadata_future_effective_session_fails_closed() -> None:
    selector_input = _base_input()
    snapshots = tuple(
        replace(row, provider_effective_session=dt.date(2026, 8, 30))
        for row in selector_input.metadata_authority_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_authority_snapshots=snapshots)
    )
    _assert_fail_closed(
        result,
        "metadata_snapshot_provider_effective_session_after_selection_session",
    )


def test_f04_disguised_self_transaction_denominator_fails_closed() -> None:
    selector_input = _base_input()
    denoms = tuple(
        replace(row, source="same_transaction_db_count")
        for row in selector_input.external_universe_denominators
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, external_universe_denominators=denoms)
    )
    _assert_fail_closed(result, "universe_denominator_source_not_authoritative")


def test_f04_denominator_session_mismatch_fails_closed() -> None:
    selector_input = _base_input()
    denoms = tuple(
        replace(row, session_date=dt.date(2026, 1, 1))
        for row in selector_input.external_universe_denominators
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, external_universe_denominators=denoms)
    )
    _assert_fail_closed(result, "universe_denominator_session_mismatch")


def test_f04_denominator_published_after_decision_fails_closed() -> None:
    selector_input = _base_input()
    late_clock = dt.datetime(2026, 8, 30, 12, 0, tzinfo=KST)
    denoms = tuple(
        replace(row, published_at=late_clock)
        for row in selector_input.external_universe_denominators
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, external_universe_denominators=denoms)
    )
    _assert_fail_closed(result, "universe_denominator_published_after_decision_at")


def test_f04_denominator_duplicate_fails_closed() -> None:
    selector_input = _base_input()
    denoms = selector_input.external_universe_denominators + (
        _external_denominator("KOSPI", 2),
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, external_universe_denominators=denoms)
    )
    _assert_fail_closed(result, "universe_denominator_external_basis_unproven")


def test_f03_quote_naive_capture_fails_closed() -> None:
    selector_input = _base_input()
    naive_capture = dt.datetime(2026, 7, 29, 15, 35)
    quotes = tuple(
        replace(row, captured_at=naive_capture)
        for row in selector_input.quote_timestamp_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, quote_timestamp_evidence=quotes)
    )
    _assert_fail_closed(result, "selected_quote_actual_raw_timestamp_unproven")


@pytest.mark.parametrize("order", ["valid_first", "future_first"])
def test_f03_metadata_valid_then_future_duplicate_fails_closed(order: str) -> None:
    selector_input = _base_input()
    kospi_valid = selector_input.metadata_authority_snapshots[0]
    kosdaq_valid = selector_input.metadata_authority_snapshots[1]
    kospi_future = replace(
        kospi_valid,
        provider_effective_session=dt.date(2026, 8, 30),
    )
    if order == "valid_first":
        snapshots = (kospi_valid, kospi_future, kosdaq_valid)
    else:
        snapshots = (kospi_future, kospi_valid, kosdaq_valid)

    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_authority_snapshots=snapshots)
    )
    _assert_fail_closed(result, "metadata_authority_snapshot_ambiguous_duplicate")


def test_f03_metadata_published_after_retrieved_fails_closed() -> None:
    selector_input = _base_input()
    snapshots = tuple(
        replace(
            row,
            provider_published_at=dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST),
            retrieved_at=dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST),
        )
        for row in selector_input.metadata_authority_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_authority_snapshots=snapshots)
    )
    _assert_fail_closed(result, "metadata_snapshot_published_after_retrieved")
