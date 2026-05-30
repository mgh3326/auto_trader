"""ROB-273 — snapshot-backed collector tests.

Each test verifies that the collector emits a well-formed
:class:`SnapshotCollectResult` and never reaches into broker /
order / watch / scheduler write paths.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.invest_page import (
    InvestPageSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.journal import (
    JournalSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.market import (
    MarketEventsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.news import (
    NewsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.optional_stubs import (
    BrowserProbeStubCollector,
    NaverRemoteDebugStubCollector,
    TossRemoteDebugStubCollector,
)
from app.services.action_report.snapshot_backed.collectors.portfolio import (
    PortfolioSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
from app.services.action_report.snapshot_backed.collectors.symbol import (
    SymbolSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.watch_context import (
    WatchContextSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


def _request(market: str = "kr", account_scope: str = "kis_live") -> CollectorRequest:
    return CollectorRequest(
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
    )


# ---------------------------------------------------------------------------
# Portfolio collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_portfolio_collector_returns_holdings(monkeypatch: pytest.MonkeyPatch):
    """v1 manual-primary path remains for non-(kis_live) combos.

    ROB-278 reserved kr+kis_live and ROB-297 reserved us+kis_live for the
    KIS live path (see test_portfolio_v2_*). Other combos keep the v1
    contract and additionally surface ``primary_source="manual"``.
    """
    from app.models.manual_holdings import MarketType

    session = MagicMock()

    class _Row:
        ticker = "AAPL"
        market_type = MarketType.US
        quantity = 10
        avg_price = 150.0
        display_name = "Apple"
        updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[_Row()])
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=result)

    collector = PortfolioSnapshotCollector(session)
    # us + alpaca_paper is a non-canonical (collector-only) combo that still
    # falls back to manual_primary — exactly the contract this test asserts.
    results = await collector.collect(
        _request(market="us", account_scope="alpaca_paper")
    )
    assert len(results) == 1
    assert results[0].snapshot_kind == "portfolio"
    assert results[0].source_kind == "auto_trader_mcp"
    assert results[0].payload_json["count"] == 1
    assert results[0].payload_json["holdings"][0]["ticker"] == "AAPL"
    # ROB-278 — payload v2 surfaces primary_source even on the v1 path.
    assert results[0].payload_json["primary_source"] == "manual"


@pytest.mark.asyncio
async def test_portfolio_collector_empty_holdings_returns_partial():
    """No matching holdings → result still emitted, status='partial'."""
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=[]))
    result = MagicMock(scalars=MagicMock(return_value=scalars))
    session.execute = AsyncMock(return_value=result)

    collector = PortfolioSnapshotCollector(session)
    results = await collector.collect(
        _request(market="us", account_scope="alpaca_paper")
    )
    assert len(results) == 1
    assert results[0].snapshot_kind == "portfolio"
    assert results[0].freshness_status == "partial"
    assert results[0].payload_json["count"] == 0


# ---------------------------------------------------------------------------
# Portfolio v2 — ROB-278 KIS live path for KR + kis_live.
#
# Lockdown policy:
# - user_id missing on kis_live → fail-closed (unavailable, no implicit default).
# - KIS success → primary_source="kis"; manual rows go to reference_holdings.
# - KIS failure → primary_source="none"; manual NEVER promoted to primary.
# - Payload v2 is additive: existing v1 keys (holdings, count, market) preserved.
# - Non-(kr+kis_live) combos preserve v1 manual-primary behaviour.
# ---------------------------------------------------------------------------
def _kr_kis_request(user_id: int | None = None) -> CollectorRequest:
    return CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
        user_id=user_id,
    )


def _empty_manual_session() -> MagicMock:
    """Session whose execute() returns no manual_holdings rows."""
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=[]))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


def _manual_kr_session(rows: list[Any] | None = None) -> MagicMock:
    from app.models.manual_holdings import MarketType

    class _ManualRow:
        def __init__(
            self,
            ticker: str = "005930",
            quantity: float = 5.0,
            avg_price: float = 70_000,
        ) -> None:
            self.ticker = ticker
            self.market_type = MarketType.KR
            self.quantity = quantity
            self.avg_price = avg_price
            self.display_name = ticker
            self.updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    rows = rows if rows is not None else [_ManualRow()]
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=rows))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


def _kis_reader_with_holdings() -> MagicMock:
    """KISHomeReader stub returning one KR holding + KRW cash."""
    from app.schemas.invest_home import Account, CashAmounts, Holding
    from app.services.invest_home_service import _SourceFetchResult

    holding_kr = Holding(
        holdingId="kis:kr:005930",
        accountId="kis_account",
        source="kis",
        accountKind="live",
        symbol="005930",
        market="KR",
        assetType="equity",
        assetCategory="kr_stock",
        displayName="삼성전자",
        quantity=10.0,
        averageCost=70_000,
        costBasis=700_000,
        currency="KRW",
        valueNative=750_000,
        valueKrw=750_000,
        pnlKrw=50_000,
        pnlRate=0.0714,
        sellableQuantity=8.0,
        pendingSellQuantity=2.0,
        referenceQuantity=0.0,
    )
    account = Account(
        accountId="kis_account",
        displayName="KIS 실계좌",
        source="kis",
        accountKind="live",
        includedInHome=True,
        valueKrw=750_000,
        costBasisKrw=700_000,
        pnlKrw=50_000,
        pnlRate=0.0714,
        cashBalances=CashAmounts(krw=1_200_000.0, usd=None),
        buyingPower=CashAmounts(krw=1_000_000.0, usd=None),
    )
    reader = MagicMock()
    reader.fetch = AsyncMock(
        return_value=_SourceFetchResult(
            accounts=[account], holdings=[holding_kr], warning=None
        )
    )
    return reader


def _kis_reader_failed() -> MagicMock:
    from app.schemas.invest_home import InvestHomeWarning
    from app.services.invest_home_service import _SourceFetchResult

    reader = MagicMock()
    reader.fetch = AsyncMock(
        return_value=_SourceFetchResult(
            accounts=[],
            holdings=[],
            warning=InvestHomeWarning(source="kis", message="connection timeout"),
        )
    )
    return reader


@pytest.mark.asyncio
async def test_portfolio_v2_kr_kis_live_missing_user_id_is_fail_closed():
    """ROB-278 — no user_id on kis_live request → unavailable, no implicit default."""
    session = _empty_manual_session()
    reader = MagicMock()
    reader.fetch = AsyncMock(
        side_effect=AssertionError("KIS must not be called without user_id")
    )
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_kr_kis_request(user_id=None))
    assert len(results) == 1
    assert results[0].snapshot_kind == "portfolio"
    assert results[0].freshness_status == "unavailable"
    assert "user_id" in results[0].errors_json["reason"]
    # ROB-318 Slice 1 — collector emits the closed reason_code for the gate.
    assert results[0].errors_json["reason_code"] == "user_id_missing"
    # primary_source label is present and explicitly "none" (manual NOT promoted).
    assert results[0].payload_json.get("primary_source") == "none"
    reader.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_portfolio_v2_kr_kis_live_success_populates_kis_primary():
    """ROB-278 — KIS success: primary_source=kis, KIS holdings primary, manual in reference."""
    session = _manual_kr_session()
    reader = _kis_reader_with_holdings()
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_kr_kis_request(user_id=42))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "fresh"
    # v1 keys preserved (additive shape).
    assert "holdings" in payload
    assert "count" in payload
    assert "market" in payload
    # v2 fields.
    assert payload["primary_source"] == "kis"
    assert payload["count"] == 1
    assert payload["holdings"][0]["ticker"] == "005930"
    assert payload["holdings"][0]["source"] == "kis"
    assert payload["holdings"][0]["sellable_quantity"] == 8.0
    assert payload["holdings"][0]["pending_sell_quantity"] == 2.0
    assert payload["cash"]["krw"] == 1_200_000.0
    assert payload["buying_power"]["krw"] == 1_000_000.0
    assert payload["sellable_summary"]["sellable_count"] == 1
    # Manual KR row appears in reference_holdings, NOT in holdings.
    assert payload["reference_holdings"][0]["ticker"] == "005930"
    assert payload["reference_holdings"][0]["source"] == "manual"
    # Provenance.
    assert payload["provenance"]["kis_fetch_status"] == "ok"
    assert payload["provenance"]["account_scope"] == "kis_live"
    reader.fetch.assert_awaited_once_with(user_id=42)


@pytest.mark.asyncio
async def test_portfolio_v2_kr_kis_live_failure_does_not_promote_manual():
    """ROB-278 — KIS failure: primary_source=none, manual stays in reference_holdings."""
    session = _manual_kr_session()
    reader = _kis_reader_failed()
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_kr_kis_request(user_id=42))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert payload["holdings"] == []
    assert payload["count"] == 0
    # Manual remains visible as reference, never promoted to primary.
    assert len(payload["reference_holdings"]) == 1
    assert payload["reference_holdings"][0]["source"] == "manual"
    # Provenance carries the failure reason.
    assert payload["provenance"]["kis_fetch_status"] == "failed"
    assert "kis" in str(payload["provenance"]["warnings"]).lower()


@pytest.mark.asyncio
async def test_portfolio_v2_kr_kis_live_exception_is_fail_closed():
    """ROB-278 — KISHomeReader raising is treated like 'failed', not crash."""
    session = _manual_kr_session()
    reader = MagicMock()
    reader.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_kr_kis_request(user_id=42))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert "boom" in payload["provenance"]["errors"][0]


# ---------------------------------------------------------------------------
# Portfolio v2 — ROB-369 E9: crypto + upbit_live reads the live Upbit account.
#
# Before ROB-369 the crypto/upbit_live combo fell through to the v1
# manual-primary path, so the portfolio snapshot carried no live NAV / cash /
# buying power. The Hermes portfolio stage then reported "NAV=0,
# buying_power_krw=0" for a real Upbit account (eval ~25.75M KRW) and misfired
# a "buying_power < 5% NAV" risk on a 0/0 artifact. The collector now routes
# crypto+upbit_live to ``UpbitHomeReader`` (mirroring the KIS-live path): live
# holdings + KRW cash/orderable become primary; manual CRYPTO rows stay
# reference-only and are never promoted.
# ---------------------------------------------------------------------------
def _crypto_upbit_request(user_id: int | None = 1) -> CollectorRequest:
    return CollectorRequest(
        market="crypto",
        account_scope="upbit_live",
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
        user_id=user_id,
    )


def _manual_crypto_session(rows: list[Any] | None = None) -> MagicMock:
    from app.models.manual_holdings import MarketType

    class _ManualCryptoRow:
        def __init__(
            self,
            ticker: str = "KRW-BTC",
            quantity: float = 0.01,
            avg_price: float = 40_000_000.0,
        ) -> None:
            self.ticker = ticker
            self.market_type = MarketType.CRYPTO
            self.quantity = quantity
            self.avg_price = avg_price
            self.display_name = ticker
            self.updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    rows = rows if rows is not None else [_ManualCryptoRow()]
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=rows))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


def _upbit_reader_with_holdings(
    *,
    warning: Any | None = None,
    cash_krw: float | None = 365_342.0,
    hidden_dust: int = 0,
    hidden_inactive: int = 0,
) -> MagicMock:
    """UpbitHomeReader stub: one live BTC holding + KRW cash/orderable.

    Upbit reports the same KRW figure for cash balance and buying power
    (orderable), and never carries a USD leg — mirrors the real reader.
    ``cash_krw=None`` models a KRW-less (all-coin) account; ``hidden_*`` model
    the reader's dust/inactive filtering.
    """
    from app.schemas.invest_home import (
        Account,
        CashAmounts,
        Holding,
        InvestHomeHiddenCounts,
    )
    from app.services.invest_home_service import _SourceFetchResult

    holding_btc = Holding(
        holdingId="upbit:BTC",
        accountId="upbit_account",
        source="upbit",
        accountKind="live",
        symbol="BTC",
        market="CRYPTO",
        assetType="crypto",
        assetCategory="crypto",
        displayName="BTC",
        quantity=0.235,
        averageCost=90_000_000.0,
        costBasis=21_150_000.0,
        currency="KRW",
        valueNative=25_384_658.0,
        valueKrw=25_384_658.0,
        pnlKrw=4_234_658.0,
        pnlRate=0.20,
        priceState="live",
    )
    account = Account(
        accountId="upbit_account",
        displayName="Upbit",
        source="upbit",
        accountKind="live",
        includedInHome=True,
        valueKrw=25_384_658.0,
        costBasisKrw=21_150_000.0,
        pnlKrw=4_234_658.0,
        pnlRate=0.20,
        cashBalances=CashAmounts(krw=cash_krw, usd=None),
        buyingPower=CashAmounts(krw=cash_krw, usd=None),
    )
    hidden_counts = InvestHomeHiddenCounts()
    hidden_counts.upbitDust = hidden_dust
    hidden_counts.upbitInactive = hidden_inactive
    reader = MagicMock()
    reader.fetch = AsyncMock(
        return_value=_SourceFetchResult(
            accounts=[account],
            holdings=[holding_btc],
            warning=warning,
            hidden_counts=hidden_counts,
        )
    )
    return reader


def _upbit_reader_failed() -> MagicMock:
    from app.schemas.invest_home import InvestHomeWarning
    from app.services.invest_home_service import _SourceFetchResult

    reader = MagicMock()
    reader.fetch = AsyncMock(
        return_value=_SourceFetchResult(
            accounts=[],
            holdings=[],
            warning=InvestHomeWarning(source="upbit", message="upbit auth refused"),
        )
    )
    return reader


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_success_populates_upbit_primary():
    """ROB-369 E9 — Upbit live success: primary_source=upbit, live KRW NAV/cash,
    manual CRYPTO rows surface as reference only (never promoted)."""
    session = _manual_crypto_session()
    upbit_reader = _upbit_reader_with_holdings()
    # KIS reader MUST NOT be touched for upbit_live.
    kis_reader = MagicMock()
    kis_reader.fetch = AsyncMock(
        side_effect=AssertionError("KIS reader called for upbit_live request")
    )
    collector = PortfolioSnapshotCollector(
        session, kis_reader=kis_reader, upbit_reader=upbit_reader
    )
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "fresh"
    assert payload["primary_source"] == "upbit"
    assert payload["count"] == 1
    assert payload["holdings"][0]["ticker"] == "BTC"
    assert payload["holdings"][0]["source"] == "upbit"
    assert payload["holdings"][0]["value_krw"] == 25_384_658.0
    # KRW cash + orderable surfaced — the E9 fix (not None/0).
    assert payload["cash"]["krw"] == 365_342.0
    assert payload["buying_power"]["krw"] == 365_342.0
    # Crypto has no pending-sell concept on the reader.
    assert payload["sellable_summary"] is None
    # Manual CRYPTO row visible as reference, never promoted to primary.
    assert len(payload["reference_holdings"]) == 1
    assert payload["reference_holdings"][0]["ticker"] == "KRW-BTC"
    assert payload["reference_holdings"][0]["source"] == "manual"
    # Provenance.
    assert payload["provenance"]["upbit_fetch_status"] == "ok"
    assert payload["provenance"]["account_scope"] == "upbit_live"
    upbit_reader.fetch.assert_awaited_once_with(user_id=1)
    kis_reader.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_failure_does_not_promote_manual():
    """ROB-369 E9 — Upbit fetch failure: primary_source=none, manual stays reference."""
    session = _manual_crypto_session()
    upbit_reader = _upbit_reader_failed()
    collector = PortfolioSnapshotCollector(session, upbit_reader=upbit_reader)
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert payload["holdings"] == []
    assert payload["count"] == 0
    assert payload["cash"] is None
    assert payload["buying_power"] is None
    # Manual remains visible as reference, never promoted to primary.
    assert len(payload["reference_holdings"]) == 1
    assert payload["reference_holdings"][0]["source"] == "manual"
    assert payload["provenance"]["upbit_fetch_status"] == "failed"
    assert "upbit" in str(payload["provenance"]["warnings"]).lower()


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_exception_is_fail_closed():
    """ROB-369 E9 — UpbitHomeReader raising is treated like 'failed', not a crash."""
    session = _manual_crypto_session()
    upbit_reader = MagicMock()
    upbit_reader.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    collector = PortfolioSnapshotCollector(session, upbit_reader=upbit_reader)
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert "boom" in payload["provenance"]["errors"][0]


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_price_warning_is_partial():
    """ROB-369 E9 — a price warning with live holdings → partial, still upbit primary."""
    from app.schemas.invest_home import InvestHomeWarning

    session = _manual_crypto_session()
    upbit_reader = _upbit_reader_with_holdings(
        warning=InvestHomeWarning(
            source="upbit",
            message="일부 코인은 현재가가 없어 평가금액에서 제외했습니다.",
        )
    )
    collector = PortfolioSnapshotCollector(session, upbit_reader=upbit_reader)
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    payload = results[0].payload_json
    assert results[0].freshness_status == "partial"
    assert payload["primary_source"] == "upbit"
    assert payload["provenance"]["upbit_fetch_status"] == "partial"
    assert payload["count"] == 1


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_krw_less_account_emits_explicit_zero():
    """ROB-369 E9 — a KRW-less (all-coin) Upbit account reports krw=None on the
    reader. The collector must coerce to an explicit 0.0 (the honest value for
    Upbit, unlike KIS-overseas None=unsupported) so the portfolio citation
    ``$.buying_power.krw`` resolves to a real number, not null."""
    session = _manual_crypto_session()
    upbit_reader = _upbit_reader_with_holdings(cash_krw=None)
    collector = PortfolioSnapshotCollector(session, upbit_reader=upbit_reader)
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    payload = results[0].payload_json
    # Successful fetch — a 0-KRW coin-only account is a complete, valid state.
    assert results[0].freshness_status == "fresh"
    assert payload["primary_source"] == "upbit"
    # Explicit 0.0, never None — the citation must point at a real value.
    assert payload["cash"]["krw"] == 0.0
    assert payload["buying_power"]["krw"] == 0.0
    assert payload["cash"]["krw"] is not None
    assert payload["buying_power"]["krw"] is not None


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_surfaces_hidden_dust_count():
    """ROB-369 E9 — the reader hides dust (<5000 KRW) and inactive coins, so the
    snapshot NAV undercounts the raw Upbit eval. Surface the hidden counts in
    coverage so the divergence is auditable rather than silent."""
    session = _manual_crypto_session()
    upbit_reader = _upbit_reader_with_holdings(hidden_dust=3, hidden_inactive=2)
    collector = PortfolioSnapshotCollector(session, upbit_reader=upbit_reader)
    results = await collector.collect(_crypto_upbit_request(user_id=1))
    coverage = results[0].coverage_json
    assert coverage["hidden_dust_count"] == 3
    assert coverage["hidden_inactive_count"] == 2


# ---------------------------------------------------------------------------
# Portfolio v2 — ROB-297 KIS live path for US + kis_live.
#
# Guardrails (ROB-297 pre-implementation comment):
#   - market="us" + account_scope="kis_live" is canonical KIS overseas.
#   - Toss/manual US holdings stay reference-only — never summed into the
#     KIS-primary ``holdings`` or ``sellable_summary``.
#   - KIS unavailable → primary_source="none"; manual NEVER promoted.
# ---------------------------------------------------------------------------
def _us_kis_request(user_id: int | None = None) -> CollectorRequest:
    return CollectorRequest(
        market="us",
        account_scope="kis_live",
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
        user_id=user_id,
    )


def _manual_us_session(rows: list[Any] | None = None) -> MagicMock:
    """Session whose execute() returns Toss-style manual US holdings.

    Default row is intentionally a different quantity from the KIS holding
    fixture so any accidental KIS+manual sum surfaces as a test failure.
    """
    from app.models.manual_holdings import MarketType

    class _ManualUSRow:
        def __init__(
            self,
            ticker: str = "AAPL",
            quantity: float = 5.0,
            avg_price: float = 140.0,
        ) -> None:
            self.ticker = ticker
            self.market_type = MarketType.US
            self.quantity = quantity
            self.avg_price = avg_price
            self.display_name = ticker
            self.updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    rows = rows if rows is not None else [_ManualUSRow()]
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=rows))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


def _kis_reader_with_us_holdings() -> MagicMock:
    """KISHomeReader stub returning one US holding + USD cash.

    The KIS quantity (10.0) is deliberately different from the
    manual/Toss row default (5.0) so the Toss-no-sum invariant is
    observable in assertions.
    """
    from app.schemas.invest_home import Account, CashAmounts, Holding
    from app.services.invest_home_service import _SourceFetchResult

    holding_us = Holding(
        holdingId="kis:us:AAPL",
        accountId="kis_overseas",
        source="kis",
        accountKind="live",
        symbol="AAPL",
        market="US",
        assetType="equity",
        assetCategory="us_stock",
        displayName="Apple",
        quantity=10.0,
        averageCost=150.0,
        costBasis=1_500.0,
        currency="USD",
        valueNative=1_700.0,
        valueKrw=2_300_000.0,
        pnlKrw=270_000.0,
        pnlRate=0.1333,
        sellableQuantity=8.0,
        pendingSellQuantity=2.0,
        referenceQuantity=0.0,
    )
    # KIS also exposes any KR-side holdings on the same account fetch;
    # include one to verify the US branch filters by ``h.market == "US"``
    # and does not mix domestic holdings into the US payload.
    holding_kr_noise = Holding(
        holdingId="kis:kr:005930",
        accountId="kis_overseas",
        source="kis",
        accountKind="live",
        symbol="005930",
        market="KR",
        assetType="equity",
        assetCategory="kr_stock",
        displayName="삼성전자",
        quantity=99.0,
        averageCost=70_000,
        costBasis=6_930_000,
        currency="KRW",
        valueNative=7_000_000,
        valueKrw=7_000_000,
        pnlKrw=70_000,
        pnlRate=0.01,
        sellableQuantity=99.0,
        pendingSellQuantity=0.0,
        referenceQuantity=0.0,
    )
    account = Account(
        accountId="kis_overseas",
        displayName="KIS 해외주식",
        source="kis",
        accountKind="live",
        includedInHome=True,
        valueKrw=2_300_000.0,
        costBasisKrw=2_000_000.0,
        pnlKrw=300_000.0,
        pnlRate=0.15,
        cashBalances=CashAmounts(krw=None, usd=12_500.0),
        buyingPower=CashAmounts(krw=None, usd=10_000.0),
    )
    reader = MagicMock()
    reader.fetch = AsyncMock(
        return_value=_SourceFetchResult(
            accounts=[account],
            holdings=[holding_us, holding_kr_noise],
            warning=None,
        )
    )
    return reader


def _kis_reader_us_failed() -> MagicMock:
    from app.schemas.invest_home import InvestHomeWarning
    from app.services.invest_home_service import _SourceFetchResult

    reader = MagicMock()
    reader.fetch = AsyncMock(
        return_value=_SourceFetchResult(
            accounts=[],
            holdings=[],
            warning=InvestHomeWarning(source="kis", message="overseas auth refused"),
        )
    )
    return reader


@pytest.mark.asyncio
async def test_portfolio_v2_us_kis_live_missing_user_id_is_fail_closed():
    """ROB-297 — no user_id on us+kis_live → unavailable, manual not promoted."""
    session = _manual_us_session()
    reader = MagicMock()
    reader.fetch = AsyncMock(
        side_effect=AssertionError("KIS must not be called without user_id")
    )
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_us_kis_request(user_id=None))
    assert len(results) == 1
    assert results[0].snapshot_kind == "portfolio"
    assert results[0].freshness_status == "unavailable"
    assert "user_id" in results[0].errors_json["reason"]
    payload = results[0].payload_json
    assert payload["primary_source"] == "none"
    assert payload["holdings"] == []
    # Manual US row stays visible as reference (never promoted).
    assert len(payload["reference_holdings"]) == 1
    assert payload["reference_holdings"][0]["source"] == "manual"
    reader.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_portfolio_v2_us_kis_live_success_populates_kis_primary_with_toss_reference():
    """ROB-297 — KIS US success: primary_source=kis, USD cash, Toss in reference.

    Toss-no-sum invariant (ROB-297 guardrail #3): manual US quantity (5)
    must NEVER be summed into KIS US holding (10) or sellable_summary.
    """
    session = _manual_us_session()
    reader = _kis_reader_with_us_holdings()
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_us_kis_request(user_id=42))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "fresh"
    # KIS-primary holdings filtered to market="US"; KR noise stays out.
    assert payload["primary_source"] == "kis"
    assert payload["count"] == 1
    assert payload["holdings"][0]["ticker"] == "AAPL"
    assert payload["holdings"][0]["source"] == "kis"
    assert payload["holdings"][0]["quantity"] == 10.0  # NOT 10+5 (Toss-no-sum).
    assert payload["holdings"][0]["sellable_quantity"] == 8.0
    assert payload["holdings"][0]["pending_sell_quantity"] == 2.0
    # USD cash + buying_power surfaced for US.
    assert payload["cash"]["usd"] == 12_500.0
    assert payload["buying_power"]["usd"] == 10_000.0
    # Sellable summary counts KIS holdings only — NOT Toss quantity.
    assert payload["sellable_summary"]["sellable_count"] == 1
    # Toss/manual US row appears in reference_holdings (untouched, source="manual").
    assert len(payload["reference_holdings"]) == 1
    assert payload["reference_holdings"][0]["ticker"] == "AAPL"
    assert payload["reference_holdings"][0]["quantity"] == 5.0
    assert payload["reference_holdings"][0]["source"] == "manual"
    # Provenance.
    assert payload["provenance"]["kis_fetch_status"] == "ok"
    assert payload["provenance"]["account_scope"] == "kis_live"
    reader.fetch.assert_awaited_once_with(user_id=42)


@pytest.mark.asyncio
async def test_portfolio_v2_us_kis_live_failure_does_not_promote_manual():
    """ROB-297 — KIS US failure: primary_source=none, manual stays reference."""
    session = _manual_us_session()
    reader = _kis_reader_us_failed()
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_us_kis_request(user_id=42))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert payload["holdings"] == []
    assert payload["count"] == 0
    # Manual remains visible as reference, never promoted to primary.
    assert len(payload["reference_holdings"]) == 1
    assert payload["reference_holdings"][0]["source"] == "manual"
    # Provenance carries the failure reason.
    assert payload["provenance"]["kis_fetch_status"] == "failed"
    assert "kis" in str(payload["provenance"]["warnings"]).lower()


@pytest.mark.asyncio
async def test_portfolio_v2_us_kis_live_exception_is_fail_closed():
    """ROB-297 — KISHomeReader raising on US is treated like 'failed', not crash."""
    session = _manual_us_session()
    reader = MagicMock()
    reader.fetch = AsyncMock(side_effect=RuntimeError("overseas endpoint down"))
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_us_kis_request(user_id=42))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert payload["holdings"] == []
    # Manual US row remains visible.
    assert len(payload["reference_holdings"]) == 1
    assert payload["provenance"]["kis_fetch_status"] == "failed"


# ---------------------------------------------------------------------------
# Journal collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_journal_collector_returns_active_and_recent():
    session = MagicMock()

    class _ActiveJournal:
        id = 1
        symbol = "005930"
        instrument_type = MagicMock(value="kr_stock")
        side = "buy"
        status = "active"
        entry_price = 70_000
        quantity = 10
        thesis = "thesis"
        strategy = "swing"
        target_price = 80_000
        stop_loss = 65_000
        hold_until = None
        exit_price = None
        exit_reason = None
        pnl_pct = None
        account_type = "live"
        created_at = dt.datetime(2026, 5, 18, tzinfo=dt.UTC)
        updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    active_scalars = MagicMock(all=MagicMock(return_value=[_ActiveJournal()]))
    active_result = MagicMock(scalars=MagicMock(return_value=active_scalars))
    recent_scalars = MagicMock(all=MagicMock(return_value=[]))
    recent_result = MagicMock(scalars=MagicMock(return_value=recent_scalars))
    session.execute = AsyncMock(side_effect=[active_result, recent_result])

    collector = JournalSnapshotCollector(session)
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].snapshot_kind == "journal"
    assert results[0].payload_json["active_count"] == 1
    assert results[0].payload_json["retrospective_count"] == 0


# ---------------------------------------------------------------------------
# Watch-context collector — MUST NOT call activation paths
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_watch_context_collector_uses_only_read_methods():
    """The collector reads via list_active_alerts and never touches activation."""

    session = MagicMock()
    repo = MagicMock()

    class _Alert:
        alert_uuid = "11111111-1111-1111-1111-111111111111"
        source_report_uuid = "22222222-1111-1111-1111-111111111111"
        source_item_uuid = "33333333-1111-1111-1111-111111111111"
        market = "kr"
        symbol = "005930"
        metric = "price"
        operator = "above"
        threshold = 80_000
        threshold_key = "80000"
        intent = "buy_review"
        action_mode = "notify_only"
        rationale = "rationale"
        valid_until = dt.datetime(2026, 5, 20, tzinfo=dt.UTC)
        status = "active"
        activated_at = dt.datetime(2026, 5, 18, tzinfo=dt.UTC)

    repo.list_active_alerts = AsyncMock(return_value=[_Alert()])
    # Force the test to fail if the collector tries to activate/insert/transition.
    repo.insert_alert = MagicMock(
        side_effect=AssertionError("collector must not insert_alert")
    )
    repo.update_alert_status = MagicMock(
        side_effect=AssertionError("collector must not update_alert_status")
    )

    collector = WatchContextSnapshotCollector(session, repository=repo)
    results = await collector.collect(_request())
    assert results[0].snapshot_kind == "watch_context"
    assert results[0].payload_json["active_count"] == 1
    repo.list_active_alerts.assert_awaited_once()
    # Mutation methods must not have been called.
    assert not repo.insert_alert.called
    assert not repo.update_alert_status.called


# ---------------------------------------------------------------------------
# Market collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_market_collector_returns_events():
    from app.schemas.market_events import MarketEventsRangeResponse

    session = MagicMock()
    query = MagicMock()
    query.list_for_range = AsyncMock(
        return_value=MarketEventsRangeResponse(
            from_date=dt.date(2026, 5, 19),
            to_date=dt.date(2026, 5, 20),
            count=0,
            events=[],
        )
    )
    collector = MarketEventsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].snapshot_kind == "market"
    assert results[0].payload_json["event_count"] == 0


@pytest.mark.asyncio
async def test_market_collector_query_failure_returns_unavailable():
    session = MagicMock()
    query = MagicMock()
    query.list_for_range = AsyncMock(side_effect=RuntimeError("boom"))
    collector = MarketEventsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"
    assert "boom" in results[0].errors_json["reason"]


def _empty_events_query() -> MagicMock:
    from app.schemas.market_events import MarketEventsRangeResponse

    query = MagicMock()
    query.list_for_range = AsyncMock(
        return_value=MarketEventsRangeResponse(
            from_date=dt.date(2026, 5, 19),
            to_date=dt.date(2026, 5, 20),
            count=0,
            events=[],
        )
    )
    return query


@pytest.mark.asyncio
async def test_market_collector_us_populates_indices_dict():
    # ROB-366 B5: market-conditioned US index set, list-rows adapted into the
    # dict-of-dicts MarketStage reads, with change_pct → change_percent.
    captured: dict = {}

    async def fake_index_fn(symbols):
        captured["symbols"] = list(symbols)
        return [
            {"symbol": "SPX", "name": "S&P 500", "current": 5000.0, "change_pct": 1.1},
            {
                "symbol": "NASDAQ",
                "name": "NASDAQ",
                "current": 16000.0,
                "change_pct": 0.8,
            },
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="us"))
    payload = results[0].payload_json
    assert payload["indices"]["SPX"]["change_percent"] == 1.1
    assert payload["indices"]["NASDAQ"]["change_percent"] == 0.8
    assert "events" in payload  # events payload still emitted
    # market-conditioned symbol set requested from the source
    assert "SPX" in captured["symbols"] and "NASDAQ" in captured["symbols"]


@pytest.mark.asyncio
async def test_market_collector_kr_populates_kospi():
    async def fake_index_fn(symbols):
        return [
            {"symbol": "KOSPI", "name": "코스피", "current": 2700.0, "change_pct": 0.5},
            {
                "symbol": "KOSDAQ",
                "name": "코스닥",
                "current": 850.0,
                "change_pct": -0.2,
            },
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="kr"))
    payload = results[0].payload_json
    assert payload["indices"]["KOSPI"]["change_percent"] == 0.5
    assert "events" in payload


@pytest.mark.asyncio
async def test_market_collector_omits_index_with_none_change_pct():
    # yfinance previous_close missing → change_pct None must be omitted, never 0.0.
    async def fake_index_fn(symbols):
        return [{"symbol": "SPX", "name": "S&P 500", "change_pct": None}]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="us"))
    payload = results[0].payload_json
    assert payload.get("indices", {}) == {}


@pytest.mark.asyncio
async def test_market_collector_index_fetch_failure_is_soft():
    async def fake_index_fn(symbols):
        raise RuntimeError("yfinance down")

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="us"))
    # Index failure is soft: events payload still emitted, snapshot still fresh.
    assert results[0].freshness_status == "fresh"
    assert "events" in results[0].payload_json
    assert results[0].payload_json.get("indices", {}) == {}


@pytest.mark.asyncio
async def test_market_collector_crypto_emits_no_indices():
    called = {"hit": False}

    async def fake_index_fn(symbols):
        called["hit"] = True
        return []

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="crypto"))
    assert results[0].payload_json.get("indices", {}) == {}
    assert called["hit"] is False  # no index fetch for crypto


@pytest.mark.asyncio
async def test_market_collector_no_index_fn_emits_no_indices():
    # Back-compat: without an injected source the payload is events-only.
    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query()
    )
    results = await collector.collect(_request(market="us"))
    assert "indices" not in results[0].payload_json


# ---------------------------------------------------------------------------
# News collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_news_collector_returns_citations():
    from app.schemas.research_reports import (
        ResearchReportCitation,
        ResearchReportCitationListResponse,
    )

    session = MagicMock()
    query = MagicMock()
    citation = ResearchReportCitation(
        report_uuid="44444444-1111-1111-1111-111111111111",
        title="t",
        source="kr_news",
        symbol_candidates=[],
        published_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        summary_text="s",
    )
    query.find_relevant = AsyncMock(
        return_value=ResearchReportCitationListResponse(count=1, citations=[citation])
    )
    collector = NewsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].snapshot_kind == "news"
    assert results[0].source_kind == "news_ingestor"
    assert results[0].payload_json["count"] == 1


@pytest.mark.asyncio
async def test_news_collector_failure_is_fail_open():
    session = MagicMock()
    query = MagicMock()
    query.find_relevant = AsyncMock(side_effect=RuntimeError("transient"))
    collector = NewsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"


# --- ROB-366 B8: market-scoped news articles via injected fetch fn ----------
@pytest.mark.asyncio
async def test_news_collector_articles_from_news_fetch_fn():
    captured: dict = {}

    async def fake_news_fn(market, hours, limit):
        captured["market"] = market
        return [
            {"title": "Apple beats earnings", "url": "u1", "stock_symbol": "AAPL"},
            {"title": "Fed holds rates", "url": "u2", "stock_symbol": None},
        ]

    collector = NewsSnapshotCollector(MagicMock(), news_fetch_fn=fake_news_fn)
    results = await collector.collect(_request(market="us"))
    payload = results[0].payload_json
    # The articles key (what NewsStage reads) is populated, market-scoped.
    assert payload["count"] == 2
    assert [a["title"] for a in payload["articles"]] == [
        "Apple beats earnings",
        "Fed holds rates",
    ]
    assert captured["market"] == "us"
    assert results[0].freshness_status == "fresh"


@pytest.mark.asyncio
async def test_news_collector_articles_empty_is_partial():
    async def fake_news_fn(market, hours, limit):
        return []

    collector = NewsSnapshotCollector(MagicMock(), news_fetch_fn=fake_news_fn)
    results = await collector.collect(_request(market="us"))
    assert results[0].payload_json["count"] == 0
    assert results[0].payload_json["articles"] == []
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_news_collector_articles_fetch_failure_is_fail_open():
    async def fake_news_fn(market, hours, limit):
        raise RuntimeError("news feed down")

    collector = NewsSnapshotCollector(MagicMock(), news_fetch_fn=fake_news_fn)
    results = await collector.collect(_request(market="us"))
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"


def _make_citation(*, report_uuid: str, symbols: list[str]):
    from app.schemas.research_reports import (
        ResearchReportCitation,
        ResearchReportSymbolCandidate,
    )

    return ResearchReportCitation(
        report_uuid=report_uuid,
        title=f"t-{report_uuid[:4]}",
        source="kr_news",
        symbol_candidates=[
            ResearchReportSymbolCandidate(symbol=s, market="kr") for s in symbols
        ],
        published_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        summary_text="s",
    )


@pytest.mark.asyncio
async def test_news_collector_filters_to_focus_symbols_when_supplied():
    """ROB-278 Phase 2 — request.symbols → return only citations that touch
    one of the focus symbols; record the symbol-match mapping."""
    from app.schemas.research_reports import ResearchReportCitationListResponse
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = MagicMock()
    query = MagicMock()
    citations = [
        _make_citation(
            report_uuid="11111111-1111-1111-1111-111111111111",
            symbols=["005930"],
        ),
        _make_citation(
            report_uuid="22222222-2222-2222-2222-222222222222",
            symbols=["999999"],  # not in focus
        ),
    ]
    query.find_relevant = AsyncMock(
        return_value=ResearchReportCitationListResponse(
            count=len(citations), citations=citations
        )
    )
    collector = NewsSnapshotCollector(session, query_service=query)
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930", "000660"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=42,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    # Only the 005930 citation reached the output.
    assert payload["count"] == 1
    symbols_in_first = {
        cand["symbol"] for cand in payload["citations"][0]["symbol_candidates"]
    }
    assert symbols_in_first == {"005930"}
    # Per-symbol match map preserved.
    assert payload["symbol_matches"]["005930"] == 1
    assert payload["symbol_matches"]["000660"] == 0
    assert payload.get("no_data_reason") is None


@pytest.mark.asyncio
async def test_news_collector_no_focus_matches_surfaces_no_data_reason():
    """ROB-278 Phase 2 — focus symbols supplied, but no citation touches them →
    payload carries an explicit no_data_reason and a partial freshness."""
    from app.schemas.research_reports import ResearchReportCitationListResponse
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = MagicMock()
    query = MagicMock()
    citations = [
        _make_citation(
            report_uuid="11111111-1111-1111-1111-111111111111",
            symbols=["XYZ"],
        ),
    ]
    query.find_relevant = AsyncMock(
        return_value=ResearchReportCitationListResponse(
            count=len(citations), citations=citations
        )
    )
    collector = NewsSnapshotCollector(session, query_service=query)
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=42,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload["count"] == 0
    assert payload["citations"] == []
    assert payload["no_data_reason"]
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_news_collector_no_focus_symbols_returns_general_feed():
    """ROB-278 Phase 2 — when no focus symbols, return general citations
    (legacy behaviour) but still emit the symbol_matches/no_data_reason fields."""
    from app.schemas.research_reports import ResearchReportCitationListResponse
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = MagicMock()
    query = MagicMock()
    citations = [
        _make_citation(
            report_uuid="11111111-1111-1111-1111-111111111111",
            symbols=["005930"],
        ),
    ]
    query.find_relevant = AsyncMock(
        return_value=ResearchReportCitationListResponse(
            count=len(citations), citations=citations
        )
    )
    collector = NewsSnapshotCollector(session, query_service=query)
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
        user_id=None,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    # Legacy path: all citations included.
    assert payload["count"] == 1
    assert payload["symbol_matches"] == {}
    assert payload.get("no_data_reason") is None


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collector_cls",
    [
        NaverRemoteDebugStubCollector,
        TossRemoteDebugStubCollector,
        BrowserProbeStubCollector,
    ],
)
async def test_remote_debug_stubs_return_unavailable(collector_cls: type) -> None:
    collector = collector_cls()
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"
    assert results[0].snapshot_kind == collector.snapshot_kind


# ---------------------------------------------------------------------------
# Symbol collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_symbol_collector_returns_unavailable_when_no_symbols():
    session = MagicMock()
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(_request())  # symbols=None
    assert results[0].snapshot_kind == "symbol"
    assert results[0].freshness_status == "unavailable"


@pytest.mark.asyncio
async def test_symbol_collector_returns_results_for_each_symbol():
    from app.services.investment_snapshots.collectors import CollectorRequest

    class _Row:
        def __init__(self, symbol: str, name: str) -> None:
            self.symbol = symbol
            self.name = name
            self.instrument_type = "equity_kr"
            self.exchange = "KRX"
            self.sector = "Tech"
            self.market_cap = 1_000_000.0
            self.is_active = True

    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=[_Row("005930", "삼성전자")]))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )

    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930", "000660"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)
    # Two entries: one for resolved 005930, one partial for missing 000660.
    assert len(results) == 2
    assert any(r.symbol == "005930" for r in results)
    assert any(r.freshness_status == "partial" for r in results)


@pytest.mark.asyncio
async def test_symbol_collector_query_failure_is_fail_open():
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = MagicMock()
    session.execute = AsyncMock(side_effect=RuntimeError("transient"))
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)
    assert results[0].freshness_status == "unavailable"


# ---------------------------------------------------------------------------
# Symbol collector quote/orderbook enrichment — ROB-278 Phase 2.
#
# For (market=kr, account_scope=kis_live, user_id present), the collector
# enriches each resolved symbol with read-only KIS quote/orderbook evidence
# (last price, best bid/ask, spread bps, depth). Adapter is injected so
# tests use a fake. Per-symbol fetch failures fail-open with explicit
# unavailable reasons and never crash other symbols.
# ---------------------------------------------------------------------------
def _stock_info_row(symbol: str = "005930", name: str = "삼성전자"):
    class _Row:
        def __init__(self) -> None:
            self.symbol = symbol
            self.name = name
            self.instrument_type = "equity_kr"
            self.exchange = "KRX"
            self.sector = "Tech"
            self.market_cap = 1_000_000.0
            self.is_active = True

    return _Row()


def _stock_info_session(rows: list[Any]) -> MagicMock:
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=rows))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


def _fake_quote_client_ok() -> MagicMock:
    """Fake KIS quote/orderbook client returning a full top-of-book."""

    async def fetch_quote(symbol: str) -> dict[str, Any]:
        return {
            "last_price": 70_000.0,
            "best_bid": 69_900.0,
            "best_ask": 70_100.0,
            "bid_depth": 1234.0,
            "ask_depth": 1500.0,
            "venue": "krx",
            "as_of": "2026-05-20T10:30:00+09:00",
            "session": "regular",
            "nxt_eligible": True,
        }

    client = MagicMock()
    client.fetch_quote_orderbook = AsyncMock(side_effect=fetch_quote)
    return client


@pytest.mark.asyncio
async def test_symbol_collector_enriches_with_kis_quote_when_kis_live():
    """ROB-278 Phase 2 — KR + kis_live + user_id → per-symbol quote attached."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_stock_info_row("005930", "삼성전자")])
    quote_client = _fake_quote_client_ok()
    collector = SymbolSnapshotCollector(session, kis_quote_client=quote_client)
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=42,
    )
    results = await collector.collect(req)
    # One result per symbol (no missing).
    assert len(results) == 1
    payload = results[0].payload_json
    # v1 keys preserved.
    assert payload["symbol"] == "005930"
    assert payload["name"] == "삼성전자"
    # Quote/orderbook attached.
    assert payload["quote"]["last_price"] == 70_000.0
    assert payload["quote"]["best_bid"] == 69_900.0
    assert payload["quote"]["best_ask"] == 70_100.0
    # Derived spread.
    assert payload["quote"]["spread"] == 200.0
    assert payload["quote"]["spread_bps"] == pytest.approx(28.57, rel=0.01)
    assert payload["quote"]["bid_depth"] == 1234.0
    assert payload["quote"]["ask_depth"] == 1500.0
    # Venue provenance.
    assert payload["quote"]["venue"] == "krx"
    assert payload["quote"]["nxt_eligible"] is True
    assert payload["quote"]["session"] == "regular"
    assert payload["quote"]["status"] == "ok"
    quote_client.fetch_quote_orderbook.assert_awaited_once_with("005930")


@pytest.mark.asyncio
async def test_symbol_collector_skips_quote_when_no_kis_live():
    """ROB-278 Phase 2 — non-kis_live request must not call quote client."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_stock_info_row("005930", "삼성전자")])
    quote_client = MagicMock()
    quote_client.fetch_quote_orderbook = AsyncMock(
        side_effect=AssertionError("quote client must not be called")
    )
    collector = SymbolSnapshotCollector(session, kis_quote_client=quote_client)
    req = CollectorRequest(
        market="kr",
        account_scope=None,
        symbols=["005930"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=42,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert "quote" not in payload or payload.get("quote") is None
    quote_client.fetch_quote_orderbook.assert_not_called()


@pytest.mark.asyncio
async def test_symbol_collector_skips_quote_when_no_user_id():
    """ROB-278 Phase 2 — quote enrichment requires explicit user_id (fail-closed)."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_stock_info_row("005930", "삼성전자")])
    quote_client = MagicMock()
    quote_client.fetch_quote_orderbook = AsyncMock(
        side_effect=AssertionError("must not be called without user_id")
    )
    collector = SymbolSnapshotCollector(session, kis_quote_client=quote_client)
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=None,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload.get("quote", {}).get("status") == "unavailable"
    assert "user_id" in payload["quote"]["unavailable_reason"]
    quote_client.fetch_quote_orderbook.assert_not_called()


@pytest.mark.asyncio
async def test_symbol_collector_quote_exception_marks_unavailable():
    """ROB-278 Phase 2 — KIS error on one symbol marks that symbol unavailable
    without crashing other symbols."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    rows = [
        _stock_info_row("005930", "삼성전자"),
        _stock_info_row("000660", "SK하이닉스"),
    ]
    session = _stock_info_session(rows)

    async def fetch(symbol: str):
        if symbol == "005930":
            raise RuntimeError("session closed")
        return {
            "last_price": 100_000.0,
            "best_bid": 99_900.0,
            "best_ask": 100_100.0,
            "bid_depth": 500.0,
            "ask_depth": 500.0,
            "venue": "krx",
            "as_of": "2026-05-20T10:30:00+09:00",
            "session": "regular",
            "nxt_eligible": False,
        }

    quote_client = MagicMock()
    quote_client.fetch_quote_orderbook = AsyncMock(side_effect=fetch)
    collector = SymbolSnapshotCollector(session, kis_quote_client=quote_client)
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930", "000660"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=42,
    )
    results = await collector.collect(req)
    by_symbol = {r.symbol: r for r in results if r.symbol}
    assert by_symbol["005930"].payload_json["quote"]["status"] == "unavailable"
    assert (
        "session closed"
        in by_symbol["005930"].payload_json["quote"]["unavailable_reason"]
    )
    assert by_symbol["000660"].payload_json["quote"]["status"] == "ok"
    assert by_symbol["000660"].payload_json["quote"]["last_price"] == 100_000.0


@pytest.mark.asyncio
async def test_symbol_collector_quote_empty_book_marks_no_data_reason():
    """ROB-278 Phase 2 — empty book (zero bid/ask) → unavailable with reason."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_stock_info_row("005930", "삼성전자")])

    async def fetch(symbol: str):
        return {
            "last_price": 0.0,
            "best_bid": 0.0,
            "best_ask": 0.0,
            "bid_depth": 0.0,
            "ask_depth": 0.0,
            "venue": "krx",
            "as_of": None,
            "session": "closed",
            "nxt_eligible": False,
        }

    quote_client = MagicMock()
    quote_client.fetch_quote_orderbook = AsyncMock(side_effect=fetch)
    collector = SymbolSnapshotCollector(session, kis_quote_client=quote_client)
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=42,
    )
    results = await collector.collect(req)
    quote = results[0].payload_json["quote"]
    assert quote["status"] == "unavailable"
    assert (
        "empty_book" in quote["unavailable_reason"]
        or "session" in quote["unavailable_reason"]
    )
    assert quote["session"] == "closed"


@pytest.mark.asyncio
async def test_symbol_collector_quote_respects_enrichment_cap():
    """ROB-278 Phase 2 — quote enrichment is bounded; extras get a 'capped' status."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    rows = [_stock_info_row(f"00500{i}", f"sym_{i}") for i in range(5)]
    session = _stock_info_session(rows)
    quote_client = _fake_quote_client_ok()
    collector = SymbolSnapshotCollector(
        session, kis_quote_client=quote_client, quote_enrichment_limit=3
    )
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=[f"00500{i}" for i in range(5)],
        candidate_limit=None,
        policy_snapshot={},
        user_id=42,
    )
    results = await collector.collect(req)
    enriched = [
        r
        for r in results
        if r.symbol and r.payload_json.get("quote", {}).get("status") == "ok"
    ]
    capped = [
        r
        for r in results
        if r.symbol and r.payload_json.get("quote", {}).get("status") == "skipped"
    ]
    assert len(enriched) == 3
    assert len(capped) == 2
    assert all(
        "cap" in r.payload_json["quote"]["unavailable_reason"].lower() for r in capped
    )


# ---------------------------------------------------------------------------
# Symbol collector — crypto enrichment (ROB-369 Slice 2c).
#
# Crypto symbols are NOT in stock_info; they live in upbit_symbol_universe.
# For (market=crypto, account_scope=upbit_live) the collector resolves metadata
# from that universe and enriches each symbol with read-only Upbit orderbook
# liquidity (best bid/ask, spread, depth). Public market-data → no user_id
# required (unlike KIS). Per-symbol fail-open + the shared enrichment cap.
# ---------------------------------------------------------------------------
def _upbit_universe_row(market: str = "KRW-BTC", korean_name: str = "비트코인"):
    class _Row:
        def __init__(self) -> None:
            self.market = market
            self.korean_name = korean_name
            self.english_name = "Bitcoin"
            self.base_currency = market.split("-")[-1]
            self.quote_currency = "KRW"
            self.is_active = True

    return _Row()


def _fake_upbit_quote_client_ok() -> MagicMock:
    """Fake Upbit orderbook adapter returning a full top-of-book (no last_price)."""

    async def fetch(symbol: str) -> dict[str, Any]:
        return {
            "last_price": None,
            "best_bid": 94_900_000.0,
            "best_ask": 95_100_000.0,
            "bid_depth": 0.5,
            "ask_depth": 0.3,
            "venue": "upbit",
            "session": "24h",
            "nxt_eligible": False,
            "as_of": "1716200000000",
        }

    client = MagicMock()
    client.fetch_quote_orderbook = AsyncMock(side_effect=fetch)
    return client


@pytest.mark.asyncio
async def test_symbol_collector_crypto_resolves_metadata_from_upbit_universe():
    """ROB-369 2c — crypto symbols resolve from upbit_symbol_universe (not
    stock_info); metadata is populated even when no quote adapter is wired."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_upbit_universe_row("KRW-ETH", "이더리움")])
    collector = SymbolSnapshotCollector(session)  # no upbit quote client wired
    req = CollectorRequest(
        market="crypto",
        account_scope="upbit_live",
        symbols=["KRW-ETH"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=1,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload["symbol"] == "KRW-ETH"
    assert payload["name"] == "이더리움"
    assert payload["instrument_type"] == "crypto"
    assert payload["exchange"] == "upbit"
    assert payload["is_active"] is True
    # Enrichment is wanted (crypto+upbit_live) but no client → honest unavailable.
    assert payload["quote"]["status"] == "unavailable"
    assert "no quote client" in payload["quote"]["unavailable_reason"]


@pytest.mark.asyncio
async def test_symbol_collector_crypto_enriches_with_upbit_orderbook_no_user_id():
    """ROB-369 2c — crypto+upbit_live enriches via the Upbit orderbook adapter;
    public market-data requires NO user_id (unlike KIS)."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_upbit_universe_row("KRW-BTC", "비트코인")])
    quote_client = _fake_upbit_quote_client_ok()
    collector = SymbolSnapshotCollector(session, upbit_quote_client=quote_client)
    req = CollectorRequest(
        market="crypto",
        account_scope="upbit_live",
        symbols=["KRW-BTC"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=None,  # no user_id — Upbit market-data is public
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload["symbol"] == "KRW-BTC"
    q = payload["quote"]
    assert q["status"] == "ok"
    assert q["best_bid"] == 94_900_000.0
    assert q["best_ask"] == 95_100_000.0
    assert q["spread"] == 200_000.0
    assert q["spread_bps"] == pytest.approx(21.05, rel=0.05)
    assert q["venue"] == "upbit"
    assert q["last_price"] is None  # orderbook carries no last trade — honest
    quote_client.fetch_quote_orderbook.assert_awaited_once_with("KRW-BTC")


@pytest.mark.asyncio
async def test_symbol_collector_crypto_skips_quote_when_not_upbit_live():
    """ROB-369 2c — crypto without upbit_live scope must not call the quote client."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session([_upbit_universe_row("KRW-BTC", "비트코인")])
    quote_client = MagicMock()
    quote_client.fetch_quote_orderbook = AsyncMock(
        side_effect=AssertionError("upbit quote client must not be called")
    )
    collector = SymbolSnapshotCollector(session, upbit_quote_client=quote_client)
    req = CollectorRequest(
        market="crypto",
        account_scope=None,
        symbols=["KRW-BTC"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=1,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert payload["symbol"] == "KRW-BTC"
    assert "quote" not in payload or payload.get("quote") is None
    quote_client.fetch_quote_orderbook.assert_not_called()


@pytest.mark.asyncio
async def test_symbol_collector_crypto_orderbook_failure_is_fail_open():
    """ROB-369 2c — an Upbit orderbook error marks that symbol unavailable
    without crashing others (per-symbol fail-open)."""
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = _stock_info_session(
        [
            _upbit_universe_row("KRW-BTC", "비트코인"),
            _upbit_universe_row("KRW-XRP", "리플"),
        ]
    )

    async def fetch(symbol: str):
        if symbol == "KRW-BTC":
            raise RuntimeError("upbit timeout")
        return {
            "last_price": None,
            "best_bid": 800.0,
            "best_ask": 801.0,
            "bid_depth": 100.0,
            "ask_depth": 120.0,
            "venue": "upbit",
            "session": "24h",
            "nxt_eligible": False,
            "as_of": None,
        }

    quote_client = MagicMock()
    quote_client.fetch_quote_orderbook = AsyncMock(side_effect=fetch)
    collector = SymbolSnapshotCollector(session, upbit_quote_client=quote_client)
    req = CollectorRequest(
        market="crypto",
        account_scope="upbit_live",
        symbols=["KRW-BTC", "KRW-XRP"],
        candidate_limit=None,
        policy_snapshot={},
        user_id=None,
    )
    results = await collector.collect(req)
    by_symbol = {r.symbol: r for r in results if r.symbol}
    assert by_symbol["KRW-BTC"].payload_json["quote"]["status"] == "unavailable"
    assert (
        "upbit timeout"
        in by_symbol["KRW-BTC"].payload_json["quote"]["unavailable_reason"]
    )
    assert by_symbol["KRW-XRP"].payload_json["quote"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Candidate-universe collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_candidate_universe_kr_emits_candidate_evidence():
    from types import SimpleNamespace

    from app.services.invest_screener_snapshots.repository import CoverageCounts

    repo = MagicMock()
    repo.coverage = AsyncMock(
        return_value=CoverageCounts(
            market="kr",
            today_trading_date=dt.date(2026, 5, 19),
            fresh_count=12,
            stale_count=3,
            last_computed_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        )
    )
    repo.list_top_candidates = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="005930",
                source="kis",
                change_rate=3.0,
                latest_close=78500,
                daily_volume=14_000_000,
                consecutive_up_days=3,
            )
        ]
    )
    collector = CandidateUniverseSnapshotCollector(MagicMock(), equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="kis_live"))
    payload = results[0].payload_json
    assert payload["usefulness"] == "useful"
    assert payload["fresh_count"] == 12
    assert payload["stale_count"] == 3
    assert payload["freshness_status"] == "fresh"
    assert payload["candidates"][0]["symbol"] == "005930"
    assert payload["candidates"][0]["score"] == 6.5
    assert payload["source_coverage"] == {"kis": 1}
    assert payload["missing_data"] is None


@pytest.mark.asyncio
async def test_candidate_universe_kr_stale_only_sets_missing_data():
    from types import SimpleNamespace

    from app.services.invest_screener_snapshots.repository import CoverageCounts

    repo = MagicMock()
    repo.coverage = AsyncMock(
        return_value=CoverageCounts(
            market="kr",
            today_trading_date=dt.date(2026, 5, 19),
            fresh_count=0,
            stale_count=42,
            last_computed_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        )
    )
    repo.list_top_candidates = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="000660",
                source="kis",
                change_rate=1.5,
                latest_close=120000,
                daily_volume=5_000_000,
                consecutive_up_days=1,
            )
        ]
    )
    collector = CandidateUniverseSnapshotCollector(MagicMock(), equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="kis_live"))
    payload = results[0].payload_json
    assert payload["usefulness"] == "stale_only"
    assert payload["freshness_status"] == "stale"
    assert payload["candidates"], "stale partition still yields candidate rows"
    assert payload["missing_data"]["confidence_impact"] == "cap 40"
    assert "stale" in payload["missing_data"]["what"].lower()
    # Optional kind degrades the bundle to partial, never fails it.
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_candidate_universe_kr_empty_sets_missing_data():
    from app.services.invest_screener_snapshots.repository import CoverageCounts

    repo = MagicMock()
    repo.coverage = AsyncMock(
        return_value=CoverageCounts(
            market="kr",
            today_trading_date=dt.date(2026, 5, 19),
            fresh_count=0,
            stale_count=0,
            last_computed_at=None,
        )
    )
    repo.list_top_candidates = AsyncMock(return_value=[])
    collector = CandidateUniverseSnapshotCollector(MagicMock(), equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="kis_live"))
    payload = results[0].payload_json
    assert payload["usefulness"] == "empty"
    assert payload["candidates"] == []
    assert payload["source_coverage"] == {}
    assert payload["missing_data"]["confidence_impact"] == "cap 20"
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_candidate_universe_crypto_emits_candidate_evidence():
    from types import SimpleNamespace

    from app.services.invest_crypto_screener_snapshots.repository import (
        CryptoCoverageCounts,
    )

    crypto_repo = MagicMock()
    crypto_repo.coverage = AsyncMock(
        return_value=CryptoCoverageCounts(
            latest_partition_date=dt.date(2026, 5, 19),
            latest_partition_count=7,
            stale_count=0,
            last_computed_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        )
    )
    crypto_repo.list_latest = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="KRW-BTC",
                name="비트코인",
                source="tvscreener_upbit",
                change_rate=8.0,
                latest_close=95_000_000,
                rsi=60,
                adx=30,
                trade_amount_24h=500_000_000,
                volume_24h=10,
                market_cap=None,
                market_warning=False,
            )
        ]
    )
    collector = CandidateUniverseSnapshotCollector(
        MagicMock(), crypto_repository=crypto_repo
    )
    results = await collector.collect(
        _request(market="crypto", account_scope="upbit_live")
    )
    payload = results[0].payload_json
    assert payload["usefulness"] == "useful"
    assert payload["actionable_count"] == 7
    assert payload["candidates"][0]["symbol"] == "KRW-BTC"
    assert payload["candidates"][0]["score"] == 9.0
    assert payload["source_coverage"] == {"tvscreener_upbit": 1}


@pytest.mark.asyncio
async def test_candidate_universe_failure_is_fail_open():
    session = MagicMock()
    repo = MagicMock()
    repo.coverage = AsyncMock(side_effect=RuntimeError("boom"))
    collector = CandidateUniverseSnapshotCollector(session, equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="kis_live"))
    assert results[0].freshness_status == "unavailable"


# ---------------------------------------------------------------------------
# Invest-page collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invest_page_returns_recent_published_reports():
    session = MagicMock()
    query = MagicMock()

    class _Report:
        report_uuid = "55555555-1111-1111-1111-111111111111"
        report_type = "snapshot_backed_advisory_v1"
        status = "published"
        title = "t"
        published_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)
        snapshot_bundle_uuid = "66666666-1111-1111-1111-111111111111"
        snapshot_freshness_summary = {"overall": "fresh"}

    query.list_reports = AsyncMock(return_value=[_Report()])
    collector = InvestPageSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].payload_json["count"] == 1
    assert (
        results[0].payload_json["recent_published_reports"][0][
            "snapshot_freshness_overall"
        ]
        == "fresh"
    )


@pytest.mark.asyncio
async def test_invest_page_returns_partial_when_no_recent_reports():
    session = MagicMock()
    query = MagicMock()
    query.list_reports = AsyncMock(return_value=[])
    collector = InvestPageSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_invest_page_failure_is_fail_open():
    session = MagicMock()
    query = MagicMock()
    query.list_reports = AsyncMock(side_effect=RuntimeError("transient"))
    collector = InvestPageSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_production_registry_covers_all_policy_kinds():
    from app.services.investment_snapshots.policy import INTRADAY_ACTION_REPORT_V1

    registry = production_collector_registry(session=MagicMock())
    registered = registry.list_kinds()
    policy_kinds = {k.snapshot_kind for k in INTRADAY_ACTION_REPORT_V1.kinds}

    missing = policy_kinds - registered
    assert missing == set(), f"policy kinds missing collectors: {missing}"


def test_production_registry_registers_pending_orders():
    """ROB-274 — pending_orders collector is wired into the production registry."""
    registry = production_collector_registry(session=MagicMock())
    assert "pending_orders" in registry.list_kinds()


def test_production_registry_wires_upbit_quote_client_into_symbol_collector():
    """ROB-369 2c — the symbol collector gets the Upbit orderbook adapter so
    crypto + upbit_live requests can be enriched."""
    registry = production_collector_registry(session=MagicMock())
    symbol_collector = registry.get("symbol")
    assert symbol_collector is not None
    assert symbol_collector._upbit_quote_client is not None
    assert symbol_collector._kis_quote_client is not None


@pytest.mark.asyncio
async def test_upbit_quote_adapter_maps_orderbook_top_of_book(monkeypatch):
    """ROB-369 2c — the Upbit adapter maps the orderbook top-of-book into the
    shared quote contract (last_price None — orderbook has no last trade)."""
    from app.services.action_report.snapshot_backed.collectors import (
        registry as registry_mod,
    )

    async def fake_fetch_orderbook(market: str):
        assert market == "KRW-BTC"
        return {
            "market": "KRW-BTC",
            "timestamp": 1716200000000,
            "total_ask_size": 1.0,
            "total_bid_size": 2.0,
            "orderbook_units": [
                {
                    "ask_price": 95_100_000.0,
                    "bid_price": 94_900_000.0,
                    "ask_size": 0.3,
                    "bid_size": 0.5,
                },
                {
                    "ask_price": 95_200_000.0,
                    "bid_price": 94_800_000.0,
                    "ask_size": 1.0,
                    "bid_size": 1.0,
                },
            ],
        }

    monkeypatch.setattr(
        "app.services.upbit_orderbook.fetch_orderbook", fake_fetch_orderbook
    )
    adapter = registry_mod._UpbitQuoteOrderbookAdapter()
    quote = await adapter.fetch_quote_orderbook("KRW-BTC")
    assert quote["best_bid"] == 94_900_000.0
    assert quote["best_ask"] == 95_100_000.0
    assert quote["bid_depth"] == 0.5
    assert quote["ask_depth"] == 0.3
    assert quote["last_price"] is None
    assert quote["venue"] == "upbit"
    assert quote["as_of"] == "1716200000000"


@pytest.mark.asyncio
async def test_upbit_quote_adapter_empty_orderbook_yields_none_top(monkeypatch):
    """ROB-369 2c — empty/missing orderbook → None top-of-book, which the
    collector's empty-book branch then marks unavailable."""
    from app.services.action_report.snapshot_backed.collectors import (
        registry as registry_mod,
    )

    async def fake_fetch_orderbook(market: str):
        return {}

    monkeypatch.setattr(
        "app.services.upbit_orderbook.fetch_orderbook", fake_fetch_orderbook
    )
    adapter = registry_mod._UpbitQuoteOrderbookAdapter()
    quote = await adapter.fetch_quote_orderbook("KRW-FOO")
    assert quote["best_bid"] is None
    assert quote["best_ask"] is None
    assert quote["venue"] == "upbit"


# ---------------------------------------------------------------------------
# Static-import guard — none of the collector modules pull in known
# mutation paths. If a future contributor wires the trade execution
# service, the broker SDK, or WatchActivationService into a collector
# module's import graph, this assertion fires.
# ---------------------------------------------------------------------------
def test_collector_modules_do_not_import_broker_or_activation_paths():
    import importlib
    import sys

    forbidden_substrings: tuple[str, ...] = (
        "kis_trading_service",
        "investment_reports.watch_activation",
        "alpaca_paper_ledger_service",
        "upbit.client",  # upbit broker client
        # ROB-278 — also forbid explicit broker order-mutation verbs even
        # when shipped under different paths (defence in depth).
        "place_order",
        "submit_order",
        "cancel_order",
        "modify_order",
    )
    target_modules = [
        "app.services.action_report.snapshot_backed.collectors.portfolio",
        "app.services.action_report.snapshot_backed.collectors.journal",
        "app.services.action_report.snapshot_backed.collectors.watch_context",
        "app.services.action_report.snapshot_backed.collectors.market",
        "app.services.action_report.snapshot_backed.collectors.news",
        "app.services.action_report.snapshot_backed.collectors.symbol",
        "app.services.action_report.snapshot_backed.collectors.candidate_universe",
        "app.services.action_report.snapshot_backed.collectors.invest_page",
        "app.services.action_report.snapshot_backed.collectors.optional_stubs",
        "app.services.action_report.snapshot_backed.collectors.pending_orders",
        "app.services.action_report.snapshot_backed.collectors.registry",
        "app.services.action_report.snapshot_backed.generator",
        "app.services.action_report.snapshot_backed.symbol_derivation",
        "app.services.action_report.snapshot_backed.auto_emit",
    ]

    for name in target_modules:
        importlib.import_module(name)
        module = sys.modules[name]
        source = open(module.__file__, encoding="utf-8").read()  # type: ignore[arg-type]
        for forbidden in forbidden_substrings:
            assert forbidden not in source, (
                f"{name} unexpectedly references {forbidden!r} — "
                "collectors must remain read-only"
            )
