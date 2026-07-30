"""Deterministic, fail-closed KR-B1 P0-3 liquidity selector.

This module is deliberately isolated from the normal-strategy DV20/M60 selector.
It is a pure decision service: callers inject database rows and raw upstream
evidence, and the same inputs always produce the same JSON-compatible result.
No clock, database, network, broker mutation, or fallback screener is reachable
from here.

Every gate is answered against ``decision_at``: evidence that did not exist at
the decision clock cannot prove the state at that decision (ROB-1158 r2).
Local sync/observation clocks are retrieval provenance only and are never
authority. Provider-origin identity and publication/effective clocks are required
for any "proven" claim.
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
AUTHORITATIVE_METADATA_SOURCES = frozenset({"toss_openapi"})
KST = dt.timezone(dt.timedelta(hours=9))
KRX_DAILY_COMPLETION_CUTOFF = dt.time(15, 35)
KRX_SESSION_OPEN = dt.time(9, 0)
QUOTE_EVIDENCE_AT_OR_AFTER = dt.time(15, 30)


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
    # Retrieval provenance, never authority. These describe *our* sync of the
    # master into the database, not anything the provider asserted.
    db_sync_source: str | None
    db_sync_observed_at: dt.datetime | None


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
    # Provider-origin publication/retrieval clocks. Both must precede decision_at.
    published_at: dt.datetime | None = None
    retrieved_at: dt.datetime | None = None
    raw_reference_price: str | None = None
    raw_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class CompletedBarEvidence:
    # ``symbol`` is the symbol we *requested*: request context, not identity.
    symbol: str
    endpoint: str
    tr_id: str
    # Provider-origin identity from the same daily response. The KIS daily
    # response carries no symbol field, so this is None in practice and the gate
    # stays unprovable. Request context must not stand in for it.
    raw_symbol: str | None
    raw_business_date: str | None
    raw_close: str | None
    raw_volume: str | None
    raw_value: str | None
    observed_at: dt.datetime
    raw_open: str | None = None
    raw_high: str | None = None
    raw_low: str | None = None


@dataclass(frozen=True, slots=True)
class QuoteTimestampEvidence:
    symbol: str
    endpoint: str
    tr_id: str
    raw_symbol: str | None
    raw_business_date: str | None
    raw_execution_time: str | None
    raw_last_price: str | None
    # When the evidence was captured; must precede decision_at.
    captured_at: dt.datetime | None = None
    wrapper_price_as_of: str | None = None
    wrapper_price_freshness: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataAuthoritySnapshot:
    """In-memory authority attestation injected by the caller."""

    source: str
    market: str
    symbol_count: int
    provider_published_at: dt.datetime | None
    provider_effective_session: dt.date | None
    retrieved_at: dt.datetime
    raw_payload_sha256: str


@dataclass(frozen=True, slots=True)
class ExternalUniverseDenominator:
    """In-memory external listed-count attestation injected by the caller."""

    market: str
    session_date: dt.date
    source: str
    listed_count: int
    published_at: dt.datetime
    retrieved_at: dt.datetime


@dataclass(frozen=True, slots=True)
class SelectorInput:
    as_of_session: dt.date
    target_session: dt.date
    # The clock the decision is taken at. Every evidence clock must precede it.
    decision_at: dt.datetime
    expected_universe_counts: dict[Market, int]
    universe_rows: tuple[UniverseRow, ...]
    candle_rows: tuple[CandleRow, ...]
    reference_exception_evidence: tuple[ReferenceExceptionEvidence, ...] = ()
    completed_bar_evidence: tuple[CompletedBarEvidence, ...] = ()
    quote_timestamp_evidence: tuple[QuoteTimestampEvidence, ...] = ()
    metadata_authority_snapshots: tuple[MetadataAuthoritySnapshot, ...] = ()
    external_universe_denominators: tuple[ExternalUniverseDenominator, ...] = ()
    reference_source_unavailable_reason: str | None = None
    universe_denominator_source_unavailable_reason: str | None = None


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


def _observed_after_daily_completion(
    observed_at: dt.datetime,
    session_date: dt.date,
) -> bool:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return False
    observed_kst = observed_at.astimezone(KST)
    return observed_kst.date() > session_date or (
        observed_kst.date() == session_date
        and observed_kst.time().replace(tzinfo=None) >= KRX_DAILY_COMPLETION_CUTOFF
    )


def _is_aware(value: dt.datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _kst_datetime(date: dt.date, time: dt.time) -> dt.datetime:
    return dt.datetime.combine(date, time).replace(tzinfo=KST)


def _completed_bar_matches(
    candle: CandleRow,
    evidence: CompletedBarEvidence,
    decision_at: dt.datetime,
) -> bool:
    if not _is_aware(decision_at):
        return False
    if not _is_aware(evidence.observed_at) or evidence.observed_at > decision_at:
        return False
    if evidence.raw_symbol is None or evidence.raw_symbol != candle.symbol:
        return False
    return (
        evidence.symbol == candle.symbol
        and evidence.endpoint == KIS_DAILY_ENDPOINT
        and evidence.tr_id == KIS_DAILY_TR_ID
        and evidence.raw_business_date == candle.session_date.strftime("%Y%m%d")
        and _parse_nonnegative_int_string(evidence.raw_close) == candle.close
        and _parse_nonnegative_int_string(evidence.raw_volume) == candle.volume
        and _parse_nonnegative_int_string(evidence.raw_value) == candle.value
        and _observed_after_daily_completion(evidence.observed_at, candle.session_date)
    )


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
        "db_sync_source": row.db_sync_source,
        "db_sync_observed_at": row.db_sync_observed_at,
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
    decision_at: dt.datetime,
) -> dict[str, object]:
    if evidence is None:
        return _gate(
            "unprovable",
            "selected_completed_bar_raw_evidence_missing",
            symbol=candle.symbol,
            required_endpoint=KIS_DAILY_ENDPOINT,
            required_tr_id=KIS_DAILY_TR_ID,
        )
    if evidence.raw_symbol is None:
        return _gate(
            "unprovable",
            "selected_completed_bar_provider_identity_missing",
            candle=_row_dict(candle),
            raw_evidence=_row_dict(evidence),
            request_context_symbol_is_not_identity=True,
            required_raw_field="provider-origin symbol in the same daily response",
        )
    if not _completed_bar_matches(candle, evidence, decision_at):
        return _gate(
            "unprovable",
            "selected_completed_bar_raw_evidence_mismatch",
            candle=_row_dict(candle),
            raw_evidence=_row_dict(evidence),
            required_observation_upper_bound_decision_at=decision_at.isoformat(),
        )
    return _gate(
        "proven",
        "selected_completed_bar_matches_kis_daily_raw",
        candle=_row_dict(candle),
        raw_evidence=_row_dict(evidence),
        required_observation_upper_bound_decision_at=decision_at.isoformat(),
    )


def _quote_timestamp_gate(
    *,
    symbol: str,
    as_of_session: dt.date,
    decision_at: dt.datetime,
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
    if not _is_aware(decision_at):
        return _gate(
            "unprovable",
            "selected_quote_actual_raw_timestamp_unproven",
            raw_evidence=_row_dict(evidence),
            wrapper_fields_are_insufficient=True,
            required_capture_upper_bound_decision_at=_iso(decision_at),
        )
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
    captured_too_late = evidence.captured_at is not None and (
        not _is_aware(evidence.captured_at) or evidence.captured_at > decision_at
    )
    if not raw_fields_proven or captured_too_late:
        return _gate(
            "unprovable",
            "selected_quote_actual_raw_timestamp_unproven",
            raw_evidence=_row_dict(evidence),
            wrapper_fields_are_insufficient=True,
            required_raw_fields=["stck_bsop_date", "stck_cntg_hour"],
            required_session=expected_date,
            required_time_at_or_after="153000",
            required_capture_upper_bound_decision_at=decision_at.isoformat(),
        )
    return _gate(
        "proven",
        "selected_quote_actual_raw_timestamp_proven",
        raw_evidence=_row_dict(evidence),
    )


def _decision_clock_gate(selector_input: SelectorInput) -> dict[str, object]:
    """Bound the decision clock so no gate can be satisfied retroactively."""
    decision_at = selector_input.decision_at
    as_of_session = selector_input.as_of_session
    target_session = selector_input.target_session
    required = {
        "required_timezone_aware": True,
        "required_at_or_after_kst": _kst_datetime(
            as_of_session, KRX_DAILY_COMPLETION_CUTOFF
        ).isoformat(),
        "required_before_kst": _kst_datetime(
            target_session, KRX_SESSION_OPEN
        ).isoformat(),
    }
    if not _is_aware(decision_at):
        return _gate(
            "unprovable",
            "decision_at_must_be_timezone_aware",
            decision_at=_iso(decision_at),
            **required,
        )
    if decision_at < _kst_datetime(as_of_session, KRX_DAILY_COMPLETION_CUTOFF):
        return _gate(
            "unprovable",
            "decision_at_before_completed_session_cutoff",
            decision_at=decision_at.isoformat(),
            **required,
        )
    if decision_at >= _kst_datetime(target_session, KRX_SESSION_OPEN):
        return _gate(
            "unprovable",
            "decision_at_not_before_target_session_open",
            decision_at=decision_at.isoformat(),
            **required,
        )
    return _gate(
        "proven",
        "decision_clock_within_selection_window",
        decision_at=decision_at.isoformat(),
        **required,
    )


def _metadata_authority_snapshot_gate(
    *,
    market: str,
    active_rows: list[UniverseRow],
    as_of_session: dt.date,
    decision_at: dt.datetime,
    snapshots: tuple[MetadataAuthoritySnapshot, ...],
) -> dict[str, object]:
    """Provider-origin metadata authority. Local sync clock is not admissible."""
    common = {
        "market": market,
        "required_as_of_session": as_of_session.isoformat(),
        "required_provider_clock_upper_bound_decision_at": _iso(decision_at),
    }
    if not _is_aware(decision_at):
        return _gate(
            "unprovable",
            "decision_at_must_be_timezone_aware",
            **common,
        )
    market_snapshots = [s for s in snapshots if s.market == market]
    if not market_snapshots:
        return _gate(
            "unprovable",
            "authoritative_metadata_snapshot_missing",
            authoritative_sources=sorted(AUTHORITATIVE_METADATA_SOURCES),
            **common,
        )
    snapshot = market_snapshots[0]
    if snapshot.source not in AUTHORITATIVE_METADATA_SOURCES:
        return _gate(
            "unprovable",
            "metadata_snapshot_source_not_authoritative",
            snapshot_source=snapshot.source,
            authoritative_sources=sorted(AUTHORITATIVE_METADATA_SOURCES),
            **common,
        )
    if snapshot.provider_published_at is None or not _is_aware(
        snapshot.provider_published_at
    ):
        return _gate(
            "unprovable",
            "metadata_snapshot_provider_authority_clock_missing",
            **common,
        )
    if snapshot.provider_published_at > decision_at:
        return _gate(
            "unprovable",
            "metadata_snapshot_provider_published_after_decision_at",
            provider_published_at=snapshot.provider_published_at.isoformat(),
            **common,
        )
    if (
        snapshot.provider_effective_session is None
        or snapshot.provider_effective_session < as_of_session
    ):
        return _gate(
            "unprovable",
            "metadata_snapshot_provider_effective_session_before_selection_session",
            provider_effective_session=_iso(snapshot.provider_effective_session),
            **common,
        )
    if not _is_aware(snapshot.retrieved_at) or snapshot.retrieved_at > decision_at:
        return _gate(
            "unprovable",
            "metadata_snapshot_retrieved_after_decision_at",
            retrieved_at=snapshot.retrieved_at.isoformat(),
            **common,
        )
    actual_count = len([row for row in active_rows if row.exchange == market])
    if snapshot.symbol_count != actual_count:
        return _gate(
            "unprovable",
            "metadata_snapshot_symbol_count_mismatch",
            snapshot_count=snapshot.symbol_count,
            actual_count=actual_count,
            **common,
        )
    return _gate(
        "proven",
        "metadata_authority_snapshot_proven",
        source=snapshot.source,
        symbol_count=snapshot.symbol_count,
        **common,
    )


def _row_sync_provenance_gate(
    *,
    active_rows: list[UniverseRow],
    market: str,
    as_of_session: dt.date,
    decision_at: dt.datetime,
    snapshot_gate: dict[str, object],
) -> dict[str, object]:
    """Row-level DB sync provenance. Explicitly **not** an authority claim."""
    common = {
        "sync_clock_is_retrieval_provenance_not_authority": True,
        "authority_claim_delegated_to": "metadata_authority_snapshot",
        "required_sync_clock_upper_bound_decision_at": _iso(decision_at),
        "required_sync_clock_at_or_after": as_of_session.isoformat(),
        "market": market,
    }
    if snapshot_gate.get("status") != "proven":
        return _gate(
            "unprovable",
            "metadata_row_authority_requires_provider_snapshot",
            provider_snapshot_reason=snapshot_gate.get("reason"),
            **common,
        )
    market_rows = [row for row in active_rows if row.exchange == market]
    missing = sorted(
        row.symbol
        for row in market_rows
        if row.db_sync_observed_at is None or not row.db_sync_source
    )
    if missing:
        return _gate(
            "unprovable",
            "metadata_row_sync_provenance_missing",
            missing_count=len(missing),
            missing_examples=_examples(missing),
            **common,
        )
    unusable = sorted(
        row.symbol
        for row in market_rows
        if row.db_sync_observed_at is not None
        and (
            not _is_aware(row.db_sync_observed_at)
            or row.db_sync_observed_at > decision_at
        )
    )
    if unusable:
        return _gate(
            "unprovable",
            "metadata_row_sync_clock_after_decision_at",
            retroactive_count=len(unusable),
            retroactive_examples=_examples(unusable),
            late_backfill_is_not_proof_of_state_at_decision_at=True,
            **common,
        )
    stale = sorted(
        row.symbol
        for row in market_rows
        if row.db_sync_observed_at is not None
        and row.db_sync_observed_at.astimezone(KST).date() < as_of_session
    )
    if stale:
        return _gate(
            "unprovable",
            "metadata_row_sync_clock_stale_for_selection_session",
            stale_count=len(stale),
            stale_examples=_examples(stale),
            **common,
        )
    return _gate(
        "proven",
        "metadata_row_sync_provenance_within_decision_clock",
        checked_count=len(market_rows),
        **common,
    )


def _universe_denominator_gate(
    *,
    market: str,
    as_of_session: dt.date,
    actual_count: int,
    expected_count: int | None,
    external_denominators: tuple[ExternalUniverseDenominator, ...],
    unavailable_reason: str | None,
) -> dict[str, object]:
    """Coverage denominator must come from outside the same DB read."""
    common = {
        "market": market,
        "required_as_of_session": as_of_session.isoformat(),
        "same_transaction_count_is_not_independent_evidence": True,
    }
    if unavailable_reason:
        return _gate(
            "unprovable",
            "universe_denominator_external_basis_unproven",
            defect="external_denominator_source_not_wired",
            source_unavailable_reason=unavailable_reason,
            **common,
        )
    market_denominators = [d for d in external_denominators if d.market == market]
    if not market_denominators:
        return _gate(
            "unprovable",
            "universe_denominator_external_basis_unproven",
            defect="no_external_denominator_for_market_session",
            **common,
        )
    denominator = market_denominators[0]
    if denominator.listed_count != actual_count or (
        expected_count is not None and denominator.listed_count != expected_count
    ):
        return _gate(
            "unprovable",
            "universe_denominator_disagrees_with_external_basis",
            external_count=denominator.listed_count,
            actual_count=actual_count,
            expected_count=expected_count,
            source=denominator.source,
            **common,
        )
    return _gate(
        "proven",
        "universe_denominator_external_basis_proven",
        external_count=denominator.listed_count,
        source=denominator.source,
        **common,
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
    global_gates["decision_clock"] = _decision_clock_gate(selector_input)
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

        snapshot_gate = _metadata_authority_snapshot_gate(
            market=market,
            active_rows=active_rows,
            as_of_session=selector_input.as_of_session,
            decision_at=selector_input.decision_at,
            snapshots=selector_input.metadata_authority_snapshots,
        )
        gates["metadata_authority_snapshot"] = snapshot_gate
        gates["metadata_authority_as_of"] = _row_sync_provenance_gate(
            active_rows=active_rows,
            market=market,
            as_of_session=selector_input.as_of_session,
            decision_at=selector_input.decision_at,
            snapshot_gate=snapshot_gate,
        )

        gates["universe_snapshot_coverage"] = _universe_denominator_gate(
            market=market,
            as_of_session=selector_input.as_of_session,
            actual_count=actual_count,
            expected_count=expected_count,
            external_denominators=selector_input.external_universe_denominators,
            unavailable_reason=selector_input.universe_denominator_source_unavailable_reason,
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

        missing_completed_evidence = sorted(coverage_symbols - set(completed_index))
        invalid_completed_evidence: list[str] = []
        identity_missing_symbols: list[str] = []
        if coverage_proven:
            for symbol in sorted(coverage_symbols & set(completed_index)):
                candle = candle_index.get(symbol)
                evidence = completed_index[symbol]
                if not isinstance(candle, CandleRow) or not isinstance(
                    evidence, CompletedBarEvidence
                ):
                    invalid_completed_evidence.append(symbol)
                    continue
                if evidence.raw_symbol is None:
                    identity_missing_symbols.append(symbol)
                    continue
                if not _completed_bar_matches(
                    candle, evidence, selector_input.decision_at
                ):
                    invalid_completed_evidence.append(symbol)
        if (
            not coverage_proven
            or missing_completed_evidence
            or invalid_completed_evidence
            or identity_missing_symbols
        ):
            gates["completed_session_raw_completion"] = _gate(
                "unprovable",
                "full_universe_raw_daily_local_match_unproven",
                required_endpoint=KIS_DAILY_ENDPOINT,
                required_tr_id=KIS_DAILY_TR_ID,
                required_observation_cutoff_kst="15:35:00",
                expected_count=len(coverage_symbols),
                missing_count=len(missing_completed_evidence),
                missing_examples=_examples(missing_completed_evidence),
                invalid_count=len(invalid_completed_evidence),
                invalid_examples=_examples(invalid_completed_evidence),
                identity_missing_count=len(identity_missing_symbols),
                identity_missing_examples=_examples(identity_missing_symbols),
                request_context_symbol_is_not_identity=True,
                coverage_prerequisite_proven=coverage_proven,
                ingested_at_alone_is_insufficient=True,
            )
        else:
            gates["completed_session_raw_completion"] = _gate(
                "proven",
                "full_universe_raw_daily_local_match_proven",
                endpoint=KIS_DAILY_ENDPOINT,
                tr_id=KIS_DAILY_TR_ID,
                required_observation_cutoff_kst="15:35:00",
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
                not _is_aware(selector_input.decision_at)
                or evidence.effective_session != selector_input.target_session
                or evidence.is_exception is None
                or evidence.source not in AUTHORITATIVE_REFERENCE_EXCEPTION_SOURCES
                or evidence.source_as_of.date() < selector_input.target_session
                or (
                    evidence.published_at is not None
                    and (
                        not _is_aware(evidence.published_at)
                        or evidence.published_at > selector_input.decision_at
                    )
                )
                or (
                    evidence.retrieved_at is not None
                    and (
                        not _is_aware(evidence.retrieved_at)
                        or evidence.retrieved_at > selector_input.decision_at
                    )
                )
                or _parse_nonnegative_int_string(evidence.raw_reference_price)
                in {
                    None,
                    0,
                }
                or not evidence.raw_reason_code
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
                decision_at=selector_input.decision_at,
            )
            quote_gate = _quote_timestamp_gate(
                symbol=selected_row.symbol,
                as_of_session=selector_input.as_of_session,
                decision_at=selector_input.decision_at,
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
        "schema_version": "krb1.p0_3.liquidity_selector.evidence.v2",
        "status": "selected" if all_proven else "fail_closed",
        "read_only": True,
        "fallback_used": False,
        "as_of_session": selector_input.as_of_session.isoformat(),
        "target_session": selector_input.target_session.isoformat(),
        "decision_at": selector_input.decision_at.isoformat(),
        "selection_rule": {
            "markets": list(MARKETS),
            "quantity_each": 1,
            "rank": ["value_desc", "symbol_asc"],
            "raw_limit_price": "(85 * completed_close) // 100",
            "tick_floor": "(raw // tick) * tick",
            "integer_arithmetic_only": True,
        },
        "evidence_clock_contract": {
            "late_backfill_is_not_proof_of_state_at_decision_at": True,
            "metadata": "provider_published_at <= provider_effective_session <= decision_at; retrieved_at <= decision_at",
            "completion": "observed_at <= decision_at; raw_symbol from provider response",
            "reference": "published_at <= decision_at; retrieved_at <= decision_at",
            "quote": "captured_at <= decision_at; raw business_date/time from provider response",
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
    "ExternalUniverseDenominator",
    "MARKETS",
    "MetadataAuthoritySnapshot",
    "QuoteTimestampEvidence",
    "ReferenceExceptionEvidence",
    "STANDARD_STOCK_TICK_TABLES",
    "SelectorInput",
    "TickBand",
    "UniverseRow",
    "select_krb1_p0_liquidity_candidates",
    "tick_floor_exact",
]
