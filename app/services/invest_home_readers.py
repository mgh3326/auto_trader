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
from app.services.brokers.upbit.client import (
    fetch_multiple_current_prices,
    fetch_my_coins,
)
from app.services.exchange_rate_service import get_usd_krw_rate
from app.services.invest_home_service import _SourceFetchResult
from app.services.manual_holdings_service import ManualHoldingsService

logger = logging.getLogger(__name__)


class HomeReader(Protocol):
    async def fetch(self, *, user_id: int) -> _SourceFetchResult: ...


class SafeKISClient(BaseKISClient):
    """Mutation-safe KIS client for read-only use."""

    def __init__(self) -> None:
        super().__init__()
        self.account = AccountClient(self)

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
            stocks_us = await self._client.account.fetch_my_overseas_stocks(
                exchange_code="NASD"
            )

            fx_warning: InvestHomeWarning | None = None
            usd_krw_rate: float | None = None
            try:
                usd_krw_rate = await get_usd_krw_rate()
            except Exception as exc:
                logger.warning("USD/KRW FX fetch failed: %s", exc, exc_info=True)
                fx_warning = InvestHomeWarning(
                    source="kis",
                    message="USD 보유 평가금액 환산을 위한 환율 조회에 실패했습니다.",
                )

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
                value_native = float(s.get("ovrs_stck_evlu_amt", 0))
                pnl_native = float(s.get("frcr_evlu_pfls_amt", 0))
                value_krw = (
                    value_native * usd_krw_rate if usd_krw_rate is not None else None
                )
                pnl_krw = (
                    pnl_native * usd_krw_rate if usd_krw_rate is not None else None
                )
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
                        costBasis=float(s.get("frcr_pchs_amt1", 0))
                        or (qty * avg_price),
                        currency="USD",
                        valueNative=value_native,
                        valueKrw=value_krw,
                        pnlKrw=pnl_krw,
                        pnlRate=float(s.get("evlu_pfls_rt", 0)) / 100.0,
                    )
                )

            investment_value_krw = sum(
                h.valueKrw for h in holdings if h.valueKrw is not None
            )
            account_cost_basis_krw = sum(
                h.costBasis
                for h in holdings
                if h.currency == "KRW" and h.costBasis is not None
            )
            if usd_krw_rate is not None:
                account_cost_basis_krw += sum(
                    h.costBasis * usd_krw_rate
                    for h in holdings
                    if h.currency == "USD" and h.costBasis is not None
                )

            account = Account(
                accountId="kis_account",
                displayName="KIS 실계좌",
                source="kis",
                accountKind="live",
                includedInHome=True,
                valueKrw=investment_value_krw,
                costBasisKrw=account_cost_basis_krw,
                cashBalances=CashAmounts(
                    krw=domestic_cash["balance"],
                    usd=margin.get("usd_balance"),
                ),
                buyingPower=CashAmounts(
                    krw=domestic_cash["orderable"],
                    usd=margin.get("usd_ord_psbl_amt"),
                ),
            )
            return _SourceFetchResult(
                accounts=[account], holdings=holdings, warning=fx_warning
            )

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
            crypto_rows = [c for c in coins if str(c.get("currency")) != "KRW"]
            market_codes = [f"KRW-{c.get('currency')}" for c in crypto_rows]
            price_warning: InvestHomeWarning | None = None
            current_prices: dict[str, float] = {}
            if market_codes:
                try:
                    current_prices = await fetch_multiple_current_prices(market_codes)
                except Exception as exc:
                    logger.warning("Upbit price fetch failed: %s", exc, exc_info=True)
                    price_warning = InvestHomeWarning(
                        source="upbit",
                        message="코인 평가금액 산출을 위한 현재가 조회에 실패했습니다.",
                    )
            for c in crypto_rows:
                currency = str(c.get("currency"))
                market_code = f"KRW-{currency}"
                qty = float(c.get("balance", 0)) + float(c.get("locked", 0))
                avg_price = float(c.get("avg_buy_price", 0))
                current_price = current_prices.get(market_code)
                value_krw = qty * current_price if current_price is not None else None
                cost_basis = qty * avg_price if avg_price > 0 else None
                pnl_krw = (
                    value_krw - cost_basis
                    if value_krw is not None and cost_basis is not None
                    else None
                )
                pnl_rate = (
                    pnl_krw / cost_basis
                    if pnl_krw is not None and cost_basis is not None and cost_basis > 0
                    else None
                )
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
                        averageCost=avg_price if avg_price > 0 else None,
                        costBasis=cost_basis,
                        currency="KRW",
                        valueNative=value_krw,
                        valueKrw=value_krw,
                        pnlKrw=pnl_krw,
                        pnlRate=pnl_rate,
                    )
                )

            coin_value_krw = sum(h.valueKrw for h in holdings if h.valueKrw is not None)
            coin_cost_basis_krw = (
                sum(h.costBasis for h in holdings if h.costBasis is not None)
                if holdings and all(h.costBasis is not None for h in holdings)
                else None
            )
            account = Account(
                accountId="upbit_account",
                displayName="Upbit",
                source="upbit",
                accountKind="live",
                includedInHome=True,
                valueKrw=coin_value_krw,
                costBasisKrw=coin_cost_basis_krw,
                cashBalances=CashAmounts(
                    krw=float(krw_row.get("balance", 0)) if krw_row else None
                ),
                buyingPower=CashAmounts(
                    krw=float(krw_row.get("balance", 0)) if krw_row else None
                ),
            )
            return _SourceFetchResult(
                accounts=[account], holdings=holdings, warning=price_warning
            )
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
                        costBasisKrw=None,
                        cashBalances=CashAmounts(),
                        buyingPower=CashAmounts(),
                    )

            manual_warning = (
                InvestHomeWarning(
                    source="toss_manual",
                    message="Toss 수동 보유는 현재가가 없어 평가금액에서 제외했습니다.",
                )
                if holdings
                else None
            )
            return _SourceFetchResult(
                accounts=list(accounts_map.values()),
                holdings=holdings,
                warning=manual_warning,
            )
        except Exception as exc:
            logger.warning("Manual fetch failed: %s", exc, exc_info=True)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(source="toss_manual", message=str(exc)),
            )
