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
METADATA_AS_OF = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
REFERENCE_AS_OF = dt.datetime(2026, 7, 30, 8, 0, tzinfo=KST)
INGESTED_AT = dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST)


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
        metadata_as_of=METADATA_AS_OF,
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
        raw_reference_price="10000",
        raw_reason_code="NORMAL",
    )


def _completed(candle: CandleRow) -> CompletedBarEvidence:
    return CompletedBarEvidence(
        symbol=candle.symbol,
        endpoint="/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        tr_id="FHKST03010100",
        raw_business_date="20260729",
        raw_close=str(candle.close),
        raw_volume=str(candle.volume),
        raw_value=str(candle.value),
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
        expected_universe_counts={"KOSPI": 2, "KOSDAQ": 2},
        universe_rows=universe,
        candle_rows=candles,
        reference_exception_evidence=tuple(_reference(row.symbol) for row in universe),
        completed_bar_evidence=tuple(_completed(row) for row in candles),
        quote_timestamp_evidence=tuple(_quote(row.symbol) for row in universe),
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
        replace(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_not_authoritative_as_of_selection_session")


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
