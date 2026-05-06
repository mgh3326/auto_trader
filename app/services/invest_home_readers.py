"""ROB-123 — read-only adapters used by InvestHomeService.

각 reader 는 한 source 의 read-only 데이터만 가져온다.
broker mutation / order / watch / scheduler / worker 경로는 import / 호출 금지.
DB write / backfill 금지 — read-only 조회만 사용.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import MarketType
from app.schemas.invest_home import (
    Account,
    CashAmounts,
    Holding,
    InvestHomeWarning,
)
from app.services.account.service import get_cash, get_positions
from app.services.invest_home_service import _SourceFetchResult
from app.services.manual_holdings_service import ManualHoldingsService

logger = logging.getLogger(__name__)


class HomeReader(Protocol):
    async def fetch(self, *, user_id: int) -> _SourceFetchResult: ...


class KISHomeReader:
    """KIS 실계좌 read-only reader. 잔고/평단/현금/매수가능만."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        try:
            # 1. Positions (Domestic & Overseas)
            pos_kr = await get_positions(market="equity_kr")
            pos_us = await get_positions(market="equity_us")
            all_pos = pos_kr + pos_us

            holdings = [
                Holding(
                    holdingId=f"kis:{p.symbol}",
                    accountId="kis_account",  # KIS integrated account
                    source="kis",
                    accountKind="live",
                    symbol=p.symbol,
                    market="KR" if p.market == "equity_kr" else "US",
                    assetType="equity",
                    displayName=p.name or p.symbol,
                    quantity=p.quantity,
                    averageCost=p.avg_price,
                    costBasis=(p.avg_price * p.quantity)
                    if p.avg_price is not None
                    else None,
                    currency="KRW" if p.market == "equity_kr" else "USD",
                    valueNative=p.evaluation_amount
                    if p.market == "equity_us"
                    else None,
                    valueKrw=p.evaluation_amount
                    if p.market == "equity_kr"
                    else None,  # US evaluation needs FX, but Position usually has it if fetched via integrated margin
                    pnlKrw=p.profit_loss,
                    pnlRate=p.profit_rate,
                )
                for p in all_pos
                if p.source == "kis"
            ]

            # 2. Cash & Buying Power
            cash_list = await get_cash()
            kis_cash = [c for c in cash_list if c.source == "kis"]

            krw_cash = next((c for c in kis_cash if c.currency == "KRW"), None)
            usd_cash = next((c for c in kis_cash if c.currency == "USD"), None)

            value_krw = sum(h.valueKrw for h in holdings if h.valueKrw is not None)
            # US holdings valueKrw calculation (simplified or using Position evaluation_amount if it's already KRW)
            # Note: app.services.account.service Position.evaluation_amount for US is usually USD.
            # We might need FX rate here, but to keep it simple and follow the "read-only" spirit,
            # we'll use evaluation_amount as is for valueNative and handle KRW conversion if possible.

            account = Account(
                accountId="kis_account",
                displayName="KIS 실계좌",
                source="kis",
                accountKind="live",
                includedInHome=True,
                valueKrw=value_krw + (krw_cash.balance if krw_cash else 0),
                costBasisKrw=sum(
                    h.costBasis for h in holdings if h.costBasis is not None
                ),
                cashBalances=CashAmounts(
                    krw=krw_cash.balance if krw_cash else None,
                    usd=usd_cash.balance if usd_cash else None,
                ),
                buyingPower=CashAmounts(
                    krw=krw_cash.orderable if krw_cash else None,
                    usd=usd_cash.orderable if usd_cash else None,
                ),
            )
            # Recalculate account pnl
            if account.costBasisKrw:
                account.pnlKrw = account.valueKrw - account.costBasisKrw
                account.pnlRate = account.pnlKrw / account.costBasisKrw

            return _SourceFetchResult(accounts=[account], holdings=holdings)
        except Exception as exc:
            logger.warning("KIS fetch failed: %s", exc)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(source="kis", message=str(exc)),
            )


class UpbitHomeReader:
    """Upbit read-only reader. 잔고/평단/원화 매수가능만."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        try:
            pos = await get_positions(market="crypto")
            cash_list = await get_cash(market="crypto")
            upbit_cash = next(
                (c for c in cash_list if c.source == "upbit" and c.currency == "KRW"),
                None,
            )

            holdings = [
                Holding(
                    holdingId=f"upbit:{p.symbol}",
                    accountId="upbit_account",
                    source="upbit",
                    accountKind="live",
                    symbol=p.symbol,
                    market="CRYPTO",
                    assetType="crypto",
                    displayName=p.name or p.symbol,
                    quantity=p.quantity,
                    averageCost=p.avg_price,
                    costBasis=(p.avg_price * p.quantity)
                    if p.avg_price is not None
                    else None,
                    currency="KRW",
                    valueNative=None,
                    valueKrw=None,  # We need current price for this
                    pnlKrw=None,
                    pnlRate=None,
                )
                for p in pos
                if p.source == "upbit"
            ]

            # Account value calculation (Upbit)
            account = Account(
                accountId="upbit_account",
                displayName="Upbit",
                source="upbit",
                accountKind="live",
                includedInHome=True,
                valueKrw=upbit_cash.balance
                if upbit_cash
                else 0,  # Should add crypto value
                cashBalances=CashAmounts(
                    krw=upbit_cash.balance if upbit_cash else None
                ),
                buyingPower=CashAmounts(
                    krw=upbit_cash.orderable if upbit_cash else None
                ),
            )

            return _SourceFetchResult(accounts=[account], holdings=holdings)
        except Exception as exc:
            logger.warning("Upbit fetch failed: %s", exc)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(source="upbit", message=str(exc)),
            )


class ManualHomeReader:
    """manual_holdings (Toss 등) read-only reader."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._service = ManualHoldingsService(db)

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        try:
            # Plan: Use ManualHoldingsService.get_holdings_by_user(user_id=...)
            # broker_account.broker_type == "toss" only.
            raw_holdings = await self._service.get_holdings_by_user(user_id)
            toss_holdings = [
                h
                for h in raw_holdings
                if str(getattr(h.broker_account, "broker_type", "")).lower() == "toss"
            ]

            holdings = [
                Holding(
                    holdingId=f"manual:{h.id}",
                    accountId=str(h.broker_account_id),
                    source="toss_manual",
                    accountKind="manual",
                    symbol=h.ticker,
                    market="KR" if h.market_type == MarketType.KR else "US",
                    assetType="equity",
                    displayName=h.display_name or h.ticker,
                    quantity=float(h.quantity),
                    averageCost=float(h.avg_price) if h.avg_price else None,
                    costBasis=(float(h.quantity) * float(h.avg_price))
                    if h.avg_price
                    else None,
                    currency="KRW" if h.market_type == MarketType.KR else "USD",
                    valueNative=None,
                    valueKrw=None,
                    pnlKrw=None,
                    pnlRate=None,
                )
                for h in toss_holdings
            ]

            # Aggregate into Account objects (per broker_account_id)
            accounts_map: dict[int, Account] = {}
            for h in toss_holdings:
                ba = h.broker_account
                if ba.id not in accounts_map:
                    accounts_map[ba.id] = Account(
                        accountId=str(ba.id),
                        displayName=ba.account_name or "Toss 수동",
                        source="toss_manual",
                        accountKind="manual",
                        includedInHome=True,
                        valueKrw=0,
                        costBasisKrw=0,
                        cashBalances=CashAmounts(),
                        buyingPower=CashAmounts(),
                    )
                acc = accounts_map[ba.id]
                # Note: Manual holdings don't have current value easily available here without FX and price fetching.
                # For MVP, we use costBasis as a placeholder or leave it 0 if unknown.
                if h.avg_price:
                    acc.costBasisKrw += float(h.quantity) * float(h.avg_price)
                    # For KR, valueKrw can be same as costBasis for placeholder
                    if h.market_type == MarketType.KR:
                        acc.valueKrw += float(h.quantity) * float(h.avg_price)

            return _SourceFetchResult(
                accounts=list(accounts_map.values()), holdings=holdings
            )
        except Exception as exc:
            logger.warning("Manual fetch failed: %s", exc)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(source="toss_manual", message=str(exc)),
            )
