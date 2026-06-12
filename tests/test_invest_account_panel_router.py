"""Tests for GET /invest/api/account-panel."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.invest_home import (
    Account,
    CashAmounts,
    Holding,
    HomeSummary,
)
from app.services.invest_home_service import _AccountPanelView, build_grouped_holdings


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
    fake_view = _AccountPanelView(
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
        groupedHoldings=build_grouped_holdings(holdings),
        warnings=[],
    )
    home_service = MagicMock()
    home_service.build_account_panel_view = AsyncMock(return_value=fake_view)
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
    assert {"kis", "upbit", "alpaca_paper", "kis_mock", "toss_api"}.issubset(sources)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_build_account_panel_uses_slim_view_path(monkeypatch):
    from app.services.invest_view_model.account_panel_service import build_account_panel

    call_log: list[str] = []

    class _StubService:
        async def get_home(self, **kwargs):
            call_log.append("get_home")
            raise AssertionError("build_account_panel must not call get_home")

        async def build_account_panel_view(self, **kwargs):
            call_log.append("build_account_panel_view")
            from app.schemas.invest_home import HomeSummary
            from app.services.invest_home_service import _AccountPanelView

            return _AccountPanelView(
                homeSummary=HomeSummary(
                    includedSources=[],
                    excludedSources=[],
                    totalValueKrw=0,
                ),
                accounts=[],
                groupedHoldings=[],
                warnings=[],
            )

    class _DBStub:
        async def execute(self, _stmt):
            class _R:
                def all(self):
                    return []

            return _R()

    monkeypatch.setattr(
        "app.services.invest_view_model.account_panel_service._load_watch_symbols",
        AsyncMock(return_value=([], True)),
    )

    resp = await build_account_panel(
        user_id=1, db=_DBStub(), home_service=_StubService()
    )

    assert "build_account_panel_view" in call_log
    assert "get_home" not in call_log
    assert resp.homeSummary.totalValueKrw == 0
