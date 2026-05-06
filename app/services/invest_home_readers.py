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
from app.services.brokers.kis.account import (
    AccountClient,
    extract_domestic_cash_summary_from_integrated_margin,
)
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.upbit.client import fetch_my_coins
from app.services.invest_home_service import _SourceFetchResult
from app.services.manual_holdings_service import ManualHoldingsService

logger = logging.getLogger(__name__)


class HomeReader(Protocol):
    async def fetch(self, *, user_id: int) -> _SourceFetchResult: ...


class SafeKISClient(BaseKISClient):
    """Mutation-safe KIS client for read-only use."""

    def __init__(self) -> None:
        super().__init__()
        self.account = AccountClient(self)  # type: ignore

    async def _ensure_token(self) -> None:
        # Avoid module-level settings.kis_access_token side effects if possible,
        # but BaseKISClient already handles it via redis_token_manager.
        return await super()._ensure_token()


class KISHomeReader:
    """KIS 실계좌 read-only reader."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._client = SafeKISClient()

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        try:
            # 1. Domestic
            stocks_kr = await self._client.account.fetch_my_stocks(is_overseas=False)
            margin = await self._client.account.inquire_integrated_margin()
            domestic_cash = extract_domestic_cash_summary_from_integrated_margin(margin)

            # 2. Overseas (simplified to NASD for now as per account.py common usage)
            stocks_us = await self._client.account.fetch_my_overseas_stocks(exchange_code="NASD")

            holdings = []
            # Map KR
            for s in stocks_kr:
                qty = float(s.get("hldg_qty", 0))
                avg_price = float(s.get("pchs_avg_pric", 0))
                holdings.append(
                    Holding(
                        holdingId=f"kis:kr:{s.get('pdno')}",
                        accountId="kis_account",
                        source="kis",
                        accountKind="live",
                        symbol=str(s.get("pdno")),
                        market="KR",
                        assetType="equity",
                        displayName=str(s.get("prdt_name")),
                        quantity=qty,
                        averageCost=avg_price,
                        costBasis=float(s.get("pchs_amt", 0)) or (qty * avg_price),
                        currency="KRW",
                        valueNative=float(s.get("evlu_amt", 0)),
                        valueKrw=float(s.get("evlu_amt", 0)),
                        pnlKrw=float(s.get("evlu_pfls_amt", 0)),
                        pnlRate=float(s.get("evlu_pfls_rt", 0)) / 100.0,
                    )
                )
            # Map US
            for s in stocks_us:
                qty = float(s.get("ovrs_cblc_qty", 0))
                avg_price = float(s.get("pchs_avg_pric", 0))
                holdings.append(
                    Holding(
                        holdingId=f"kis:us:{s.get('ovrs_pdno')}",
                        accountId="kis_account",
                        source="kis",
                        accountKind="live",
                        symbol=str(s.get("ovrs_pdno")),
                        market="US",
                        assetType="equity",
                        displayName=str(s.get("ovrs_item_name")),
                        quantity=qty,
                        averageCost=avg_price,
                        costBasis=float(s.get("frcr_pchs_amt1", 0)) or (qty * avg_price),
                        currency="USD",
                        valueNative=float(s.get("ovrs_stck_evlu_amt", 0)),
                        valueKrw=None,  # Needs FX
                        pnlKrw=float(s.get("frcr_evlu_pfls_amt", 0)),
                        pnlRate=float(s.get("evlu_pfls_rt", 0)) / 100.0,
                    )
                )

            account = Account(
                accountId="kis_account",
                displayName="KIS 실계좌",
                source="kis",
                accountKind="live",
                includedInHome=True,
                valueKrw=sum(h.valueKrw for h in holdings if h.valueKrw) + domestic_cash["balance"],
                costBasisKrw=sum(h.costBasis for h in holdings if h.currency == "KRW"),
                cashBalances=CashAmounts(
                    krw=domestic_cash["balance"],
                    usd=margin.get("usd_balance"),
                ),
                buyingPower=CashAmounts(
                    krw=domestic_cash["orderable"],
                    usd=margin.get("usd_ord_psbl_amt"),
                ),
            )
            return _SourceFetchResult(accounts=[account], holdings=holdings)

        except Exception as exc:
            logger.warning("KIS fetch failed: %s", exc, exc_info=True)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(source="kis", message=str(exc)),
            )


class UpbitHomeReader:
    """Upbit read-only reader."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        try:
            coins = await fetch_my_coins()
            krw_row = next((c for c in coins if c.get("currency") == "KRW"), None)

            holdings = []
            for c in coins:
                currency = str(c.get("currency"))
                if currency == "KRW":
                    continue
                qty = float(c.get("balance", 0)) + float(c.get("locked", 0))
                avg_price = float(c.get("avg_buy_price", 0))
                holdings.append(
                    Holding(
                        holdingId=f"upbit:{currency}",
                        accountId="upbit_account",
                        source="upbit",
                        accountKind="live",
                        symbol=currency,
                        market="CRYPTO",
                        assetType="crypto",
                        displayName=currency,
                        quantity=qty,
                        averageCost=avg_price,
                        costBasis=qty * avg_price,
                        currency="KRW",
                        valueNative=None,
                        valueKrw=None,
                        pnlKrw=None,
                        pnlRate=None,
                    )
                )

            account = Account(
                accountId="upbit_account",
                displayName="Upbit",
                source="upbit",
                accountKind="live",
                includedInHome=True,
                valueKrw=float(krw_row.get("balance", 0)) if krw_row else 0,
                cashBalances=CashAmounts(
                    krw=float(krw_row.get("balance", 0)) if krw_row else None
                ),
                buyingPower=CashAmounts(
                    krw=float(krw_row.get("balance", 0)) if krw_row else None
                ),
            )
            return _SourceFetchResult(accounts=[account], holdings=holdings)
        except Exception as exc:
            logger.warning("Upbit fetch failed: %s", exc, exc_info=True)
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
            raw_holdings = await self._service.get_holdings_by_user(user_id)
            toss_holdings = [
                h for h in raw_holdings
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
                    costBasis=(float(h.quantity) * float(h.avg_price)) if h.avg_price else None,
                    currency="KRW" if h.market_type == MarketType.KR else "USD",
                    valueNative=None,
                    valueKrw=None,
                    pnlKrw=None,
                    pnlRate=None,
                )
                for h in toss_holdings
            ]

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
                if h.avg_price:
                    acc.costBasisKrw += float(h.quantity) * float(h.avg_price)
                    if h.market_type == MarketType.KR:
                        acc.valueKrw += float(h.quantity) * float(h.avg_price)

            return _SourceFetchResult(accounts=list(accounts_map.values()), holdings=holdings)
        except Exception as exc:
            logger.warning("Manual fetch failed: %s", exc, exc_info=True)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(source="toss_manual", message=str(exc)),
            )
