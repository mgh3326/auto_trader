"""Deterministic, fail-closed KR-B1 P0-3 liquidity selector.

This module is deliberately isolated from the normal-strategy DV20/M60 selector.
It is a pure decision service: callers inject database rows and raw upstream
evidence, and the same inputs always produce the same JSON-compatible result.
No clock, database, network, broker mutation, or fallback screener is reachable
from here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Literal

Market = Literal["KOSPI", "KOSDAQ"]
GateStatus = Literal["proven", "unprovable"]

MARKETS: tuple[Market, Market] = ("KOSPI", "KOSDAQ")
ACTIVE_LISTING_STATUS = "ACTIVE"
STANDARD_STOCK_SECURITY_TYPE = "STOCK"
KIS_PRICE_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-price"
KIS_PRICE_TR_ID = "FHKST01010100"
KIS_DAILY_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
KIS_DAILY_TR_ID = "FHKST03010100"

# No provider in the repository currently emits this evidence. The identifier is
# intentionally narrow so a caller-supplied generic screener cannot satisfy the
# gate. Wiring a real source is a separate reviewed change.
AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES = frozenset({"krx_official_base_price"})


@dataclass(frozen=True, slots=True)
class UniverseRow:
    symbol: str
    name: str
    exchange: str
    is_active: bool
    security_type: str | None
    is_common_share: bool | None
    listing_status: str | None
    list_date: dt.date | None
    krx_trading_suspended: bool | None
    metadata_source: str | None
    metadata_as_of: dt.datetime | None


@dataclass(frozen=True, slots=True)
class CandleRow:
    session_date: dt.date
    symbol: str
    venue: str
    open: int
    high: int
    low: int
    close: int
    volume: int
    value: int
    source: str
    ingested_at: dt.datetime


@dataclass(frozen=True, slots=True)
class ReferenceExceptionEvidence:
    symbol: str
    effective_session: dt.date
    is_exception: bool | None
    source: str
    source_as_of: dt.datetime
    raw_reference_price: str | None = None
    raw_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class CompletedBarEvidence:
    symbol: str
    endpoint: str
    tr_id: str
    raw_business_date: str | None
    raw_close: str | None
    raw_volume: str | None
    raw_value: str | None


@dataclass(frozen=True, slots=True)
class QuoteTimestampEvidence:
    symbol: str
    endpoint: str
    tr_id: str
    raw_symbol: str | None
    raw_business_date: str | None
    raw_execution_time: str | None
    raw_last_price: str | None
    wrapper_price_as_of: str | None = None
    wrapper_price_freshness: str | None = None


@dataclass(frozen=True, slots=True)
class SelectorInput:
    as_of_session: dt.date
    target_session: dt.date
    expected_universe_counts: dict[Market, int]
    universe_rows: tuple[UniverseRow, ...]
    candle_rows: tuple[CandleRow, ...]
    reference_exception_evidence: tuple[ReferenceExceptionEvidence, ...] = ()
    completed_bar_evidence: tuple[CompletedBarEvidence, ...] = ()
    quote_timestamp_evidence: tuple[QuoteTimestampEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class TickBand:
    lower: int
    upper_exclusive: int | None
    tick: int


# Exact integer transcription of
# tests/fixtures/krb1_c_stress/p0_1_standard_stock_tick_tables.json.
# KOSPI and KOSDAQ standard-stock tables are identical in that canonical fixture,
# but remain separate mappings so market isolation is explicit.
_STANDARD_STOCK_BANDS: tuple[TickBand, ...] = (
    TickBand(0, 2_000, 1),
    TickBand(2_000, 5_000, 5),
    TickBand(5_000, 20_000, 10),
    TickBand(20_000, 50_000, 50),
    TickBand(50_000, 200_000, 100),
    TickBand(200_000, 500_000, 500),
    TickBand(500_000, None, 1_000),
)
STANDARD_STOCK_TICK_TABLES: dict[Market, tuple[TickBand, ...]] = {
    "KOSPI": _STANDARD_STOCK_BANDS,
    "KOSDAQ": _STANDARD_STOCK_BANDS,
}


def tick_floor_exact(price: int, market: Market) -> int:
    """Floor ``price`` to the market tick using integer arithmetic only."""
    if type(price) is not int or price < 0:
        raise ValueError("price must be a non-negative int")
    try:
        bands = STANDARD_STOCK_TICK_TABLES[market]
    except KeyError as exc:
        raise ValueError(f"unsupported market: {market}") from exc
    for band in bands:
        if price >= band.lower and (
            band.upper_exclusive is None or price < band.upper_exclusive
        ):
            return (price // band.tick) * band.tick
    raise ValueError(f"no tick band for price={price} market={market}")


def _gate(
    status: GateStatus,
    reason: str,
    **evidence: object,
) -> dict[str, object]:
    return {"status": status, "reason": reason, "evidence": evidence}


def _iso(value: object) -> object:
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _iso(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_iso(item) for item in value]
    return value


def _row_dict(row: object) -> dict[str, object]:
    return _iso(asdict(row))  # type: ignore[return-value]


def _examples(symbols: list[str], limit: int = 20) -> list[str]:
    return symbols[:limit]


def _valid_hhmmss(value: str | None) -> bool:
    if value is None or len(value) != 6 or not value.isdigit():
        return False
    try:
        dt.time(int(value[0:2]), int(value[2:4]), int(value[4:6]))
    except ValueError:
        return False
    return True


def _parse_nonnegative_int_string(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    return int(value)


def _index_unique(
    rows: tuple[object, ...],
    *,
    symbol_getter: str = "symbol",
) -> tuple[dict[str, object], list[str]]:
    indexed: dict[str, object] = {}
    duplicates: list[str] = []
    for row in rows:
        symbol = str(getattr(row, symbol_getter))
        if symbol in indexed:
            duplicates.append(symbol)
        else:
            indexed[symbol] = row
    return indexed, sorted(set(duplicates))


def _metadata_missing_fields(row: UniverseRow) -> list[str]:
    required = {
        "security_type": row.security_type,
        "is_common_share": row.is_common_share,
        "listing_status": row.listing_status,
        "list_date": row.list_date,
        "krx_trading_suspended": row.krx_trading_suspended,
        "metadata_source": row.metadata_source,
        "metadata_as_of": row.metadata_as_of,
    }
    return [key for key, value in required.items() if value is None]


def _coverage_universe(rows: list[UniverseRow], as_of: dt.date) -> list[UniverseRow]:
    return [
        row
        for row in rows
        if row.is_active
        and row.listing_status == ACTIVE_LISTING_STATUS
        and row.list_date is not None
        and row.list_date <= as_of
    ]


def _pre_reference_eligible(
    rows: list[UniverseRow], target_session: dt.date
) -> list[UniverseRow]:
    return [
        row
        for row in rows
        if row.is_active
        and row.listing_status == ACTIVE_LISTING_STATUS
        and row.security_type == STANDARD_STOCK_SECURITY_TYPE
        and row.is_common_share is True
        and row.list_date is not None
        and row.list_date < target_session
        and row.krx_trading_suspended is False
    ]


def _completed_bar_gate(
    candle: CandleRow,
    evidence: CompletedBarEvidence | None,
) -> dict[str, object]:
    if evidence is None:
        return _gate(
            "unprovable",
            "selected_completed_bar_raw_evidence_missing",
            symbol=candle.symbol,
            required_endpoint=KIS_DAILY_ENDPOINT,
            required_tr_id=KIS_DAILY_TR_ID,
        )
    parsed_close = _parse_nonnegative_int_string(evidence.raw_close)
    parsed_volume = _parse_nonnegative_int_string(evidence.raw_volume)
    parsed_value = _parse_nonnegative_int_string(evidence.raw_value)
    expected_date = candle.session_date.strftime("%Y%m%d")
    matches = (
        evidence.symbol == candle.symbol
        and evidence.endpoint == KIS_DAILY_ENDPOINT
        and evidence.tr_id == KIS_DAILY_TR_ID
        and evidence.raw_business_date == expected_date
        and parsed_close == candle.close
        and parsed_volume == candle.volume
        and parsed_value == candle.value
    )
    if not matches:
        return _gate(
            "unprovable",
            "selected_completed_bar_raw_evidence_mismatch",
            candle=_row_dict(candle),
            raw_evidence=_row_dict(evidence),
        )
    return _gate(
        "proven",
        "selected_completed_bar_matches_kis_daily_raw",
        candle=_row_dict(candle),
        raw_evidence=_row_dict(evidence),
    )


def _quote_timestamp_gate(
    *,
    symbol: str,
    as_of_session: dt.date,
    evidence: QuoteTimestampEvidence | None,
) -> dict[str, object]:
    if evidence is None:
        return _gate(
            "unprovable",
            "selected_quote_raw_timestamp_missing",
            symbol=symbol,
            wrapper_fields_are_insufficient=True,
        )
    raw_date = evidence.raw_business_date
    raw_time = evidence.raw_execution_time
    expected_date = as_of_session.strftime("%Y%m%d")
    raw_fields_proven = (
        evidence.symbol == symbol
        and evidence.raw_symbol == symbol
        and evidence.endpoint == KIS_PRICE_ENDPOINT
        and evidence.tr_id == KIS_PRICE_TR_ID
        and raw_date == expected_date
        and _valid_hhmmss(raw_time)
        and raw_time is not None
        and raw_time >= "153000"
    )
    if not raw_fields_proven:
        return _gate(
            "unprovable",
            "selected_quote_actual_raw_timestamp_unproven",
            raw_evidence=_row_dict(evidence),
            wrapper_fields_are_insufficient=True,
            required_raw_fields=["stck_bsop_date", "stck_cntg_hour"],
            required_session=expected_date,
            required_time_at_or_after="153000",
        )
    return _gate(
        "proven",
        "selected_quote_actual_raw_timestamp_proven",
        raw_evidence=_row_dict(evidence),
    )


def select_krb1_p0_liquidity_candidates(
    selector_input: SelectorInput,
) -> dict[str, object]:
    """Select one KOSPI and one KOSDAQ row or return no result.

    Any unprovable gate makes the *entire* run fail closed. Diagnostic rank
    heads may be emitted as evidence, but ``selected_candidates`` is always
    empty unless every gate in both markets is proven.
    """
    _universe_index, universe_duplicates = _index_unique(selector_input.universe_rows)
    candle_index, candle_duplicates = _index_unique(selector_input.candle_rows)
    reference_index, reference_duplicates = _index_unique(
        selector_input.reference_exception_evidence
    )
    completed_index, completed_duplicates = _index_unique(
        selector_input.completed_bar_evidence
    )
    quote_index, quote_duplicates = _index_unique(
        selector_input.quote_timestamp_evidence
    )

    duplicate_evidence = {
        "universe": universe_duplicates,
        "candles": candle_duplicates,
        "reference_exception": reference_duplicates,
        "completed_bar": completed_duplicates,
        "quote_timestamp": quote_duplicates,
    }
    global_gates: dict[str, dict[str, object]] = {}
    if selector_input.target_session <= selector_input.as_of_session:
        global_gates["session_order"] = _gate(
            "unprovable",
            "target_session_must_follow_as_of_session",
            as_of_session=selector_input.as_of_session.isoformat(),
            target_session=selector_input.target_session.isoformat(),
        )
    else:
        global_gates["session_order"] = _gate(
            "proven",
            "session_order_valid",
            as_of_session=selector_input.as_of_session.isoformat(),
            target_session=selector_input.target_session.isoformat(),
        )
    duplicate_symbols = sorted(
        {symbol for symbols in duplicate_evidence.values() for symbol in symbols}
    )
    if duplicate_symbols:
        global_gates["unique_evidence_rows"] = _gate(
            "unprovable",
            "duplicate_symbol_evidence_rows",
            duplicates=duplicate_evidence,
        )
    else:
        global_gates["unique_evidence_rows"] = _gate(
            "proven", "evidence_rows_unique", duplicates=duplicate_evidence
        )

    market_results: dict[str, dict[str, object]] = {}
    pending_candidates: list[dict[str, object]] = []

    for market in MARKETS:
        rows = sorted(
            [row for row in selector_input.universe_rows if row.exchange == market],
            key=lambda row: row.symbol,
        )
        gates: dict[str, dict[str, object]] = {}
        expected_count = selector_input.expected_universe_counts.get(market)
        actual_count = len(rows)
        if expected_count is None or expected_count != actual_count:
            gates["universe_snapshot_coverage"] = _gate(
                "unprovable",
                "full_universe_snapshot_coverage_mismatch",
                expected_count=expected_count,
                actual_count=actual_count,
            )
        else:
            gates["universe_snapshot_coverage"] = _gate(
                "proven",
                "full_universe_snapshot_coverage_proven",
                expected_count=expected_count,
                actual_count=actual_count,
            )

        active_rows = [row for row in rows if row.is_active]
        missing_metadata = [
            (row.symbol, _metadata_missing_fields(row))
            for row in active_rows
            if _metadata_missing_fields(row)
        ]
        if missing_metadata:
            gates["market_product_metadata"] = _gate(
                "unprovable",
                "active_universe_market_product_metadata_missing",
                active_count=len(active_rows),
                missing_count=len(missing_metadata),
                examples=[
                    {"symbol": symbol, "fields": fields}
                    for symbol, fields in missing_metadata[:20]
                ],
            )
        else:
            gates["market_product_metadata"] = _gate(
                "proven",
                "active_universe_market_product_metadata_complete",
                active_count=len(active_rows),
                missing_count=0,
            )

        stale_metadata = sorted(
            row.symbol
            for row in active_rows
            if row.metadata_as_of is None
            or row.metadata_as_of.date() < selector_input.as_of_session
        )
        if stale_metadata:
            gates["metadata_authority_as_of"] = _gate(
                "unprovable",
                "metadata_not_authoritative_as_of_selection_session",
                required_as_of=selector_input.as_of_session.isoformat(),
                stale_count=len(stale_metadata),
                stale_examples=_examples(stale_metadata),
            )
        else:
            gates["metadata_authority_as_of"] = _gate(
                "proven",
                "metadata_authoritative_as_of_selection_session",
                required_as_of=selector_input.as_of_session.isoformat(),
                checked_count=len(active_rows),
            )

        if missing_metadata:
            coverage_rows: list[UniverseRow] = []
            preliminary: list[UniverseRow] = []
        else:
            coverage_rows = _coverage_universe(rows, selector_input.as_of_session)
            preliminary = _pre_reference_eligible(rows, selector_input.target_session)

        if preliminary:
            gates["eligible_universe"] = _gate(
                "proven",
                "pre_reference_eligible_universe_nonempty",
                count=len(preliminary),
                filters={
                    "is_active": True,
                    "listing_status": ACTIVE_LISTING_STATUS,
                    "security_type": STANDARD_STOCK_SECURITY_TYPE,
                    "is_common_share": True,
                    "list_date_before_target_session": True,
                    "krx_trading_suspended": False,
                },
            )
        else:
            gates["eligible_universe"] = _gate(
                "unprovable",
                "no_pre_reference_eligible_standard_common_stock",
                count=0,
            )

        coverage_symbols = {row.symbol for row in coverage_rows}
        market_candle_symbols = {
            row.symbol
            for row in selector_input.candle_rows
            if row.symbol in {item.symbol for item in rows}
            and row.session_date == selector_input.as_of_session
            and row.venue == "KRX"
        }
        missing_candles = sorted(coverage_symbols - market_candle_symbols)
        unexpected_candles = sorted(market_candle_symbols - coverage_symbols)
        coverage_proven = not (
            not coverage_symbols
            or missing_candles
            or unexpected_candles
            or candle_duplicates
        )
        if not coverage_proven:
            gates["completed_session_universe_coverage"] = _gate(
                "unprovable",
                "completed_session_full_universe_coverage_unproven",
                expected_count=len(coverage_symbols),
                actual_count=len(market_candle_symbols),
                missing_count=len(missing_candles),
                missing_examples=_examples(missing_candles),
                unexpected_count=len(unexpected_candles),
                unexpected_examples=_examples(unexpected_candles),
            )
        else:
            gates["completed_session_universe_coverage"] = _gate(
                "proven",
                "completed_session_full_universe_coverage_proven",
                expected_count=len(coverage_symbols),
                actual_count=len(market_candle_symbols),
                missing_count=0,
                unexpected_count=0,
            )

        invalid_candles: list[str] = []
        for symbol in sorted(coverage_symbols & market_candle_symbols):
            candle = candle_index.get(symbol)
            if not isinstance(candle, CandleRow):
                invalid_candles.append(symbol)
                continue
            if (
                candle.session_date != selector_input.as_of_session
                or candle.venue != "KRX"
                or candle.open <= 0
                or candle.high <= 0
                or candle.low <= 0
                or candle.close <= 0
                or candle.volume < 0
                or candle.value < 0
                or not candle.source
            ):
                invalid_candles.append(symbol)
        if not coverage_proven:
            gates["completed_session_row_integrity"] = _gate(
                "unprovable",
                "completed_session_row_integrity_requires_full_coverage",
                checked_count=len(coverage_symbols & market_candle_symbols),
                expected_count=len(coverage_symbols),
            )
        elif invalid_candles:
            gates["completed_session_row_integrity"] = _gate(
                "unprovable",
                "completed_session_row_integrity_unproven",
                invalid_count=len(invalid_candles),
                invalid_examples=_examples(invalid_candles),
            )
        else:
            gates["completed_session_row_integrity"] = _gate(
                "proven",
                "completed_session_row_integrity_proven",
                checked_count=len(coverage_symbols),
            )

        preliminary_symbols = {row.symbol for row in preliminary}
        missing_reference = sorted(preliminary_symbols - set(reference_index))
        invalid_reference: list[str] = []
        for symbol in sorted(preliminary_symbols & set(reference_index)):
            evidence = reference_index[symbol]
            if not isinstance(evidence, ReferenceExceptionEvidence):
                invalid_reference.append(symbol)
                continue
            if (
                evidence.effective_session != selector_input.target_session
                or evidence.is_exception is None
                or evidence.source not in AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES
                or evidence.source_as_of.date() < selector_input.target_session
            ):
                invalid_reference.append(symbol)
        if missing_reference or invalid_reference or not preliminary_symbols:
            gates["reference_price_exception_coverage"] = _gate(
                "unprovable",
                "target_session_reference_price_exception_unproven",
                target_session=selector_input.target_session.isoformat(),
                required_authoritative_sources=sorted(
                    AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES
                ),
                expected_count=len(preliminary_symbols),
                missing_count=len(missing_reference),
                missing_examples=_examples(missing_reference),
                invalid_count=len(invalid_reference),
                invalid_examples=_examples(invalid_reference),
                fallback_forbidden=True,
            )
        else:
            gates["reference_price_exception_coverage"] = _gate(
                "proven",
                "target_session_reference_price_exception_coverage_proven",
                target_session=selector_input.target_session.isoformat(),
                checked_count=len(preliminary_symbols),
            )

        pre_reference_ranked = sorted(
            (
                (row, candle_index.get(row.symbol))
                for row in preliminary
                if isinstance(candle_index.get(row.symbol), CandleRow)
            ),
            key=lambda item: (-item[1].value, item[0].symbol),  # type: ignore[union-attr]
        )
        pre_reference_head: dict[str, object] | None = None
        if pre_reference_ranked:
            head_row, head_candle = pre_reference_ranked[0]
            assert isinstance(head_candle, CandleRow)
            pre_reference_head = {
                "not_a_selection": True,
                "reason": "reference_exception_gate_not_yet_applied",
                "universe_row": _row_dict(head_row),
                "candle_row": _row_dict(head_candle),
                "rank_key": {
                    "value_desc": head_candle.value,
                    "symbol_asc": head_row.symbol,
                },
            }

        reference_gate_proven = (
            gates["reference_price_exception_coverage"]["status"] == "proven"
        )
        final_ranked: list[tuple[UniverseRow, CandleRow]] = []
        if reference_gate_proven:
            for row in preliminary:
                evidence = reference_index[row.symbol]
                assert isinstance(evidence, ReferenceExceptionEvidence)
                candle = candle_index.get(row.symbol)
                if evidence.is_exception is False and isinstance(candle, CandleRow):
                    final_ranked.append((row, candle))
            final_ranked.sort(key=lambda item: (-item[1].value, item[0].symbol))

        if not final_ranked:
            gates["ranked_candidate"] = _gate(
                "unprovable",
                (
                    "rank_unavailable_until_reference_exception_gate_proven"
                    if not reference_gate_proven
                    else "no_eligible_candidate_after_reference_exception_filter"
                ),
                market=market,
            )
        else:
            gates["ranked_candidate"] = _gate(
                "proven",
                "market_rank_deterministically_resolved",
                market=market,
                value_order="descending",
                tie_break="symbol_ascending",
            )
            selected_row, selected_candle = final_ranked[0]
            completed_gate = _completed_bar_gate(
                selected_candle,
                completed_index.get(selected_row.symbol)
                if isinstance(
                    completed_index.get(selected_row.symbol), CompletedBarEvidence
                )
                else None,
            )
            quote_gate = _quote_timestamp_gate(
                symbol=selected_row.symbol,
                as_of_session=selector_input.as_of_session,
                evidence=(
                    quote_index.get(selected_row.symbol)
                    if isinstance(
                        quote_index.get(selected_row.symbol), QuoteTimestampEvidence
                    )
                    else None
                ),
            )
            gates["completed_close"] = completed_gate
            gates["selected_quote_raw_timestamp"] = quote_gate
            raw_limit_price = (85 * selected_candle.close) // 100
            floored_limit_price = tick_floor_exact(raw_limit_price, market)
            pending_candidates.append(
                {
                    "market": market,
                    "quantity": 1,
                    "universe_row": _row_dict(selected_row),
                    "candle_row": _row_dict(selected_candle),
                    "reference_exception_evidence": _row_dict(
                        reference_index[selected_row.symbol]
                    ),
                    "completed_bar_evidence": (
                        _row_dict(completed_index[selected_row.symbol])
                        if selected_row.symbol in completed_index
                        else None
                    ),
                    "quote_timestamp_evidence": (
                        _row_dict(quote_index[selected_row.symbol])
                        if selected_row.symbol in quote_index
                        else None
                    ),
                    "ranking": {
                        "value_desc": selected_candle.value,
                        "tie_break_symbol_asc": selected_row.symbol,
                    },
                    "limit_price_calculation": {
                        "expression": "(85 * completed_close) // 100",
                        "completed_close": selected_candle.close,
                        "numerator": 85 * selected_candle.close,
                        "raw_limit_price": raw_limit_price,
                        "tick_floor_expression": "(raw // tick) * tick",
                        "tick": next(
                            band.tick
                            for band in STANDARD_STOCK_TICK_TABLES[market]
                            if raw_limit_price >= band.lower
                            and (
                                band.upper_exclusive is None
                                or raw_limit_price < band.upper_exclusive
                            )
                        ),
                        "limit_price": floored_limit_price,
                        "integer_arithmetic_only": True,
                    },
                }
            )

        if "completed_close" not in gates:
            gates["completed_close"] = _gate(
                "unprovable",
                "completed_close_unavailable_without_ranked_candidate",
                market=market,
            )
        if "selected_quote_raw_timestamp" not in gates:
            gates["selected_quote_raw_timestamp"] = _gate(
                "unprovable",
                "selected_quote_unavailable_without_ranked_candidate",
                market=market,
                wrapper_fields_are_insufficient=True,
            )

        market_results[market] = {
            "gates": gates,
            "counts": {
                "universe": len(rows),
                "active": len(active_rows),
                "coverage_universe": len(coverage_rows),
                "pre_reference_eligible": len(preliminary),
                "post_reference_eligible": len(final_ranked),
            },
            "pre_reference_rank_head": pre_reference_head,
        }

    all_gates = [("global", name, gate) for name, gate in global_gates.items()] + [
        (market, name, gate)
        for market, result in market_results.items()
        for name, gate in result["gates"].items()  # type: ignore[union-attr]
    ]
    fail_close_reasons = [
        {
            "scope": scope,
            "gate": name,
            "reason": gate["reason"],
        }
        for scope, name, gate in all_gates
        if gate["status"] != "proven"
    ]
    all_proven = not fail_close_reasons
    selected_candidates = (
        [
            next(
                candidate
                for candidate in pending_candidates
                if candidate["market"] == market
            )
            for market in MARKETS
        ]
        if all_proven and len(pending_candidates) == 2
        else []
    )
    if len(selected_candidates) != 2:
        all_proven = False
        selected_candidates = []
        if not any(
            reason["gate"] == "two_market_selection" for reason in fail_close_reasons
        ):
            fail_close_reasons.append(
                {
                    "scope": "global",
                    "gate": "two_market_selection",
                    "reason": "both_markets_must_be_fully_proven",
                }
            )

    return {
        "schema_version": "krb1.p0_3.liquidity_selector.evidence.v1",
        "status": "selected" if all_proven else "fail_closed",
        "read_only": True,
        "fallback_used": False,
        "as_of_session": selector_input.as_of_session.isoformat(),
        "target_session": selector_input.target_session.isoformat(),
        "selection_rule": {
            "markets": list(MARKETS),
            "quantity_each": 1,
            "rank": ["value_desc", "symbol_asc"],
            "raw_limit_price": "(85 * completed_close) // 100",
            "tick_floor": "(raw // tick) * tick",
            "integer_arithmetic_only": True,
        },
        "global_gates": global_gates,
        "market_results": market_results,
        "selected_candidates": selected_candidates,
        "fail_close_reasons": fail_close_reasons,
    }


__all__ = [
    "AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES",
    "CandleRow",
    "CompletedBarEvidence",
    "MARKETS",
    "QuoteTimestampEvidence",
    "ReferenceExceptionEvidence",
    "STANDARD_STOCK_TICK_TABLES",
    "SelectorInput",
    "TickBand",
    "UniverseRow",
    "select_krb1_p0_liquidity_candidates",
    "tick_floor_exact",
]
