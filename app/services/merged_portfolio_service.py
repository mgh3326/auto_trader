"""
Merged Portfolio Service

KIS 보유 종목과 수동 등록 종목을 통합하여 포트폴리오 제공
"""
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import MarketType, ManualHolding
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.kis import KISClient

logger = logging.getLogger(__name__)

KIS_FIELD_CONFIG = {
    MarketType.KR: {
        "ticker": "pdno",
        "name": "prdt_name",
        "quantity": lambda stock: int(stock.get("hldg_qty", 0)),
        "avg_price": lambda stock: float(stock.get("pchs_avg_pric", 0)),
        "current_price": lambda stock: float(stock.get("prpr", 0)),
        "evaluation": lambda stock: float(stock.get("evlu_amt", 0)),
        "profit_loss": lambda stock: float(stock.get("evlu_pfls_amt", 0)),
        "profit_rate": lambda stock: float(stock.get("evlu_pfls_rt", 0)) / 100.0,
    },
    MarketType.US: {
        "ticker": "ovrs_pdno",
        "name": "ovrs_item_name",
        "quantity": lambda stock: int(float(stock.get("ovrs_cblc_qty", 0))),
        "avg_price": lambda stock: float(stock.get("pchs_avg_pric", 0)),
        "current_price": lambda stock: float(stock.get("now_pric2", 0)),
        "evaluation": lambda stock: float(stock.get("ovrs_stck_evlu_amt", 0)),
        "profit_loss": lambda stock: float(stock.get("frcr_evlu_pfls_amt", 0)),
        "profit_rate": lambda stock: float(stock.get("evlu_pfls_rt", 0)) / 100.0,
    },
}


@dataclass
class HoldingInfo:
    """단일 브로커의 보유 정보"""
    broker: str
    quantity: float
    avg_price: float


@dataclass
class ReferencePrices:
    """참조 평단가 정보"""
    kis_avg: float | None = None
    kis_quantity: int = 0
    toss_avg: float | None = None
    toss_quantity: int = 0
    combined_avg: float | None = None
    total_quantity: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kis_avg": self.kis_avg,
            "kis_quantity": self.kis_quantity,
            "toss_avg": self.toss_avg,
            "toss_quantity": self.toss_quantity,
            "combined_avg": self.combined_avg,
            "total_quantity": self.total_quantity,
        }


@dataclass
class MergedHolding:
    """통합 보유 종목 정보"""
    ticker: str
    name: str
    market_type: str
    holdings: list[HoldingInfo] = field(default_factory=list)
    kis_quantity: int = 0
    kis_avg_price: float = 0.0
    toss_quantity: int = 0
    toss_avg_price: float = 0.0
    other_quantity: int = 0
    other_avg_price: float = 0.0
    combined_avg_price: float = 0.0
    total_quantity: int = 0
    current_price: float = 0.0
    evaluation: float = 0.0
    profit_loss: float = 0.0
    profit_rate: float = 0.0
    # AI 분석 정보
    analysis_id: int | None = None
    last_analysis_at: str | None = None
    last_analysis_decision: str | None = None
    analysis_confidence: int | None = None
    # 거래 설정
    settings_quantity: float | None = None
    settings_price_levels: int | None = None
    settings_active: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "market_type": self.market_type,
            "holdings": [
                {"broker": h.broker, "quantity": h.quantity, "avg_price": h.avg_price}
                for h in self.holdings
            ],
            "kis_quantity": self.kis_quantity,
            "kis_avg_price": self.kis_avg_price,
            "toss_quantity": self.toss_quantity,
            "toss_avg_price": self.toss_avg_price,
            "other_quantity": self.other_quantity,
            "other_avg_price": self.other_avg_price,
            "combined_avg_price": self.combined_avg_price,
            "total_quantity": self.total_quantity,
            "current_price": self.current_price,
            "evaluation": self.evaluation,
            "profit_loss": self.profit_loss,
            "profit_rate": self.profit_rate,
            "analysis_id": self.analysis_id,
            "last_analysis_at": self.last_analysis_at,
            "last_analysis_decision": self.last_analysis_decision,
            "analysis_confidence": self.analysis_confidence,
            "settings_quantity": self.settings_quantity,
            "settings_price_levels": self.settings_price_levels,
            "settings_active": self.settings_active,
        }


class MergedPortfolioService:
    """통합 포트폴리오 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.manual_holdings_service = ManualHoldingsService(db)

    @staticmethod
    def calculate_combined_avg(holdings: list[HoldingInfo]) -> float:
        """가중 평균 평단가 계산"""
        total_value = 0.0
        total_quantity = 0.0

        for h in holdings:
            total_value += h.quantity * h.avg_price
            total_quantity += h.quantity

        if total_quantity == 0:
            return 0.0

        return total_value / total_quantity

    @staticmethod
    def _get_or_create_holding(
        merged: dict[str, MergedHolding],
        ticker: str,
        name: str,
        market_type: MarketType,
        current_price: float = 0.0,
    ) -> MergedHolding:
        if ticker not in merged:
            merged[ticker] = MergedHolding(
                ticker=ticker,
                name=name,
                market_type=market_type.value,
                current_price=current_price,
            )
        elif current_price:
            merged[ticker].current_price = current_price

        return merged[ticker]

    async def _fetch_kis_holdings(
        self, kis_client: KISClient, market_type: MarketType
    ) -> list[dict[str, Any]]:
        try:
            if market_type == MarketType.KR:
                return await kis_client.fetch_my_stocks()
            return await kis_client.fetch_overseas_stocks()
        except Exception as exc:
            logger.error(
                "Failed to fetch KIS %s stocks: %s", market_type.value, exc
            )
            return []

    def _apply_kis_holdings(
        self,
        merged: dict[str, MergedHolding],
        stocks: list[dict[str, Any]],
        market_type: MarketType,
    ) -> None:
        mapping = KIS_FIELD_CONFIG.get(market_type)
        if not mapping:
            return

        for stock in stocks:
            ticker = stock.get(mapping["ticker"], "")
            if not ticker:
                continue

            name = stock.get(mapping["name"], ticker)
            qty = mapping["quantity"](stock)
            avg_price = mapping["avg_price"](stock)
            current_price = mapping["current_price"](stock)
            evaluation = mapping["evaluation"](stock)
            profit_loss = mapping["profit_loss"](stock)
            profit_rate = mapping["profit_rate"](stock)

            holding = self._get_or_create_holding(
                merged, ticker, name, market_type, current_price
            )
            holding.kis_quantity = qty
            holding.kis_avg_price = avg_price
            holding.current_price = current_price
            holding.evaluation = evaluation
            holding.profit_loss = profit_loss
            holding.profit_rate = profit_rate
            holding.holdings.append(
                HoldingInfo(
                    broker="kis", quantity=qty, avg_price=avg_price
                )
            )

    async def _apply_manual_holdings(
        self,
        merged: dict[str, MergedHolding],
        user_id: int,
        market_type: MarketType,
    ) -> None:
        manual_holdings = await self.manual_holdings_service.get_holdings_by_user(
            user_id, market_type=market_type
        )

        for holding in manual_holdings:
            ticker = holding.ticker
            broker_type = holding.broker_account.broker_type.value
            qty = int(holding.quantity)
            avg_price = float(holding.avg_price)
            name = holding.display_name or ticker

            merged_holding = merged.get(ticker)
            if not merged_holding:
                merged_holding = self._get_or_create_holding(
                    merged, ticker, name, market_type
                )

            if broker_type == "toss":
                merged_holding.toss_quantity = qty
                merged_holding.toss_avg_price = avg_price
            else:
                merged_holding.other_quantity += qty

            merged_holding.holdings.append(
                HoldingInfo(
                    broker=broker_type, quantity=qty, avg_price=avg_price
                )
            )

    def _finalize_holdings(self, merged: dict[str, MergedHolding]) -> None:
        for holding in merged.values():
            holding.total_quantity = sum(
                int(item.quantity) for item in holding.holdings
            )
            holding.combined_avg_price = self.calculate_combined_avg(
                holding.holdings
            )

            if holding.combined_avg_price > 0 and holding.current_price > 0:
                holding.evaluation = (
                    holding.current_price * holding.total_quantity
                )
                holding.profit_loss = (
                    holding.current_price - holding.combined_avg_price
                ) * holding.total_quantity
                holding.profit_rate = (
                    holding.current_price - holding.combined_avg_price
                ) / holding.combined_avg_price

    async def _attach_analysis_and_settings(
        self, merged: dict[str, MergedHolding]
    ) -> None:
        from app.services.stock_info_service import StockAnalysisService
        from app.services.symbol_trade_settings_service import (
            SymbolTradeSettingsService,
        )

        stock_service = StockAnalysisService(self.db)
        settings_service = SymbolTradeSettingsService(self.db)

        tickers = list(merged.keys())
        analysis_map = (
            await stock_service.get_latest_analysis_results_for_coins(tickers)
        )

        for ticker, merged_holding in merged.items():
            analysis = analysis_map.get(ticker)
            if analysis:
                merged_holding.analysis_id = analysis.id
                merged_holding.last_analysis_at = (
                    analysis.created_at.isoformat()
                    if analysis.created_at
                    else None
                )
                merged_holding.last_analysis_decision = analysis.decision
                merged_holding.analysis_confidence = analysis.confidence

            settings = await settings_service.get_by_symbol(ticker)
            if settings and settings.is_active:
                merged_holding.settings_quantity = float(
                    settings.buy_quantity_per_order
                )
                merged_holding.settings_price_levels = settings.buy_price_levels
                merged_holding.settings_active = settings.is_active

    async def _build_merged_portfolio(
        self,
        user_id: int,
        market_type: MarketType,
        kis_client: KISClient,
    ) -> list[MergedHolding]:
        merged: dict[str, MergedHolding] = {}

        kis_stocks = await self._fetch_kis_holdings(kis_client, market_type)
        self._apply_kis_holdings(merged, kis_stocks, market_type)
        await self._apply_manual_holdings(merged, user_id, market_type)
        self._finalize_holdings(merged)
        await self._attach_analysis_and_settings(merged)

        return list(merged.values())

    async def get_reference_prices(
        self,
        user_id: int,
        ticker: str,
        market_type: MarketType,
        kis_holdings: dict[str, Any] | None = None,
    ) -> ReferencePrices:
        """특정 종목의 참조 평단가 정보 조회"""
        ref = ReferencePrices()
        holdings_list: list[HoldingInfo] = []

        # 1. KIS 보유 정보 (전달받았으면 사용, 아니면 API 호출)
        if kis_holdings:
            kis_qty = float(kis_holdings.get("quantity", 0))
            kis_avg = float(kis_holdings.get("avg_price", 0))
            if kis_qty > 0:
                ref.kis_quantity = int(kis_qty)
                ref.kis_avg = kis_avg
                holdings_list.append(HoldingInfo(
                    broker="kis", quantity=kis_qty, avg_price=kis_avg
                ))

        # 2. 수동 등록 보유 종목
        manual_holdings = await self.manual_holdings_service.get_holdings_by_ticker_all_accounts(
            user_id, ticker, market_type
        )

        for holding in manual_holdings:
            broker_type = holding.broker_account.broker_type.value
            qty = float(holding.quantity)
            avg = float(holding.avg_price)

            if broker_type == "toss":
                ref.toss_quantity = int(qty)
                ref.toss_avg = avg
            # 다른 브로커가 추가되면 여기에 처리

            holdings_list.append(HoldingInfo(
                broker=broker_type, quantity=qty, avg_price=avg
            ))

        # 3. 통합 평단가 계산
        if holdings_list:
            ref.combined_avg = self.calculate_combined_avg(holdings_list)
            ref.total_quantity = sum(int(h.quantity) for h in holdings_list)

        return ref

    async def get_merged_portfolio_domestic(
        self,
        user_id: int,
        kis_client: KISClient | None = None,
    ) -> list[MergedHolding]:
        """국내주식 통합 포트폴리오 조회"""
        if kis_client is None:
            kis_client = KISClient()
        return await self._build_merged_portfolio(
            user_id, MarketType.KR, kis_client
        )

    async def get_merged_portfolio_overseas(
        self,
        user_id: int,
        kis_client: KISClient | None = None,
    ) -> list[MergedHolding]:
        """해외주식 통합 포트폴리오 조회"""
        if kis_client is None:
            kis_client = KISClient()
        return await self._build_merged_portfolio(
            user_id, MarketType.US, kis_client
        )
