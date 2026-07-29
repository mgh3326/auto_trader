"""End-to-end: the three blocked gates become provable, the unwired source still blocks.

Offline (no DB, no network): evidence is written through the real append-only
service layer, read back with chain verification, and fed to the selector.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.services.krb1_completion_manifest import (
    DbDailyBar,
    RawDailyBar,
    append_completion_manifest,
    build_completion_manifest,
    load_latest_completion_manifest,
)
from app.services.krb1_metadata_authority import (
    SymbolMetadata,
    append_metadata_snapshot,
    load_latest_metadata_snapshot,
)
from app.services.krb1_p0_liquidity_selector import (
    CandleRow,
    CompletedBarEvidence,
    QuoteTimestampCapture,
    SelectorInput,
    select_krb1_p0_liquidity_candidates,
)
from app.services.krb1_reference_exception_adapter import (
    FAIL_CLOSED_REASON,
    fetch_reference_price_exceptions,
)
from app.services.krb1_reference_price_evidence import ReferencePriceExceptionRecord

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
AS_OF = dt.date(2026, 7, 29)
TARGET = dt.date(2026, 7, 30)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
RETRIEVED_AT = dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST)
OBSERVED_AT = dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST)
FINALIZED_AT = dt.datetime(2026, 7, 29, 16, 45, tzinfo=KST)
MARKETS = ("KOSPI", "KOSDAQ")
UNIVERSE = {"KOSPI": ("000001", "000002"), "KOSDAQ": ("100001", "100002")}
CLOSES = {"000001": 10_003, "000002": 20_003, "100001": 30_003, "100002": 40_003}
VALUES = {"000001": 2_000, "000002": 1_000, "100001": 3_000, "100002": 4_000}


def _candle(symbol: str) -> CandleRow:
    close = CLOSES[symbol]
    return CandleRow(
        session_date=AS_OF,
        symbol=symbol,
        venue="KRX",
        open=close - 100,
        high=close + 100,
        low=close - 200,
        close=close,
        volume=1_000,
        value=VALUES[symbol],
        source="kis",
        ingested_at=OBSERVED_AT,
    )


def _selector_input(store: Path, **overrides: object) -> SelectorInput:
    from app.services.krb1_p0_liquidity_selector import UniverseRow

    universe = tuple(
        UniverseRow(
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
            metadata_as_of=RETRIEVED_AT,
        )
        for market in MARKETS
        for symbol in UNIVERSE[market]
    )
    candles = tuple(
        _candle(symbol) for market in MARKETS for symbol in UNIVERSE[market]
    )

    snapshot_path = store / "toss_metadata_snapshot.jsonl"
    manifest_path = store / "completion_manifest.jsonl"
    for market in MARKETS:
        append_metadata_snapshot(
            snapshot_path,
            source="toss_openapi",
            market=market,
            rows=tuple(
                SymbolMetadata(
                    symbol=row.symbol,
                    exchange=row.exchange,
                    security_type=row.security_type,
                    is_common_share=row.is_common_share,
                    listing_status=row.listing_status,
                    list_date=row.list_date,
                    krx_trading_suspended=row.krx_trading_suspended,
                )
                for row in universe
                if row.exchange == market
            ),
            raw_payload=b'{"result":[{"symbol":"000001"}]}',
            metadata_as_of=RETRIEVED_AT,
            retrieved_at=RETRIEVED_AT,
        )
        market_candles = [row for row in candles if row.symbol in UNIVERSE[market]]
        append_completion_manifest(
            manifest_path,
            build_completion_manifest(
                market=market,
                session_date=AS_OF,
                universe_symbols=UNIVERSE[market],
                raw_bars=[
                    RawDailyBar(
                        symbol=row.symbol,
                        endpoint=(
                            "/uapi/domestic-stock/v1/quotations/"
                            "inquire-daily-itemchartprice"
                        ),
                        tr_id="FHKST03010100",
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
            ),
        )

    snapshots = tuple(
        snapshot
        for market in MARKETS
        if (snapshot := load_latest_metadata_snapshot(snapshot_path, market=market))
    )
    manifests = tuple(
        manifest
        for market in MARKETS
        if (
            manifest := load_latest_completion_manifest(
                manifest_path, market=market, session_date=AS_OF
            )
        )
    )
    base = SelectorInput(
        as_of_session=AS_OF,
        target_session=TARGET,
        decision_at=DECISION_AT,
        expected_universe_counts={"KOSPI": 2, "KOSDAQ": 2},
        universe_rows=universe,
        candle_rows=candles,
        completed_bar_evidence=tuple(
            CompletedBarEvidence(
                symbol=row.symbol,
                endpoint=(
                    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
                ),
                tr_id="FHKST03010100",
                raw_business_date="20260729",
                raw_close=str(row.close),
                raw_volume=str(row.volume),
                raw_value=str(row.value),
                observed_at=OBSERVED_AT,
            )
            for row in candles
        ),
        quote_timestamp_evidence=tuple(
            QuoteTimestampCapture(
                symbol=row.symbol,
                endpoint="/uapi/domestic-stock/v1/quotations/inquire-price",
                tr_id="FHKST01010100",
                raw_symbol=row.symbol,
                raw_business_date="20260729",
                raw_execution_time="153000",
                raw_last_price=str(CLOSES[row.symbol]),
                captured_at=dt.datetime(2026, 7, 29, 15, 40, tzinfo=KST),
            )
            for row in universe
        ),
        metadata_snapshots=snapshots,
        completion_manifests=manifests,
    )
    from dataclasses import replace

    return replace(base, **overrides)  # type: ignore[arg-type]


def _gate(result: dict[str, object], market: str, gate: str) -> dict[str, object]:
    markets = result["market_results"]
    assert isinstance(markets, dict)
    gates = markets[market]["gates"]  # type: ignore[index]
    assert isinstance(gates, dict)
    return gates[gate]  # type: ignore[return-value]


def test_metadata_and_completion_gates_are_now_provable(tmp_path: Path) -> None:
    fetched = fetch_reference_price_exceptions(
        symbols=[symbol for market in MARKETS for symbol in UNIVERSE[market]],
        target_session=TARGET,
        decision_at=DECISION_AT,
    )
    result = select_krb1_p0_liquidity_candidates(
        _selector_input(
            tmp_path,
            reference_price_exception_records=fetched.records,
            reference_source_unavailable_reason=fetched.reason,
        )
    )

    for market in MARKETS:
        assert _gate(result, market, "metadata_authority_as_of")["status"] == "proven"
        assert (
            _gate(result, market, "metadata_authority_snapshot")["status"] == "proven"
        )
        assert (
            _gate(result, market, "completed_session_raw_completion")["status"]
            == "proven"
        )
        assert (
            _gate(result, market, "completed_session_completion_manifest")["status"]
            == "proven"
        )

    # The only remaining blocker is the unwired authoritative base-price source.
    assert result["status"] == "fail_closed"
    assert result["selected_candidates"] == []
    blocking_gates = {
        item["gate"]
        for item in result["fail_close_reasons"]  # type: ignore[union-attr]
    }
    assert blocking_gates == {
        "reference_price_exception_coverage",
        "ranked_candidate",
        "completed_close",
        "selected_quote_raw_timestamp",
        "two_market_selection",
    }
    for market in MARKETS:
        gate = _gate(result, market, "reference_price_exception_coverage")
        assert gate["reason"] == "target_session_reference_price_exception_unproven"
        assert gate["evidence"]["source_unavailable_reason"] == FAIL_CLOSED_REASON  # type: ignore[index]


def test_wiring_an_authoritative_source_completes_the_selection(
    tmp_path: Path,
) -> None:
    """The remaining block is attributable to the source, not to a broken gate."""
    records = tuple(
        ReferencePriceExceptionRecord(
            symbol=symbol,
            effective_session=TARGET,
            is_exception=False,
            source="krx_official_base_price",
            published_at=dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST),
            retrieved_at=RETRIEVED_AT,
            raw_reference_price=str(CLOSES[symbol]),
            raw_reason_code="NORMAL",
            raw_payload_sha256="d" * 64,
        )
        for market in MARKETS
        for symbol in UNIVERSE[market]
    )

    result = select_krb1_p0_liquidity_candidates(
        _selector_input(tmp_path, reference_price_exception_records=records)
    )

    assert result["status"] == "selected", result["fail_close_reasons"]
    selected = {
        str(row["market"]): row
        for row in result["selected_candidates"]  # type: ignore[union-attr]
    }
    assert selected["KOSPI"]["universe_row"]["symbol"] == "000001"
    assert selected["KOSDAQ"]["universe_row"]["symbol"] == "100002"
