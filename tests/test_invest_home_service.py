"""ROB-123 — InvestHomeService unit tests (read-only)."""

from __future__ import annotations

import pytest

from app.schemas.invest_home import Holding
from app.services.invest_home_service import (
    HOME_INCLUDED_SOURCES,
    build_grouped_holdings,
    build_home_summary,
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
        "displayName": "AAA",
        "quantity": 1.0,
        "averageCost": None,
        "costBasis": None,
        "currency": "KRW",
        "valueNative": None,
        "valueKrw": None,
        "pnlKrw": None,
        "pnlRate": None,
    }
    base.update(kw)
    return Holding(**base)


@pytest.mark.unit
def test_home_included_sources_is_locked() -> None:
    assert HOME_INCLUDED_SOURCES == frozenset({"kis", "upbit", "toss_manual"})


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
    assert g.totalQuantity == 50
    assert g.costBasis == 2_100_000 + 1_376_000
    assert g.averageCost == pytest.approx((2_100_000 + 1_376_000) / 50)
    assert g.valueKrw == 2_148_000 + 1_432_000
    assert sorted(g.includedSources) == ["kis", "toss_manual"]
    assert {b.holdingId for b in g.sourceBreakdown} == {"1", "2"}


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
    assert g.costBasis is None
    assert g.averageCost is None
    assert g.pnlKrw is None
    assert g.pnlRate is None
    assert g.valueKrw == 1_400_000


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
