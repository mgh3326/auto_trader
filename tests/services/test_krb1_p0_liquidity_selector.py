from __future__ import annotations

import ast
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.services import krb1_completion_finality, krb1_metadata_authority
from app.services import krb1_universe_denominator as denominator_module
from app.services.krb1_completion_finality import ProviderFinalityAttestation
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
from app.services.krb1_universe_denominator import ExternalUniverseDenominator

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
DECLARED_PUBLISHED = frozenset({"publishedAt"})
DECLARED_EFFECTIVE = frozenset({"effectiveSession"})
HYPOTHETICAL_FINALITY_SOURCE = "krx_official_daily_finality"
HYPOTHETICAL_DENOMINATOR_SOURCE = "krx_official_listed_instrument_count"


@pytest.fixture(autouse=True)
def _declared_provider_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare hypothetical provider contracts for these tests only.

    🔴 Test-only seam (monkeypatched module attributes, never a parameter — see
    ROB-1172 F-INT-04). Three separate sources are unwired in production:

    * no wired Toss market-data endpoint declares a publication or
      effective-session field (D1), so the metadata allowlists are empty;
    * no wired surface attests daily-OHLCV finality (A3/F-INT-01); and
    * no external listed-instrument count is available (F-INT-03; ROB-1175).

    Each one alone fails the whole run closed, which is the correct production
    state. Without these hypotheticals every other gate in this file would be
    masked by those fail-closes and nothing else could be attributed.
    """
    monkeypatch.setattr(
        krb1_metadata_authority, "PROVIDER_PUBLISHED_AT_FIELDS", DECLARED_PUBLISHED
    )
    monkeypatch.setattr(
        krb1_metadata_authority,
        "PROVIDER_EFFECTIVE_SESSION_FIELDS",
        DECLARED_EFFECTIVE,
    )
    monkeypatch.setattr(krb1_completion_finality, "FINALITY_SOURCE_WIRED", True)
    monkeypatch.setattr(
        krb1_completion_finality,
        "ADMISSIBLE_FINALITY_SOURCES",
        frozenset({HYPOTHETICAL_FINALITY_SOURCE}),
    )
    monkeypatch.setattr(denominator_module, "EXTERNAL_DENOMINATOR_SOURCE_WIRED", True)
    monkeypatch.setattr(
        denominator_module,
        "ADMISSIBLE_EXTERNAL_DENOMINATOR_SOURCES",
        frozenset({HYPOTHETICAL_DENOMINATOR_SOURCE}),
    )


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
        db_sync_source="db.kr_symbol_universe.toss_master_updated_at",
        db_sync_observed_at=METADATA_RETRIEVED_AT,
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


def _finality(market: str) -> ProviderFinalityAttestation:
    """Hypothetical provider finality attestation.

    No wired source produces one (A3), so the stub fails closed in production; the
    fixture injects one to keep the other gates reachable.
    """
    return ProviderFinalityAttestation(
        market=market,
        session_date=AS_OF,
        source=HYPOTHETICAL_FINALITY_SOURCE,
        revision="1",
        declared_final_at=dt.datetime(2026, 7, 29, 16, 40, tzinfo=KST),
        retrieved_at=dt.datetime(2026, 7, 29, 16, 50, tzinfo=KST),
        correction_policy="corrections republished with an incremented revision",
        raw_payload_sha256="f" * 64,
    )


def _denominator(
    market: str, rows: tuple[UniverseRow, ...]
) -> ExternalUniverseDenominator:
    """Hypothetical external listed-instrument count.

    🔴 It is derived from the fixture universe here only so the *other* gates stay
    reachable. That is exactly what makes it hypothetical: in production the count
    has to come from a source that is not our database, and none is wired
    (F-INT-03), so the gate blocks. A test that needs a real disagreement builds
    the denominator explicitly instead of calling this.
    """
    return ExternalUniverseDenominator(
        market=market,
        session_date=AS_OF,
        source=HYPOTHETICAL_DENOMINATOR_SOURCE,
        listed_count=len([row for row in rows if row.exchange == market]),
        published_at=dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST),
        retrieved_at=dt.datetime(2026, 7, 29, 16, 10, tzinfo=KST),
        raw_payload_sha256="b" * 64,
    )


def _reference(symbol: str, *, is_exception: bool = False, **overrides: object):
    base = ReferencePriceExceptionRecord(
        symbol=symbol,
        effective_session=TARGET,
        is_exception=is_exception,
        source="krx_official_base_price",
        published_at=REFERENCE_PUBLISHED_AT,
        retrieved_at=REFERENCE_RETRIEVED_AT,
        determination_method="NORMAL_PRIOR_CLOSE",
        raw_reference_price="10000",
        raw_reason_code="NORMAL",
        raw_payload_sha256=SHA_STUB,
    )
    return replace(base, **overrides) if overrides else base


def _completed(candle: CandleRow) -> CompletedBarEvidence:
    return CompletedBarEvidence(
        symbol=candle.symbol,
        endpoint=KIS_DAILY_ENDPOINT,
        tr_id=KIS_DAILY_TR_ID,
        # Hypothetical provider-origin identity. The wired KIS daily response has
        # none (F-02), so production stays unprovable; the fixture supplies one so
        # the other gates remain reachable.
        raw_symbol=candle.symbol,
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
                raw_symbol=row.symbol,
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
        external_universe_denominators=tuple(
            _denominator(market, updated.universe_rows)
            for market in ("KOSPI", "KOSDAQ")
            # An empty market has no listed count to attest; the two-market guard
            # and the denominator gate both block it, which is the point.
            if any(row.exchange == market for row in updated.universe_rows)
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
        provider_finality_attestations=tuple(
            _finality(market) for market in ("KOSPI", "KOSDAQ")
        ),
        external_universe_denominators=tuple(
            _denominator(market, universe) for market in ("KOSPI", "KOSDAQ")
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
    manifest = candidate["local_reconcile_manifest"]
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
    """Evidence observed after the decision cannot prove the decision."""
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
        _with_evidence(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "active_universe_market_product_metadata_missing")


def test_stale_db_sync_clock_fails_closed() -> None:
    """The prod-observed value: toss_master_updated_at = 2026-07-28 08:47 KST."""
    selector_input = _base_input()
    universe = tuple(
        replace(
            row,
            db_sync_observed_at=dt.datetime(2026, 7, 28, 8, 47, tzinfo=KST),
        )
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_row_sync_clock_stale_for_selection_session")


def test_db_sync_clock_after_decision_at_fails_closed() -> None:
    """🔴 The upper bound: a 07-30 sync cannot justify a 07-29 decision."""
    selector_input = _base_input()
    universe = tuple(
        replace(row, db_sync_observed_at=dt.datetime(2026, 7, 30, 8, 47, tzinfo=KST))
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_row_sync_clock_after_decision_at")


def test_naive_db_sync_clock_fails_closed() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, db_sync_observed_at=dt.datetime(2026, 7, 29, 17, 0))
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(selector_input, universe_rows=universe)
    )
    _assert_fail_closed(result, "metadata_row_sync_clock_after_decision_at")


# ───────────── D4: the sibling gate no longer sells sync as authority ─────────────


def test_row_gate_refuses_authority_without_a_provider_snapshot() -> None:
    """🔴 p8Z F1 anchor: provider clock absent -> BOTH metadata gates unprovable.

    Before D4 this configuration produced
    `metadata_authority_as_of = proven / metadata_authoritative_as_of_selection_session`
    while the snapshot gate failed closed — an evidence artifact asserting
    authority that nothing had established.
    """
    selector_input = _base_input()
    snapshots = tuple(
        replace(snapshot, provider_clock=None)
        for snapshot in selector_input.metadata_snapshots
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_snapshots=snapshots)
    )

    _assert_fail_closed(result, "metadata_row_authority_requires_provider_snapshot")
    for market in ("KOSPI", "KOSDAQ"):
        gates = result["market_results"][market]["gates"]  # type: ignore[index]
        row_gate = gates["metadata_authority_as_of"]
        assert row_gate["status"] == "unprovable"
        assert (
            row_gate["evidence"]["sync_clock_is_retrieval_provenance_not_authority"]
            is True
        )
        assert row_gate["evidence"]["authority_claim_delegated_to"] == (
            "metadata_authority_snapshot"
        )


def test_row_gate_refuses_when_provider_says_the_master_is_stale() -> None:
    """🔴 p8Z F1 PROBE C: provider says 07-28 while our sync column says today."""
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

    _assert_fail_closed(result, "metadata_row_authority_requires_provider_snapshot")
    for market in ("KOSPI", "KOSDAQ"):
        gates = result["market_results"][market]["gates"]  # type: ignore[index]
        assert gates["metadata_authority_as_of"]["status"] == "unprovable"
        assert gates["metadata_authority_snapshot"]["reason"] == (
            "metadata_snapshot_provider_effective_session_before_selection_session"
        )


def test_row_gate_requires_recorded_sync_provenance() -> None:
    selector_input = _base_input()
    universe = tuple(
        replace(row, db_sync_source=None) if row.symbol == "000001" else row
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(selector_input, universe_rows=universe)
    )
    # A null provenance label is first caught by the completeness gate; the row
    # provenance gate names it too once metadata is otherwise complete.
    _assert_fail_closed(result, "active_universe_market_product_metadata_missing")


def test_evidence_contract_string_no_longer_cites_the_retired_rule() -> None:
    result = select_krb1_p0_liquidity_candidates(_base_input())
    contract = result["evidence_clock_contract"]
    assert isinstance(contract, dict)
    metadata_rule = str(contract["metadata"])
    assert "provider_published_at" in metadata_rule
    assert "provider_effective_session" in metadata_rule
    assert "metadata_as_of <= retrieved_at" not in metadata_rule


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


# ───────── A3: the two completion axes are separate at the run level ─────────


def test_local_reconcile_alone_does_not_produce_a_selection() -> None:
    """🔴 E3: a clean full-universe local reconcile must not be promoted.

    Both local axes stay proven, and the run still fails closed because no provider
    declared the daily revision final.
    """
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, provider_finality_attestations=())
    )

    _assert_fail_closed(result, "completed_session_provider_finality_unproven")
    for market in ("KOSPI", "KOSDAQ"):
        gates = result["market_results"][market]["gates"]  # type: ignore[index]
        assert gates["completed_session_local_reconcile"]["status"] == "proven"
        assert gates["completed_session_raw_completion"]["status"] == "proven"
        finality = gates["completed_session_provider_finality"]
        assert finality["status"] == "unprovable"
        assert finality["evidence"]["local_reconcile_proven"] is True
        assert finality["evidence"]["local_reconcile_is_not_provider_finality"] is True


def test_local_axes_no_longer_claim_completion_in_their_reasons() -> None:
    """The evidence artifact must not say 'completion proven' for a local match."""
    result = select_krb1_p0_liquidity_candidates(_base_input())
    gates = result["market_results"]["KOSPI"]["gates"]  # type: ignore[index]

    assert gates["completed_session_raw_completion"]["reason"] == (
        "full_universe_raw_daily_local_match_proven"
    )
    assert gates["completed_session_local_reconcile"]["reason"] == (
        "local_full_universe_exact_reconcile_proven"
    )
    contract = result["evidence_clock_contract"]
    assert isinstance(contract, dict)
    assert "completion_provider_finality" in contract
    assert "local reconcile can never substitute" in str(
        contract["completion_provider_finality"]
    )


def test_unwired_finality_source_reason_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            provider_finality_attestations=(),
            finality_source_unavailable_reason=(
                "provider_daily_ohlcv_finality_source_not_wired"
            ),
        )
    )
    _assert_fail_closed(result, "completed_session_provider_finality_unproven")


# ───────── A4: determination method drives exclusion, not estimation ─────────


def test_opening_call_symbol_is_excluded_when_it_is_not_rank_one() -> None:
    """KOSPI rank #1 is 000001 (value 2000); 000002 is #2 and gets excluded."""
    selector_input = _base_input()
    records = tuple(
        _reference(
            row.symbol,
            determination_method="TARGET_DAY_OPENING_CALL",
            raw_reference_price=None,
        )
        if row.symbol == "000002"
        else _reference(row.symbol)
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_price_exception_records=records)
    )

    assert result["status"] == "selected", result["fail_close_reasons"]
    assert _selected_by_market(result)["KOSPI"]["universe_row"]["symbol"] == "000001"
    counts = result["market_results"]["KOSPI"]["counts"]  # type: ignore[index]
    assert counts["excluded_pending_opening_call"] == 1


def test_global_rank_one_excluded_fails_closed_without_promoting_rank_two() -> None:
    """🔴 E4: no automatic #2 promotion in this sealed child."""
    selector_input = _base_input()
    records = tuple(
        _reference(
            row.symbol,
            determination_method="TARGET_DAY_OPENING_CALL",
            raw_reference_price=None,
        )
        if row.symbol == "000001"
        else _reference(row.symbol)
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_price_exception_records=records)
    )

    _assert_fail_closed(result, "global_rank_one_excluded_pending_opening_call")
    gate = result["market_results"]["KOSPI"]["gates"]["ranked_candidate"]  # type: ignore[index]
    assert gate["evidence"]["global_rank_one"] == "000001"
    assert gate["evidence"]["automatic_promotion_of_rank_two_forbidden"] is True
    assert (
        gate["evidence"]["positively_proven_subset_ranking_requires_future_child"]
        is True
    )
    # 000002 must not appear as a candidate anywhere.
    assert result["selected_candidates"] == []


def test_unknown_determination_method_fails_the_run() -> None:
    """🔴 E4: UNKNOWN is run-level fail-close, not an exclusion."""
    selector_input = _base_input()
    records = tuple(
        _reference(row.symbol, determination_method="UNKNOWN")
        if row.symbol == "100002"
        else _reference(row.symbol)
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_price_exception_records=records)
    )

    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")
    gate = result["market_results"]["KOSDAQ"]["gates"][  # type: ignore[index]
        "reference_price_exception_coverage"
    ]
    assert {
        "symbol": "100002",
        "defect": "determination_method_unknown",
    } in gate["evidence"]["defect_examples"]


def test_opening_call_symbol_is_never_estimated_or_awaited() -> None:
    """A record that carries a number it should not have yet is a defect."""
    selector_input = _base_input()
    records = tuple(
        _reference(
            row.symbol,
            determination_method="TARGET_DAY_OPENING_CALL",
            raw_reference_price="10000",
        )
        if row.symbol == "000002"
        else _reference(row.symbol)
        for row in selector_input.universe_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, reference_price_exception_records=records)
    )
    _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


# ───────── #1729 F-05: anchors for the mutants that survived that review ─────────
#
# Each test below kills a specific mutant the adversarial review applied to the
# #1729 selector without any test noticing. The mutant IDs are quoted so the
# mapping stays traceable.


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
        replace(row, **mutation) if row.symbol == "000002" else row  # type: ignore[arg-type]
        for row in selector_input.candle_rows
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(
            selector_input,
            candle_rows=candles,
            completed_bar_evidence=tuple(_completed(row) for row in candles),
        )
    )

    assert result["status"] == "fail_closed"
    reasons = {str(item["reason"]) for item in result["fail_close_reasons"]}  # type: ignore[union-attr]
    assert reasons & {
        "completed_session_row_integrity_unproven",
        "local_full_universe_exact_reconcile_unproven",
        "full_universe_raw_daily_local_match_unproven",
    }, reasons


def test_m18_two_market_guard_is_anchored() -> None:
    """M18: one market alone can never produce a selection."""
    selector_input = _base_input()
    kospi_only = tuple(
        row for row in selector_input.universe_rows if row.exchange == "KOSPI"
    )
    result = select_krb1_p0_liquidity_candidates(
        _with_evidence(
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
            reference_price_exception_records=tuple(
                _reference(row.symbol) for row in kospi_only
            ),
        )
    )

    assert result["status"] == "fail_closed"
    assert result["selected_candidates"] == []
    reasons = {str(item["reason"]) for item in result["fail_close_reasons"]}  # type: ignore[union-attr]
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
        replace(row, **mutation)  # type: ignore[arg-type]
        for row in selector_input.completed_bar_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completed_bar_evidence=evidence)
    )
    _assert_fail_closed(result, "full_universe_raw_daily_local_match_unproven")


def test_m22_reference_authoritative_source_check_is_anchored() -> None:
    """M22: a non-authoritative reference source must fail closed at run level."""
    selector_input = _base_input()
    for source in ("generic_screener", "operator_assertion", "", "toss_openapi"):
        records = tuple(
            replace(row, source=source)
            for row in selector_input.reference_price_exception_records
        )
        result = select_krb1_p0_liquidity_candidates(
            replace(selector_input, reference_price_exception_records=records)
        )
        _assert_fail_closed(result, "target_session_reference_price_exception_unproven")


@pytest.mark.parametrize(
    "mutation",
    [
        {"endpoint": "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"},
        {"endpoint": ""},
        {"tr_id": "FHKST03010230"},
        {"tr_id": ""},
        {"http_method": "POST"},
    ],
)
def test_m24_quote_endpoint_and_tr_checks_are_anchored(
    mutation: dict[str, object],
) -> None:
    """M24: quote evidence from the wrong endpoint/TR/method must not count."""
    selector_input = _base_input()
    quotes = tuple(
        replace(row, **mutation)  # type: ignore[arg-type]
        for row in selector_input.quote_timestamp_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, quote_timestamp_evidence=quotes)
    )
    _assert_fail_closed(result, "selected_quote_actual_raw_timestamp_unproven")


# ───────── #1729 F-02/F-04: the two proof-semantics violations ─────────


def test_f02_request_context_symbol_cannot_stand_in_for_provider_identity() -> None:
    """🔴 F-02: completion evidence without a provider-origin symbol fails closed.

    This is the shape #1729 accepted: ``symbol`` is the value we asked for, so an
    evidence row carrying only that has never been confirmed by the response.
    """
    selector_input = _base_input()
    evidence = tuple(
        replace(row, raw_symbol=None) for row in selector_input.completed_bar_evidence
    )
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, completed_bar_evidence=evidence)
    )

    _assert_fail_closed(result, "full_universe_raw_daily_local_match_unproven")
    gates = result["market_results"]["KOSPI"]["gates"]  # type: ignore[index]
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


def test_f04_self_consistent_truncated_universe_fails_closed() -> None:
    """🔴 F-04: one row per market with a matching count must not prove coverage.

    #1729 read the denominator and the rows from the same transaction, so this
    input produced ``selected``. The denominator now has to agree with the sealed
    append-only snapshot.
    """
    selector_input = _base_input()
    truncated = tuple(
        row
        for row in selector_input.universe_rows
        if row.symbol in {"000001", "100001"}
    )
    candles = tuple(
        row for row in selector_input.candle_rows if row.symbol in {"000001", "100001"}
    )
    # Self-consistent: expected == actual == 1 per market, exactly what a truncated
    # DB read would produce. The sealed snapshot still says 2.
    result = select_krb1_p0_liquidity_candidates(
        replace(
            selector_input,
            universe_rows=truncated,
            candle_rows=candles,
            expected_universe_counts={"KOSPI": 1, "KOSDAQ": 1},
            completed_bar_evidence=tuple(_completed(row) for row in candles),
            quote_timestamp_evidence=tuple(_quote(row.symbol) for row in truncated),
            reference_price_exception_records=tuple(
                _reference(row.symbol) for row in truncated
            ),
        )
    )

    _assert_fail_closed(result, "universe_denominator_disagrees_with_sealed_basis")
    gates = result["market_results"]["KOSPI"]["gates"]  # type: ignore[index]
    evidence = gates["universe_snapshot_coverage"]["evidence"]
    assert evidence["expected_count"] == 1
    assert evidence["actual_count"] == 1
    assert evidence["sealed_count"] == 2
    assert evidence["same_transaction_count_is_not_independent_evidence"] is True


def test_f04_denominator_without_a_sealed_basis_fails_closed() -> None:
    selector_input = _base_input()
    result = select_krb1_p0_liquidity_candidates(
        replace(selector_input, metadata_snapshots=())
    )
    _assert_fail_closed(result, "universe_denominator_has_no_sealed_basis")


# ───── F-INT-03: the coverage denominator needs a basis outside our own read ─────


def test_first_snapshot_of_an_already_truncated_universe_fails_closed() -> None:
    """🔴 F-INT-03: sealing a short read does not make it the whole market.

    ``test_f04_self_consistent_truncated_universe_fails_closed`` covers a truncation
    that happens *after* a snapshot exists — sealed=2 vs actual=1. This is the case
    it cannot cover: the database was already short when the very first snapshot was
    captured, so capture read 1 row, requested that 1 symbol, hashed that payload
    and sealed count 1. expected == actual == sealed == 1 and the coverage gate is
    satisfied. The external listed count is what disagrees.
    """
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
            _with_evidence(
                selector_input,
                universe_rows=truncated,
                candle_rows=candles,
                expected_universe_counts={"KOSPI": 1, "KOSDAQ": 1},
                completed_bar_evidence=tuple(_completed(row) for row in candles),
                quote_timestamp_evidence=tuple(_quote(row.symbol) for row in truncated),
                reference_price_exception_records=tuple(
                    _reference(row.symbol) for row in truncated
                ),
            ),
            # The external source still reports the real market size. Our numbers
            # all agree with each other and all disagree with it.
            external_universe_denominators=tuple(
                _denominator(market, selector_input.universe_rows)
                for market in ("KOSPI", "KOSDAQ")
            ),
        )
    )

    _assert_fail_closed(result, "universe_denominator_external_basis_unproven")
    for market in ("KOSPI", "KOSDAQ"):
        gates = result["market_results"][market]["gates"]  # type: ignore[index]
        coverage = gates["universe_snapshot_coverage"]
        external = gates["universe_denominator_external_basis"]
        # The self-consistent numbers really are self-consistent...
        assert coverage["status"] == "proven"
        assert coverage["evidence"]["sealed_count"] == 1
        # ...and that is not coverage.
        assert external["status"] == "unprovable"
        assert (
            external["evidence"]["defect"]
            == "universe_denominator_disagrees_with_external_basis"
        )
        assert external["evidence"]["sealed_count_is_not_external_basis"] is True


def test_no_external_denominator_means_coverage_is_unprovable() -> None:
    """Production state: nothing attests the listed count, so the run blocks."""
    result = select_krb1_p0_liquidity_candidates(
        replace(_base_input(), external_universe_denominators=())
    )

    _assert_fail_closed(result, "universe_denominator_external_basis_unproven")
    gates = result["market_results"]["KOSPI"]["gates"]  # type: ignore[index]
    assert (
        gates["universe_denominator_external_basis"]["evidence"]["defect"]
        == "no_external_denominator_for_market_session"
    )


def test_the_shipped_denominator_source_is_unwired_so_no_input_proves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 With the real module constants, even a well-formed denominator blocks."""
    monkeypatch.setattr(denominator_module, "EXTERNAL_DENOMINATOR_SOURCE_WIRED", False)
    monkeypatch.setattr(
        denominator_module,
        "ADMISSIBLE_EXTERNAL_DENOMINATOR_SOURCES",
        frozenset(),
    )

    result = select_krb1_p0_liquidity_candidates(_base_input())

    _assert_fail_closed(result, "universe_denominator_external_basis_unproven")
    gates = result["market_results"]["KOSPI"]["gates"]  # type: ignore[index]
    evidence = gates["universe_denominator_external_basis"]["evidence"]
    assert evidence["defect"] == "external_denominator_source_not_wired"
    assert evidence["first_snapshot_of_a_truncated_universe_is_self_proving"] is True


def test_a_self_referential_denominator_source_is_refused() -> None:
    """Our own sealed count relabelled as an external basis is still our own count."""
    for source in ("metadata_snapshot_symbol_count", "db_universe_rows"):
        with pytest.raises(denominator_module.ExternalDenominatorNotWired):
            ExternalUniverseDenominator(
                market="KOSPI",
                session_date=AS_OF,
                source=source,
                listed_count=2,
                published_at=dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST),
                retrieved_at=dt.datetime(2026, 7, 29, 16, 10, tzinfo=KST),
                raw_payload_sha256="b" * 64,
            )


def test_finality_attestation_injection_cannot_bypass_the_unwired_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 F-INT-01 at the selector boundary: the verifier's A3_SELECTOR probe.

    With the real module constants, handing well-formed attestations straight to
    the selector input must not produce ``selected``.
    """
    monkeypatch.setattr(krb1_completion_finality, "FINALITY_SOURCE_WIRED", False)
    monkeypatch.setattr(
        krb1_completion_finality, "ADMISSIBLE_FINALITY_SOURCES", frozenset()
    )

    result = select_krb1_p0_liquidity_candidates(_base_input())

    _assert_fail_closed(result, "completed_session_provider_finality_unproven")
    gates = result["market_results"]["KOSPI"]["gates"]  # type: ignore[index]
    assert (
        gates["completed_session_provider_finality"]["evidence"]["defect"]
        == "provider_finality_source_not_wired"
    )
