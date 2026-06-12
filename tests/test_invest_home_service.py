"""ROB-123 — InvestHomeService unit tests (read-only)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.schemas.invest_home import Holding
from app.services.invest_home_service import (
    HOME_INCLUDED_SOURCES,
    build_grouped_holdings,
    build_home_summary,
    build_manual_account_from_holdings,
    classify_account_kind,
)


def _h(**kw) -> Holding:
    base = {
        "holdingId": "x",
        "accountId": "a",
        "source": "kis",
        "accountKind": "live",
        "symbol": "AAA",
        "market": "KR",
        "assetType": "equity",
        "assetCategory": "kr_stock",
        "displayName": "AAA",
        "quantity": 1.0,
        "averageCost": None,
        "costBasis": None,
        "currency": "KRW",
        "valueNative": None,
        "valueKrw": None,
        "pnlKrw": None,
        "pnlRate": None,
        "priceState": "live",
    }
    base.update(kw)
    return Holding(**base)


@pytest.mark.unit
def test_home_included_sources_is_locked() -> None:
    assert HOME_INCLUDED_SOURCES == frozenset(
        {"kis", "upbit", "toss_manual", "toss_api"}
    )


@pytest.mark.unit
def test_classify_account_kind_maps_sources() -> None:
    assert classify_account_kind("kis") == "live"
    assert classify_account_kind("upbit") == "live"
    assert classify_account_kind("toss_manual") == "manual"
    assert classify_account_kind("pension_manual") == "manual"
    assert classify_account_kind("isa_manual") == "manual"
    assert classify_account_kind("kis_mock") == "paper"
    assert classify_account_kind("kiwoom_mock") == "paper"
    assert classify_account_kind("alpaca_paper") == "paper"
    assert classify_account_kind("db_simulated") == "paper"


@pytest.mark.unit
def test_manual_holding_schema_forces_reference_only_defaults() -> None:
    holding = _h(
        source="toss_manual",
        accountKind="manual",
        quantity=7,
        sourceOfTruth=True,
        isTradeable=True,
        manualOnly=False,
        sellableQuantity=999,
        pendingSellQuantity=3,
        referenceQuantity=0,
    )

    assert holding.sourceOfTruth is False
    assert holding.isTradeable is False
    assert holding.manualOnly is True
    assert holding.sellableQuantity == 0
    assert holding.pendingSellQuantity == 0
    assert holding.referenceQuantity == 7


@pytest.mark.unit
def test_grouped_merges_same_market_assettype_currency_symbol() -> None:
    h_kis = _h(
        holdingId="1",
        source="kis",
        accountId="a1",
        symbol="005930",
        market="KR",
        currency="KRW",
        quantity=30,
        averageCost=70000,
        costBasis=2_100_000,
        valueNative=2_148_000,
        valueKrw=2_148_000,
        pnlKrw=48_000,
        pnlRate=48_000 / 2_100_000,
        sellableQuantity=25,
        pendingSellQuantity=5,
    )
    h_toss = _h(
        holdingId="2",
        source="toss_manual",
        accountId="a2",
        accountKind="manual",
        symbol="005930",
        market="KR",
        currency="KRW",
        quantity=20,
        averageCost=68_800,
        costBasis=1_376_000,
        valueNative=1_432_000,
        valueKrw=1_432_000,
        pnlKrw=56_000,
        pnlRate=56_000 / 1_376_000,
    )
    grouped = build_grouped_holdings([h_kis, h_toss])
    assert len(grouped) == 1
    g = grouped[0]
    assert g.groupId == "KR:equity:KRW:005930"
    assert g.assetCategory == "kr_stock"
    assert g.priceState == "live"
    assert g.totalQuantity == 50
    assert g.tradeableQuantity == 30
    assert g.sellableQuantity == 25
    assert g.pendingSellQuantity == 5
    assert g.referenceQuantity == 20
    assert g.costBasis == 2_100_000 + 1_376_000
    assert g.averageCost == pytest.approx((2_100_000 + 1_376_000) / 50)
    assert g.valueKrw == 2_148_000 + 1_432_000
    assert sorted(g.includedSources) == ["kis", "toss_manual"]
    assert {b.holdingId for b in g.sourceBreakdown} == {"1", "2"}

    kis = next(b for b in g.sourceBreakdown if b.source == "kis")
    assert kis.accountKind == "live"
    assert kis.sourceOfTruth is True
    assert kis.isTradeable is True
    assert kis.manualOnly is False
    assert kis.sellableQuantity == 25
    assert kis.pendingSellQuantity == 5
    assert kis.referenceQuantity == 0

    toss = next(b for b in g.sourceBreakdown if b.source == "toss_manual")
    assert toss.accountKind == "manual"
    assert toss.sourceOfTruth is False
    assert toss.isTradeable is False
    assert toss.manualOnly is True
    assert toss.sellableQuantity == 0
    assert toss.pendingSellQuantity == 0
    assert toss.referenceQuantity == 20


@pytest.mark.unit
def test_grouped_null_costbasis_propagates() -> None:
    a = _h(
        holdingId="1",
        source="kis",
        symbol="NVDA",
        market="US",
        currency="USD",
        quantity=2,
        averageCost=120,
        costBasis=240,
        valueNative=300,
        valueKrw=400_000,
    )
    b = _h(
        holdingId="2",
        source="toss_manual",
        accountKind="manual",
        symbol="NVDA",
        market="US",
        currency="USD",
        quantity=5,
        averageCost=None,
        costBasis=None,
        valueNative=750,
        valueKrw=1_000_000,
    )
    grouped = build_grouped_holdings([a, b])
    assert len(grouped) == 1
    g = grouped[0]
    assert g.totalQuantity == 7
    assert g.priceState == "live"
    assert g.costBasis is None
    assert g.averageCost is None
    assert g.pnlKrw is None
    assert g.pnlRate is None
    assert g.valueKrw == 1_400_000


@pytest.mark.unit
def test_grouped_infers_manual_value_from_live_same_symbol_price() -> None:
    kis = _h(
        holdingId="kis-kakao",
        source="kis",
        symbol="035720",
        market="KR",
        currency="KRW",
        quantity=6,
        averageCost=40_000,
        costBasis=240_000,
        valueNative=300_000,
        valueKrw=300_000,
        pnlKrw=60_000,
        pnlRate=0.25,
    )
    toss = _h(
        holdingId="toss-kakao",
        source="toss_manual",
        accountKind="manual",
        symbol="035720",
        market="KR",
        currency="KRW",
        quantity=4,
        averageCost=45_000,
        costBasis=180_000,
        valueNative=None,
        valueKrw=None,
        pnlKrw=None,
        pnlRate=None,
    )

    grouped = build_grouped_holdings([kis, toss])

    g = grouped[0]
    assert g.totalQuantity == 10
    assert g.priceState == "live"
    assert g.valueKrw == 500_000
    assert g.costBasis == 420_000
    assert g.pnlKrw == 80_000
    assert g.pnlRate == pytest.approx(80_000 / 420_000)


@pytest.mark.unit
def test_grouped_never_merges_crypto_with_equity() -> None:
    eq = _h(symbol="BTC", market="US", assetType="equity", currency="USD")
    cx = _h(
        holdingId="2",
        symbol="BTC",
        market="CRYPTO",
        assetType="crypto",
        currency="KRW",
        source="upbit",
    )
    grouped = build_grouped_holdings([eq, cx])
    ids = sorted(g.groupId for g in grouped)
    assert ids == ["CRYPTO:crypto:KRW:BTC", "US:equity:USD:BTC"]


@pytest.mark.unit
def test_grouped_never_merges_different_currency() -> None:
    a = _h(symbol="AAA", currency="KRW")
    b = _h(holdingId="2", symbol="AAA", currency="USD", market="US")
    grouped = build_grouped_holdings([a, b])
    assert len(grouped) == 2


@pytest.mark.unit
def test_grouped_uses_native_cost_basis_for_usd_rate_and_sums_krw_pnl() -> None:
    a = _h(
        symbol="AAPL",
        market="US",
        currency="USD",
        quantity=2,
        averageCost=100,
        costBasis=200,
        valueNative=220,
        valueKrw=286_000,
        pnlKrw=26_000,
    )
    grouped = build_grouped_holdings([a])
    assert len(grouped) == 1
    g = grouped[0]
    assert g.costBasis == 200
    assert g.pnlKrw == 26_000
    assert g.pnlRate == pytest.approx(0.1)


@pytest.mark.unit
def test_home_summary_uses_account_value_sum() -> None:
    from app.schemas.invest_home import Account, CashAmounts

    accounts = [
        Account(
            accountId="a1",
            displayName="KIS",
            source="kis",
            accountKind="live",
            includedInHome=True,
            valueKrw=10_000_000,
            costBasisKrw=9_000_000,
            pnlKrw=1_000_000,
            pnlRate=1 / 9,
            cashBalances=CashAmounts(),
            buyingPower=CashAmounts(),
        ),
        Account(
            accountId="a2",
            displayName="Toss",
            source="toss_manual",
            accountKind="manual",
            includedInHome=True,
            valueKrw=2_000_000,
            costBasisKrw=None,
            pnlKrw=None,
            pnlRate=None,
            cashBalances=CashAmounts(),
            buyingPower=CashAmounts(),
        ),
        Account(
            accountId="a3",
            displayName="Mock",
            source="kis_mock",
            accountKind="paper",
            includedInHome=False,
            valueKrw=999_999_999,
            costBasisKrw=None,
            pnlKrw=None,
            pnlRate=None,
            cashBalances=CashAmounts(),
            buyingPower=CashAmounts(),
        ),
    ]
    summary = build_home_summary(accounts)
    assert summary.totalValueKrw == 12_000_000
    assert summary.costBasisKrw is None  # 하나라도 null 이면 null
    assert summary.pnlKrw is None
    assert summary.pnlRate is None
    assert sorted(summary.includedSources) == ["kis", "toss_manual"]
    assert "kis_mock" in summary.excludedSources


@pytest.mark.asyncio
@pytest.mark.unit
async def test_invest_home_service_synthesizes_toss_manual_account() -> None:
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    kis_reader = AsyncMock()
    kis_reader.fetch.return_value = _SourceFetchResult(accounts=[], holdings=[])
    upbit_reader = AsyncMock()
    upbit_reader.fetch.return_value = _SourceFetchResult(accounts=[], holdings=[])

    manual_reader = AsyncMock()
    h = _h(
        holdingId="m1",
        source="toss_manual",
        accountKind="manual",
        valueKrw=100_000,
    )
    manual_reader.fetch.return_value = _SourceFetchResult(accounts=[], holdings=[h])

    service = InvestHomeService(
        kis_reader=kis_reader,
        upbit_reader=upbit_reader,
        manual_reader=manual_reader,
    )

    response = await service.get_home(user_id=1)

    toss_account = next(
        (a for a in response.accounts if a.source == "toss_manual"), None
    )
    assert toss_account is not None
    assert toss_account.displayName == "Toss 수동"
    assert toss_account.valueKrw == 100_000
    assert toss_account.costBasisKrw is None
    assert toss_account.pnlKrw is None
    assert toss_account.accountKind == "manual"


@pytest.mark.unit
def test_manual_account_calculates_krw_and_usd_cost_basis_without_nulling_summary() -> (
    None
):
    kr = _h(
        holdingId="kr",
        source="toss_manual",
        accountKind="manual",
        symbol="005930",
        market="KR",
        currency="KRW",
        quantity=10,
        costBasis=700_000,
        valueNative=720_000,
        valueKrw=720_000,
        pnlKrw=20_000,
    )
    us = _h(
        holdingId="us",
        source="toss_manual",
        accountKind="manual",
        symbol="AAPL",
        market="US",
        currency="USD",
        quantity=2,
        costBasis=200,
        valueNative=220,
        valueKrw=286_000,
        pnlKrw=26_000,
    )
    unpriced = _h(
        holdingId="missing",
        source="toss_manual",
        accountKind="manual",
        symbol="MSFT",
        market="US",
        currency="USD",
        quantity=1,
        costBasis=300,
        valueNative=None,
        valueKrw=None,
        pnlKrw=None,
        priceState="missing",
    )

    account = build_manual_account_from_holdings([kr, us, unpriced])

    assert account is not None
    assert account.valueKrw == 1_006_000
    assert account.costBasisKrw == pytest.approx(960_000)
    assert account.pnlKrw == pytest.approx(46_000)
    assert account.pnlRate == pytest.approx(46_000 / 960_000)

    summary = build_home_summary([account])
    assert summary.costBasisKrw == pytest.approx(960_000)
    assert summary.pnlKrw == pytest.approx(46_000)
    assert summary.pnlRate == pytest.approx(46_000 / 960_000)


@pytest.mark.unit
def test_home_summary_does_not_include_cash_balances_or_buying_power() -> None:
    from app.schemas.invest_home import Account, CashAmounts

    accounts = [
        Account(
            accountId="kis",
            displayName="KIS",
            source="kis",
            accountKind="live",
            includedInHome=True,
            valueKrw=720_000,
            costBasisKrw=700_000,
            cashBalances=CashAmounts(krw=100_000, usd=5),
            buyingPower=CashAmounts(krw=50_000, usd=3),
        )
    ]

    summary = build_home_summary(accounts)
    assert summary.totalValueKrw == 720_000
    assert summary.totalValueKrw != 720_000 + 100_000 + 50_000


# ---------------------------------------------------------------------------
# ROB-238: paper readers integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_paper_readers_appear_in_accounts_but_excluded_from_home_summary() -> (
    None
):
    from app.schemas.invest_home import Account, CashAmounts
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    kis_reader = AsyncMock()
    kis_reader.fetch.return_value = _SourceFetchResult(
        accounts=[
            Account(
                accountId="kis_account",
                displayName="KIS 실계좌",
                source="kis",
                accountKind="live",
                includedInHome=True,
                valueKrw=10_000_000,
                cashBalances=CashAmounts(),
                buyingPower=CashAmounts(),
            )
        ],
        holdings=[],
    )
    upbit_reader = AsyncMock()
    upbit_reader.fetch.return_value = _SourceFetchResult(accounts=[], holdings=[])
    manual_reader = AsyncMock()
    manual_reader.fetch.return_value = _SourceFetchResult(accounts=[], holdings=[])

    mock_paper_reader = AsyncMock()
    mock_paper_reader.fetch.return_value = _SourceFetchResult(
        accounts=[
            Account(
                accountId="kis_mock_account",
                displayName="KIS 모의투자",
                source="kis_mock",
                accountKind="paper",
                includedInHome=False,
                valueKrw=999_000,
                cashBalances=CashAmounts(),
                buyingPower=CashAmounts(),
            )
        ],
        holdings=[
            _h(
                holdingId="km:005930",
                accountId="kis_mock_account",
                source="kis_mock",
                accountKind="paper",
                symbol="005930",
                valueKrw=999_000,
            )
        ],
    )

    service = InvestHomeService(
        kis_reader=kis_reader,
        upbit_reader=upbit_reader,
        manual_reader=manual_reader,
        paper_readers=[mock_paper_reader],
    )
    response = await service.get_home(user_id=1, include_paper=True)

    # Both accounts are present
    sources = {a.source for a in response.accounts}
    assert "kis" in sources
    assert "kis_mock" in sources

    # Paper is excluded from home summary
    assert response.homeSummary.totalValueKrw == 10_000_000
    assert "kis_mock" not in response.homeSummary.includedSources
    assert "kis_mock" in response.homeSummary.excludedSources

    # Holdings include paper holdings
    assert any(h.source == "kis_mock" for h in response.holdings)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_paper_reader_partial_failure_does_not_break_live_accounts() -> None:
    from app.schemas.invest_home import Account, CashAmounts
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    kis_reader = AsyncMock()
    kis_reader.fetch.return_value = _SourceFetchResult(
        accounts=[
            Account(
                accountId="kis_account",
                displayName="KIS 실계좌",
                source="kis",
                accountKind="live",
                includedInHome=True,
                valueKrw=5_000_000,
                cashBalances=CashAmounts(),
                buyingPower=CashAmounts(),
            )
        ],
        holdings=[],
    )
    upbit_reader = AsyncMock()
    upbit_reader.fetch.return_value = _SourceFetchResult(accounts=[], holdings=[])
    manual_reader = AsyncMock()
    manual_reader.fetch.return_value = _SourceFetchResult(accounts=[], holdings=[])

    broken_reader = AsyncMock()
    broken_reader.source = "kis_mock"
    broken_reader.fetch.side_effect = RuntimeError("paper API unreachable")

    service = InvestHomeService(
        kis_reader=kis_reader,
        upbit_reader=upbit_reader,
        manual_reader=manual_reader,
        paper_readers=[broken_reader],
    )
    response = await service.get_home(user_id=1, include_paper=True)

    # Live KIS account still present
    assert any(a.source == "kis" for a in response.accounts)
    assert response.homeSummary.totalValueKrw == 5_000_000

    # Warning was recorded for broken paper reader
    assert len(response.meta.warnings) >= 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_service_without_paper_readers_unchanged() -> None:
    """No paper_readers param is backward-compatible."""
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    kis_reader = AsyncMock()
    kis_reader.fetch.return_value = _SourceFetchResult(accounts=[], holdings=[])
    upbit_reader = AsyncMock()
    upbit_reader.fetch.return_value = _SourceFetchResult(accounts=[], holdings=[])
    manual_reader = AsyncMock()
    manual_reader.fetch.return_value = _SourceFetchResult(accounts=[], holdings=[])

    # No paper_readers arg — backward-compatible construction
    service = InvestHomeService(
        kis_reader=kis_reader,
        upbit_reader=upbit_reader,
        manual_reader=manual_reader,
    )
    response = await service.get_home(user_id=1)
    assert response.accounts == []
    assert response.homeSummary.totalValueKrw == 0


# ---------------------------------------------------------------------------
# ROB-267: include_paper / paper_sources gating tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_home_does_not_invoke_paper_readers_when_include_paper_false():
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    class _Stub:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _SpyPaperReader:
        source = "kis_mock"
        called = False

        async def fetch(self, *, user_id):
            type(self).called = True
            return _SourceFetchResult(accounts=[], holdings=[])

    spy = _SpyPaperReader()
    service = InvestHomeService(
        kis_reader=_Stub(),
        upbit_reader=_Stub(),
        manual_reader=_Stub(),
        paper_readers=[spy],
    )

    await service.get_home(user_id=1)

    assert _SpyPaperReader.called is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_home_invokes_only_requested_paper_sources():
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    class _Stub:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _SpyKisMock:
        source = "kis_mock"
        called = False

        async def fetch(self, *, user_id):
            type(self).called = True
            return _SourceFetchResult(accounts=[], holdings=[])

    class _SpyAlpaca:
        source = "alpaca_paper"
        called = False

        async def fetch(self, *, user_id):
            type(self).called = True
            return _SourceFetchResult(accounts=[], holdings=[])

    service = InvestHomeService(
        kis_reader=_Stub(),
        upbit_reader=_Stub(),
        manual_reader=_Stub(),
        paper_readers=[_SpyKisMock(), _SpyAlpaca()],
    )

    await service.get_home(
        user_id=1, include_paper=True, paper_sources=frozenset({"kis_mock"})
    )

    assert _SpyKisMock.called is True
    assert _SpyAlpaca.called is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_home_invokes_all_paper_readers_when_sources_none():
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    class _Stub:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _SpyKisMock:
        source = "kis_mock"
        called = False

        async def fetch(self, *, user_id):
            type(self).called = True
            return _SourceFetchResult(accounts=[], holdings=[])

    class _SpyAlpaca:
        source = "alpaca_paper"
        called = False

        async def fetch(self, *, user_id):
            type(self).called = True
            return _SourceFetchResult(accounts=[], holdings=[])

    service = InvestHomeService(
        kis_reader=_Stub(),
        upbit_reader=_Stub(),
        manual_reader=_Stub(),
        paper_readers=[_SpyKisMock(), _SpyAlpaca()],
    )

    await service.get_home(user_id=1, include_paper=True, paper_sources=None)

    assert _SpyKisMock.called is True
    assert _SpyAlpaca.called is True


# ---------------------------------------------------------------------------
# ROB-267 Task 4: paper reader exception graceful fallback tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_paper_reader_exception_does_not_break_live_response():
    from app.schemas.invest_home import Account
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    class _StubLiveReader:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(
                accounts=[
                    Account(
                        accountId="kis_real",
                        displayName="KIS 실계좌",
                        source="kis",
                        accountKind="live",
                        includedInHome=True,
                        valueKrw=1_000_000,
                        costBasisKrw=900_000,
                        pnlKrw=100_000,
                        pnlRate=0.11,
                        cashBalances=Account.model_fields[
                            "cashBalances"
                        ].default_factory(),
                        buyingPower=Account.model_fields[
                            "buyingPower"
                        ].default_factory(),
                    )
                ],
                holdings=[],
            )

    class _EmptyReader:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _ExplodingPaperReader:
        source = "kis_mock"

        async def fetch(self, *, user_id):
            raise RuntimeError("paper api down")

    service = InvestHomeService(
        kis_reader=_StubLiveReader(),
        upbit_reader=_EmptyReader(),
        manual_reader=_EmptyReader(),
        paper_readers=[_ExplodingPaperReader()],
    )

    resp = await service.get_home(user_id=1, include_paper=True)

    assert len(resp.accounts) == 1
    assert resp.accounts[0].source == "kis"
    assert resp.accounts[0].valueKrw == 1_000_000
    assert any(w.source == "kis_mock" for w in resp.meta.warnings)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_paper_reader_exception_does_not_break_account_panel_view():
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    class _EmptyReader:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _ExplodingPaperReader:
        source = "alpaca_paper"

        async def fetch(self, *, user_id):
            raise RuntimeError("alpaca outage")

    service = InvestHomeService(
        kis_reader=_EmptyReader(),
        upbit_reader=_EmptyReader(),
        manual_reader=_EmptyReader(),
        paper_readers=[_ExplodingPaperReader()],
    )

    view = await service.build_account_panel_view(
        user_id=1, include_paper=True, paper_sources=frozenset({"alpaca_paper"})
    )

    assert any(w.source == "alpaca_paper" for w in view.warnings)
    # live/manual accounts list is empty in this stub but response still succeeds
    assert view.accounts == []


# ---------------------------------------------------------------------------
# ROB-267 Task 5: per-reader Sentry span tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_home_creates_reader_spans(monkeypatch):
    """Verify per-reader Sentry spans are emitted for observability."""
    import sentry_sdk

    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    spans: list[tuple[str, dict]] = []

    class _RecordingSpan:
        def __init__(self, op, name, **kwargs):
            self.op = op
            self.name = name
            self.tags = {}
            self.data = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            spans.append(
                (
                    self.name,
                    {"op": self.op, "tags": dict(self.tags), "data": dict(self.data)},
                )
            )
            return False

        def set_tag(self, k, v):
            self.tags[k] = v

        def set_data(self, k, v):
            self.data[k] = v

    def _fake_start_span(*, op=None, name=None, **_kw):
        return _RecordingSpan(op=op, name=name)

    monkeypatch.setattr(sentry_sdk, "start_span", _fake_start_span)

    class _EmptyReader:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _Paper:
        source = "kis_mock"

        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    service = InvestHomeService(
        kis_reader=_EmptyReader(),
        upbit_reader=_EmptyReader(),
        manual_reader=_EmptyReader(),
        paper_readers=[_Paper()],
    )

    await service.get_home(
        user_id=1, include_paper=True, paper_sources=frozenset({"kis_mock"})
    )

    names = [n for n, _ in spans]
    assert "invest.home.kis" in names
    assert "invest.home.upbit" in names
    assert "invest.home.manual" in names
    assert "invest.home.kis_mock" in names

    kis_mock_span = next(meta for n, meta in spans if n == "invest.home.kis_mock")
    assert kis_mock_span["tags"].get("source") == "kis_mock"
    assert kis_mock_span["tags"].get("include_paper") is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_home_default_skips_paper_spans(monkeypatch):
    import sentry_sdk

    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    spans: list[str] = []

    class _RecordingSpan:
        def __init__(self, op, name, **kwargs):
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            spans.append(self.name)
            return False

        def set_tag(self, *_):
            pass

        def set_data(self, *_):
            pass

    monkeypatch.setattr(sentry_sdk, "start_span", lambda **kw: _RecordingSpan(**kw))

    class _Stub:
        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    class _Paper:
        source = "alpaca_paper"

        async def fetch(self, *, user_id):
            return _SourceFetchResult(accounts=[], holdings=[])

    service = InvestHomeService(
        kis_reader=_Stub(),
        upbit_reader=_Stub(),
        manual_reader=_Stub(),
        paper_readers=[_Paper()],
    )
    await service.get_home(user_id=1)

    assert "invest.home.alpaca_paper" not in spans
    assert "invest.home.kis_mock" not in spans


# ---------------------------------------------------------------------------
# ROB-532 Toss API fallback & preference tests
# ---------------------------------------------------------------------------


class _Reader:
    def __init__(self, holdings=None, accounts=None, warning=None):
        self.holdings = holdings or []
        self.accounts = accounts or []
        self.warning = warning

    async def fetch(self, *, user_id):
        from app.services.invest_home_service import _SourceFetchResult

        return _SourceFetchResult(
            accounts=self.accounts,
            holdings=self.holdings,
            warning=self.warning,
        )


@pytest.mark.asyncio
async def test_get_home_uses_toss_api_instead_of_manual_when_toss_api_has_holdings():
    from app.services.invest_home_service import InvestHomeService

    toss_api_reader = _Reader(
        holdings=[
            Holding(
                holdingId="toss_api:BRK.B",
                accountId="toss_api_account",
                source="toss_api",
                accountKind="live",
                symbol="BRK.B",
                market="US",
                assetType="equity",
                assetCategory="us_stock",
                displayName="Berkshire Hathaway B",
                quantity=1.5,
                averageCost=400.0,
                costBasis=600.0,
                currency="USD",
                valueNative=645.18,
                valueKrw=None,
                sourceOfTruth=True,
                isTradeable=False,
                manualOnly=False,
                sellableQuantity=0.0,
                referenceQuantity=1.5,
            )
        ]
    )
    manual_reader = _Reader(
        holdings=[
            Holding(
                holdingId="manual:1",
                accountId="1",
                source="toss_manual",
                accountKind="manual",
                symbol="BRK.B",
                market="US",
                assetType="equity",
                assetCategory="us_stock",
                displayName="Berkshire Hathaway B",
                quantity=1.5,
                averageCost=400.0,
                costBasis=600.0,
                currency="USD",
                manualOnly=True,
            )
        ]
    )
    service = InvestHomeService(
        kis_reader=_Reader(),
        upbit_reader=_Reader(),
        manual_reader=manual_reader,
        toss_api_reader=toss_api_reader,
    )

    result = await service.get_home(user_id=1)

    assert [h.source for h in result.holdings] == ["toss_api"]
    assert result.groupedHoldings[0].tradeableQuantity == 0.0
    assert result.groupedHoldings[0].sellableQuantity == 0.0
    assert result.groupedHoldings[0].referenceQuantity == 1.5


@pytest.mark.asyncio
async def test_get_home_keeps_manual_holding_when_toss_api_does_not_duplicate_symbol():
    from app.services.invest_home_service import InvestHomeService

    toss_api_reader = _Reader(
        holdings=[
            Holding(
                holdingId="toss_api:BRK.B",
                accountId="toss_api_account",
                source="toss_api",
                accountKind="live",
                symbol="BRK.B",
                market="US",
                assetType="equity",
                assetCategory="us_stock",
                displayName="Berkshire Hathaway B",
                quantity=1.5,
                averageCost=400.0,
                costBasis=600.0,
                currency="USD",
                sourceOfTruth=True,
                isTradeable=False,
                manualOnly=False,
                sellableQuantity=0.0,
                referenceQuantity=1.5,
            )
        ]
    )
    manual_reader = _Reader(
        holdings=[
            Holding(
                holdingId="manual:1",
                accountId="1",
                source="toss_manual",
                accountKind="manual",
                symbol="AAPL",
                market="US",
                assetType="equity",
                assetCategory="us_stock",
                displayName="Apple",
                quantity=2.0,
                averageCost=100.0,
                costBasis=200.0,
                currency="USD",
                manualOnly=True,
            )
        ]
    )
    service = InvestHomeService(
        kis_reader=_Reader(),
        upbit_reader=_Reader(),
        manual_reader=manual_reader,
        toss_api_reader=toss_api_reader,
    )

    result = await service.get_home(user_id=1)

    assert [h.source for h in result.holdings] == ["toss_api", "toss_manual"]
    assert {h.symbol for h in result.holdings} == {"BRK.B", "AAPL"}


@pytest.mark.asyncio
async def test_get_home_falls_back_to_manual_when_toss_api_returns_warning_only():
    from app.schemas.invest_home import InvestHomeWarning
    from app.services.invest_home_service import InvestHomeService

    service = InvestHomeService(
        kis_reader=_Reader(),
        upbit_reader=_Reader(),
        manual_reader=_Reader(
            holdings=[
                Holding(
                    holdingId="manual:1",
                    accountId="1",
                    source="toss_manual",
                    accountKind="manual",
                    symbol="005930",
                    market="KR",
                    assetType="equity",
                    assetCategory="kr_stock",
                    displayName="삼성전자",
                    quantity=10.0,
                    averageCost=65000.0,
                    costBasis=650000.0,
                    currency="KRW",
                    valueNative=700000.0,
                    valueKrw=700000.0,
                    manualOnly=True,
                )
            ]
        ),
        toss_api_reader=_Reader(
            warning=InvestHomeWarning(source="toss_api", message="toss unavailable")
        ),
    )

    result = await service.get_home(user_id=1)

    assert [h.source for h in result.holdings] == ["toss_manual"]
    assert any(w.source == "toss_api" for w in result.meta.warnings)


@pytest.mark.asyncio
async def test_get_home_falls_back_to_manual_when_toss_api_has_cash_only_account():
    from app.schemas.invest_home import Account, CashAmounts
    from app.services.invest_home_service import InvestHomeService

    service = InvestHomeService(
        kis_reader=_Reader(),
        upbit_reader=_Reader(),
        manual_reader=_Reader(
            holdings=[
                Holding(
                    holdingId="manual:1",
                    accountId="1",
                    source="toss_manual",
                    accountKind="manual",
                    symbol="BRK.B",
                    market="US",
                    assetType="equity",
                    assetCategory="us_stock",
                    displayName="Berkshire Hathaway B",
                    quantity=1.5,
                    averageCost=400.0,
                    costBasis=600.0,
                    currency="USD",
                    manualOnly=True,
                )
            ]
        ),
        toss_api_reader=_Reader(
            accounts=[
                Account(
                    accountId="toss_api_account",
                    displayName="Toss",
                    source="toss_api",
                    accountKind="live",
                    includedInHome=True,
                    valueKrw=0.0,
                    cashBalances=CashAmounts(krw=123456.0),
                    buyingPower=CashAmounts(krw=123456.0),
                )
            ]
        ),
    )

    result = await service.get_home(user_id=1)

    assert [account.source for account in result.accounts] == [
        "toss_api",
        "toss_manual",
    ]
    assert [h.source for h in result.holdings] == ["toss_manual"]
