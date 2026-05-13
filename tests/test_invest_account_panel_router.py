"""Tests for GET /invest/api/account-panel."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.invest_home import (
    Account,
    CashAmounts,
    Holding,
    HomeSummary,
    InvestHomeResponse,
    InvestHomeResponseMeta,
)
from app.services.invest_home_service import build_grouped_holdings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_account_panel_combines_home_and_watch(monkeypatch) -> None:
    from app.services.invest_view_model.account_panel_service import build_account_panel

    holdings = [
        Holding(
            holdingId="kis:AAPL",
            accountId="a1",
            source="kis",
            accountKind="live",
            symbol="AAPL",
            market="US",
            assetType="equity",
            assetCategory="us_stock",
            displayName="Apple",
            quantity=3,
            currency="USD",
            sellableQuantity=2,
            pendingSellQuantity=1,
        ),
        Holding(
            holdingId="toss:AAPL",
            accountId="a2",
            source="toss_manual",
            accountKind="manual",
            symbol="AAPL",
            market="US",
            assetType="equity",
            assetCategory="us_stock",
            displayName="Apple",
            quantity=5,
            currency="USD",
        ),
    ]
    fake_home = InvestHomeResponse(
        homeSummary=HomeSummary(
            includedSources=["kis"], excludedSources=[], totalValueKrw=0.0
        ),
        accounts=[
            Account(
                accountId="a1",
                displayName="K",
                source="kis",
                accountKind="live",
                includedInHome=True,
                valueKrw=0.0,
                cashBalances=CashAmounts(),
                buyingPower=CashAmounts(),
            )
        ],
        holdings=holdings,
        groupedHoldings=build_grouped_holdings(holdings),
        meta=InvestHomeResponseMeta(),
    )
    home_service = MagicMock()
    home_service.get_home = AsyncMock(return_value=fake_home)
    db = MagicMock()
    monkeypatch.setattr(
        "app.services.invest_view_model.account_panel_service._load_watch_symbols",
        AsyncMock(return_value=([], True)),
    )

    resp = await build_account_panel(user_id=1, db=db, home_service=home_service)
    assert resp.homeSummary.includedSources == ["kis"]
    assert len(resp.accounts) == 1
    assert resp.watchSymbols == []
    assert resp.meta.watchlistAvailable is True
    grouped = resp.groupedHoldings[0]
    assert grouped.totalQuantity == 8
    assert grouped.tradeableQuantity == 3
    assert grouped.sellableQuantity == 2
    assert grouped.pendingSellQuantity == 1
    assert grouped.referenceQuantity == 5
    manual = next(b for b in grouped.sourceBreakdown if b.source == "toss_manual")
    assert manual.manualOnly is True
    assert manual.isTradeable is False
    assert manual.sellableQuantity == 0
    assert manual.referenceQuantity == 5
    # All known sources represented in sourceVisuals
    sources = {v.source for v in resp.sourceVisuals}
    assert {"kis", "upbit", "alpaca_paper", "kis_mock"}.issubset(sources)
