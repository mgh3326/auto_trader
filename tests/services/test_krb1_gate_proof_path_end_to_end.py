"""End-to-end (offline): evidence written through the real append-only services.

History: an earlier version of this file asserted
``metadata + completion gates are now provable``. The ROB-1172 correction (08:33)
established that both of those propositions were wrong —

* a consumer retrieval clock is not a provider authority clock (A1), and
* a local full-universe exact reconcile is not provider finality (pending A3).

That assertion was therefore removed rather than adjusted: keeping it would have
frozen the wrong contract into the test suite.

What this file asserts now:

* metadata authority stays ``unprovable`` unless the *provider* sent a clock, and
  becomes ``proven`` when it did — so the block is attributable to the provider,
  not to broken gate logic;
* the run fails closed while the authoritative base-price source is unwired.

🔴 It deliberately makes **no** assertion about the completion gates in either
direction. Splitting completion into local-reconcile vs provider-finality is A3,
which is blocked on the B5 scope decision (#1729 ownership).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from app.services import krb1_metadata_authority
from app.services.krb1_completion_manifest import (
    DbDailyBar,
    RawDailyBar,
    append_completion_manifest,
    build_completion_manifest,
    load_latest_completion_manifest,
)
from app.services.krb1_evidence_chain import EvidenceChainError
from app.services.krb1_metadata_authority import (
    ProviderAuthorityClock,
    SymbolMetadata,
    append_metadata_snapshot,
    load_latest_metadata_snapshot,
)
from app.services.krb1_p0_liquidity_selector import (
    CandleRow,
    CompletedBarEvidence,
    QuoteTimestampCapture,
    SelectorInput,
    UniverseRow,
    select_krb1_p0_liquidity_candidates,
)
from app.services.krb1_reference_exception_adapter import (
    FAIL_CLOSED_REASON,
    fetch_reference_price_exceptions,
)

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
AS_OF = dt.date(2026, 7, 29)
TARGET = dt.date(2026, 7, 30)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
PUBLISHED_AT = dt.datetime(2026, 7, 29, 16, 20, tzinfo=KST)
RETRIEVED_AT = dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST)
OBSERVED_AT = dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST)
FINALIZED_AT = dt.datetime(2026, 7, 29, 16, 45, tzinfo=KST)
MARKETS = ("KOSPI", "KOSDAQ")
UNIVERSE = {"KOSPI": ("000001", "000002"), "KOSDAQ": ("100001", "100002")}
CLOSES = {"000001": 10_003, "000002": 20_003, "100001": 30_003, "100002": 40_003}
VALUES = {"000001": 2_000, "000002": 1_000, "100001": 3_000, "100002": 4_000}
KIS_DAILY_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
KIS_DAILY_TR_ID = "FHKST03010100"


DECLARED_PUBLISHED = frozenset({"publishedAt"})
DECLARED_EFFECTIVE = frozenset({"effectiveSession"})


@pytest.fixture(autouse=True)
def _declared_provider_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hypothetical declared contract; the wired surface declares none (D1)."""
    monkeypatch.setattr(
        krb1_metadata_authority, "PROVIDER_PUBLISHED_AT_FIELDS", DECLARED_PUBLISHED
    )
    monkeypatch.setattr(
        krb1_metadata_authority,
        "PROVIDER_EFFECTIVE_SESSION_FIELDS",
        DECLARED_EFFECTIVE,
    )


def _provider_clock() -> ProviderAuthorityClock:
    """A declared provider clock. The wired Toss surface sends none (A1)."""
    return ProviderAuthorityClock(
        published_at=PUBLISHED_AT,
        published_at_field="publishedAt",
        published_at_raw=PUBLISHED_AT.isoformat(),
        effective_session=AS_OF,
        effective_session_field="effectiveSession",
        effective_session_raw=AS_OF.isoformat(),
    )


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


def _universe() -> tuple[UniverseRow, ...]:
    return tuple(
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
            db_sync_source="db.kr_symbol_universe.toss_master_updated_at",
            db_sync_observed_at=RETRIEVED_AT,
        )
        for market in MARKETS
        for symbol in UNIVERSE[market]
    )


def _selector_input(
    store: Path,
    *,
    provider_clock: ProviderAuthorityClock | None,
    **overrides: object,
) -> SelectorInput:
    universe = _universe()
    candles = tuple(
        _candle(symbol) for market in MARKETS for symbol in UNIVERSE[market]
    )
    snapshot_path = store / "toss_metadata_snapshot.jsonl"
    manifest_path = store / "completion_manifest.jsonl"

    for market in MARKETS:
        if provider_clock is not None:
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
                provider_clock=provider_clock,
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
                endpoint=KIS_DAILY_ENDPOINT,
                tr_id=KIS_DAILY_TR_ID,
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
    return replace(base, **overrides)  # type: ignore[arg-type]


def _gate(result: dict[str, object], market: str, gate: str) -> dict[str, object]:
    markets = result["market_results"]
    assert isinstance(markets, dict)
    gates = markets[market]["gates"]  # type: ignore[index]
    assert isinstance(gates, dict)
    return gates[gate]  # type: ignore[return-value]


def _fail_closed_run(store: Path, *, provider_clock: ProviderAuthorityClock | None):
    fetched = fetch_reference_price_exceptions(
        symbols=[symbol for market in MARKETS for symbol in UNIVERSE[market]],
        target_session=TARGET,
        decision_at=DECISION_AT,
    )
    result = select_krb1_p0_liquidity_candidates(
        _selector_input(
            store,
            provider_clock=provider_clock,
            reference_price_exception_records=fetched.records,
            reference_source_unavailable_reason=fetched.reason,
        )
    )
    return result, fetched


def test_metadata_authority_stays_unprovable_without_a_provider_clock(
    tmp_path: Path,
) -> None:
    """🔴 A1: nothing we do locally can make the metadata gate pass."""
    result, _fetched = _fail_closed_run(tmp_path, provider_clock=None)

    for market in MARKETS:
        gate = _gate(result, market, "metadata_authority_snapshot")
        assert gate["status"] == "unprovable"
        assert gate["reason"] == "authoritative_metadata_snapshot_missing"
    assert result["status"] == "fail_closed"
    assert result["selected_candidates"] == []


def test_metadata_authority_is_provable_once_the_provider_sends_a_clock(
    tmp_path: Path,
) -> None:
    """Attributability: the gate logic works, so the block is the provider's silence."""
    result, fetched = _fail_closed_run(tmp_path, provider_clock=_provider_clock())

    for market in MARKETS:
        gate = _gate(result, market, "metadata_authority_snapshot")
        assert gate["status"] == "proven"
        assert gate["evidence"]["snapshot"]["provider_clock"]["published_at_field"] == (
            "publishedAt"
        )
        assert gate["evidence"]["snapshot"]["retrieval_clock_is_not_authority"] is True

    # The run still fails closed: the authoritative base-price source is unwired.
    assert result["status"] == "fail_closed"
    assert result["selected_candidates"] == []
    # F6-lite: the blocking set must be observable. Membership only — this makes no
    # claim about the completion gates in either direction (A3 pending B5).
    blocking = {
        str(item["gate"])
        for item in result["fail_close_reasons"]  # type: ignore[union-attr]
    }
    assert "reference_price_exception_coverage" in blocking
    assert "metadata_authority_snapshot" not in blocking
    assert "metadata_authority_as_of" not in blocking
    for market in MARKETS:
        reference = _gate(result, market, "reference_price_exception_coverage")
        assert reference["status"] == "unprovable"
        assert reference["evidence"]["source_unavailable_reason"] == FAIL_CLOSED_REASON
    assert fetched.records == ()


def test_appended_evidence_is_chain_verified_on_read(tmp_path: Path) -> None:
    """The selector consumes evidence only through verified append-only reads."""
    _selector_input(tmp_path, provider_clock=_provider_clock())
    snapshot_path = tmp_path / "toss_metadata_snapshot.jsonl"

    loaded = load_latest_metadata_snapshot(snapshot_path, market="KOSPI")
    assert loaded is not None
    assert loaded.chain_index >= 2
    assert loaded.provider_clock == _provider_clock()

    tampered = snapshot_path.read_bytes().replace(
        b'"symbol_count":2', b'"symbol_count":3'
    )
    snapshot_path.write_bytes(tampered)
    with pytest.raises(EvidenceChainError):
        load_latest_metadata_snapshot(snapshot_path, market="KOSPI")
