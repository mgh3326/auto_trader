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
    InvestHomeHiddenCounts,
    InvestHomeWarning,
    PriceStateLiteral,
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
from app.services.invest_quote_service import InvestQuoteService
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.upbit_symbol_universe_service import (
    get_active_upbit_markets,
    get_upbit_warning_markets,
)

logger = logging.getLogger(__name__)


def _is_missing_money(value: object) -> bool:
    """Treat None/zero-like money values as unavailable for fallback checks."""

    if value is None:
        return True
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False


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
                        assetCategory="kr_stock",
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
                        assetCategory="us_stock",
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

            account_pnl_krw = investment_value_krw - account_cost_basis_krw
            account_pnl_rate = (
                account_pnl_krw / account_cost_basis_krw
                if account_cost_basis_krw > 0
                else None
            )

            usd_balance = margin.get("usd_balance")
            usd_buying_power = margin.get("usd_ord_psbl_amt")

            # Fallback for USD cash/orderable even when there are no US holdings.
            # USD cash can exist without a current overseas-stock position, so this
            # must not be gated by ``stocks_us``.
            if _is_missing_money(usd_balance) or _is_missing_money(usd_buying_power):
                try:
                    overseas_margin = (
                        await self._client.account.inquire_overseas_margin()
                    )
                    # US row: crcy_cd=USD and natn_name in {미국, US, USA}
                    us_margin = next(
                        (
                            m
                            for m in overseas_margin
                            if m.get("crcy_cd") == "USD"
                            and m.get("natn_name") in ["미국", "US", "USA"]
                        ),
                        None,
                    )
                    if us_margin:
                        usd_balance = us_margin.get("frcr_dncl_amt1")
                        usd_buying_power = us_margin.get("frcr_ord_psbl_amt1")
                except Exception as exc:
                    logger.warning("KIS overseas margin fallback failed: %s", exc)

            if _is_missing_money(usd_balance) or _is_missing_money(usd_buying_power):
                if fx_warning is None:
                    fx_warning = InvestHomeWarning(
                        source="kis",
                        message="USD 예수금/주문가능 금액을 확인할 수 없습니다.",
                    )
                else:
                    fx_warning.message += (
                        " USD 예수금/주문가능 금액을 확인할 수 없습니다."
                    )

            account = Account(
                accountId="kis_account",
                displayName="KIS 실계좌",
                source="kis",
                accountKind="live",
                includedInHome=True,
                valueKrw=investment_value_krw,
                costBasisKrw=account_cost_basis_krw,
                pnlKrw=account_pnl_krw,
                pnlRate=account_pnl_rate,
                cashBalances=CashAmounts(
                    krw=domestic_cash["balance"],
                    usd=usd_balance if usd_balance is not None else None,
                ),
                buyingPower=CashAmounts(
                    krw=domestic_cash["orderable"],
                    usd=usd_buying_power if usd_buying_power is not None else None,
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

    async def _fetch_current_prices(self, market_codes: list[str]) -> dict[str, float]:
        """Fetch Upbit prices without letting one delisted code blank the whole batch."""

        try:
            prices = await fetch_multiple_current_prices(market_codes)
        except Exception:
            prices = {}

        missing_codes = [code for code in market_codes if code not in prices]
        if not missing_codes:
            return prices

        for code in missing_codes:
            try:
                single = await fetch_multiple_current_prices([code])
            except Exception:
                continue
            if code in single:
                prices[code] = single[code]
        return prices

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        try:
            coins = await fetch_my_coins()
            krw_row = next((c for c in coins if c.get("currency") == "KRW"), None)

            crypto_rows = [
                c
                for c in coins
                if str(c.get("currency")) != "KRW"
                and (float(c.get("balance", 0) or 0) + float(c.get("locked", 0) or 0))
                > 0
            ]

            # Inactive filter
            active_markets = await get_active_upbit_markets(
                self._db, quote_currency="KRW"
            )
            caution_markets = await get_upbit_warning_markets(
                self._db, quote_currency="KRW"
            )

            tradable_rows = []
            inactive_rows = []
            for c in crypto_rows:
                market_code = f"KRW-{c.get('currency')}"
                if market_code in active_markets and market_code not in caution_markets:
                    tradable_rows.append(c)
                else:
                    inactive_rows.append(c)

            market_codes = [f"KRW-{c.get('currency')}" for c in tradable_rows]
            price_warning: InvestHomeWarning | None = None
            current_prices: dict[str, float] = {}
            if market_codes:
                try:
                    current_prices = await self._fetch_current_prices(market_codes)
                except Exception as exc:
                    logger.warning("Upbit price fetch failed: %s", exc, exc_info=True)
                    price_warning = InvestHomeWarning(
                        source="upbit",
                        message="코인 평가금액 산출을 위한 현재가 조회에 실패했습니다.",
                    )
                missing_price_codes = sorted(set(market_codes) - set(current_prices))
                if missing_price_codes and price_warning is None:
                    logger.warning(
                        "Upbit missing current prices for %s",
                        ",".join(missing_price_codes),
                    )
                    price_warning = InvestHomeWarning(
                        source="upbit",
                        message="일부 코인은 현재가가 없어 평가금액에서 제외했습니다.",
                    )

            holdings = []
            hidden_holdings = []
            hidden_counts = InvestHomeHiddenCounts()
            hidden_counts.upbitInactive = len(inactive_rows)

            # Process inactive
            for c in inactive_rows:
                currency = str(c.get("currency"))
                qty = float(c.get("balance", 0)) + float(c.get("locked", 0))
                hidden_holdings.append(
                    Holding(
                        holdingId=f"upbit:hidden:{currency}",
                        accountId="upbit_account",
                        source="upbit",
                        accountKind="live",
                        symbol=currency,
                        market="CRYPTO",
                        assetType="crypto",
                        assetCategory="crypto",
                        displayName=currency,
                        quantity=qty,
                        currency="KRW",
                        priceState="missing",
                    )
                )

            # Process tradable
            for c in tradable_rows:
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

                h = Holding(
                    holdingId=f"upbit:{currency}",
                    accountId="upbit_account",
                    source="upbit",
                    accountKind="live",
                    symbol=currency,
                    market="CRYPTO",
                    assetType="crypto",
                    assetCategory="crypto",
                    displayName=currency,
                    quantity=qty,
                    averageCost=avg_price if avg_price > 0 else None,
                    costBasis=cost_basis,
                    currency="KRW",
                    valueNative=value_krw,
                    valueKrw=value_krw,
                    pnlKrw=pnl_krw,
                    pnlRate=pnl_rate,
                    priceState="live" if current_price is not None else "missing",
                )

                if value_krw is not None and value_krw < 5000:
                    hidden_holdings.append(h)
                    hidden_counts.upbitDust += 1
                else:
                    holdings.append(h)

            priced_holdings = [h for h in holdings if h.valueKrw is not None]
            coin_value_krw = sum(
                h.valueKrw for h in priced_holdings if h.valueKrw is not None
            )
            priced_cost_vals = [h.costBasis for h in priced_holdings]
            coin_cost_basis_krw = (
                sum(v for v in priced_cost_vals if v is not None)
                if priced_cost_vals and all(v is not None for v in priced_cost_vals)
                else None
            )
            account_pnl_krw = (
                coin_value_krw - coin_cost_basis_krw
                if coin_cost_basis_krw is not None
                else None
            )
            account_pnl_rate = (
                account_pnl_krw / coin_cost_basis_krw
                if account_pnl_krw is not None and coin_cost_basis_krw > 0
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
                pnlKrw=account_pnl_krw,
                pnlRate=account_pnl_rate,
                cashBalances=CashAmounts(
                    krw=float(krw_row.get("balance", 0)) if krw_row else None
                ),
                buyingPower=CashAmounts(
                    krw=float(krw_row.get("balance", 0)) if krw_row else None
                ),
            )
            return _SourceFetchResult(
                accounts=[account],
                holdings=holdings,
                warning=price_warning,
                hidden_holdings=hidden_holdings,
                hidden_counts=hidden_counts,
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

    def __init__(
        self, db: AsyncSession, quote_service: InvestQuoteService | None = None
    ) -> None:
        self._db = db
        self._service = ManualHoldingsService(db)
        self._quote_service = quote_service

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        try:
            raw_holdings = await self._service.get_holdings_by_user(user_id)
            toss_holdings = [
                h
                for h in raw_holdings
                if str(getattr(h.broker_account, "broker_type", "")).lower() == "toss"
            ]

            kr_tickers = [
                h.ticker for h in toss_holdings if h.market_type == MarketType.KR
            ]
            us_tickers = [
                h.ticker for h in toss_holdings if h.market_type == MarketType.US
            ]

            kr_prices: dict[str, float | None] = {}
            us_prices: dict[str, float | None] = {}
            usd_krw_rate: float | None = None

            if self._quote_service:
                kr_prices = await self._quote_service.fetch_kr_prices(kr_tickers)
                us_prices = await self._quote_service.fetch_us_prices(us_tickers)
                if us_tickers:
                    try:
                        usd_krw_rate = await get_usd_krw_rate()
                    except Exception:
                        logger.warning("FX fetch failed for ManualHomeReader")

            holdings = []
            partial_valuation_failure = False

            for h in toss_holdings:
                qty = float(h.quantity)
                avg_price = float(h.avg_price) if h.avg_price else None
                cost_basis = (qty * avg_price) if avg_price else None
                market = "KR" if h.market_type == MarketType.KR else "US"
                currency = "KRW" if market == "KR" else "USD"

                price = (
                    kr_prices.get(h.ticker)
                    if market == "KR"
                    else us_prices.get(h.ticker)
                )
                price_state: PriceStateLiteral = (
                    "live" if price is not None else "missing"
                )

                value_native = qty * price if price is not None else None
                value_krw: float | None = None
                if value_native is not None:
                    if currency == "KRW":
                        value_krw = value_native
                    elif usd_krw_rate:
                        value_krw = value_native * usd_krw_rate

                if price is None and (kr_tickers or us_tickers):
                    partial_valuation_failure = True

                pnl_krw: float | None = None
                pnl_rate: float | None = None
                if value_krw is not None and cost_basis is not None:
                    # For US, cost_basis is in USD. We need cost_basis_krw for pnl_krw.
                    if currency == "KRW":
                        pnl_krw = value_krw - cost_basis
                        if cost_basis > 0:
                            pnl_rate = pnl_krw / cost_basis
                    elif usd_krw_rate:
                        cost_basis_krw = cost_basis * usd_krw_rate
                        pnl_krw = value_krw - cost_basis_krw
                        if cost_basis_krw > 0:
                            pnl_rate = pnl_krw / cost_basis_krw

                holdings.append(
                    Holding(
                        holdingId=f"manual:{h.id}",
                        accountId=str(h.broker_account_id),
                        source="toss_manual",
                        accountKind="manual",
                        symbol=h.ticker,
                        market=market,
                        assetType="equity",
                        assetCategory="kr_stock" if market == "KR" else "us_stock",
                        displayName=h.display_name or h.ticker,
                        quantity=qty,
                        averageCost=avg_price,
                        costBasis=cost_basis,
                        currency=currency,
                        valueNative=value_native,
                        valueKrw=value_krw,
                        pnlKrw=pnl_krw,
                        pnlRate=pnl_rate,
                        priceState=price_state,
                    )
                )

            manual_warning: InvestHomeWarning | None = None
            if partial_valuation_failure:
                manual_warning = InvestHomeWarning(
                    source="toss_manual",
                    message="일부 Toss 수동 보유는 현재가 조회에 실패해 평가에서 제외했습니다.",
                )
            elif not (kr_tickers or us_tickers) and holdings:
                # This case shouldn't happen with the logic above, but for safety
                manual_warning = InvestHomeWarning(
                    source="toss_manual",
                    message="Toss 수동 보유는 현재가가 없어 평가금액에서 제외했습니다.",
                )

            return _SourceFetchResult(
                accounts=[],
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
