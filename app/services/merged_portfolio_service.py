"""
Merged Portfolio Service

KIS 보유 종목과 수동 등록 종목을 통합하여 포트폴리오 제공
"""
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import BrokerType, MarketType, ManualHolding
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.broker_account_service import BrokerAccountService
from app.services.kis import KISClient

logger = logging.getLogger(__name__)


@dataclass
class HoldingInfo:
    """단일 브로커의 보유 정보"""
    broker: str
    quantity: float
    avg_price: float


@dataclass
class ReferencePrices:
    """참조 평단가 정보"""
    kis_avg: Optional[float] = None
    kis_quantity: int = 0
    toss_avg: Optional[float] = None
    toss_quantity: int = 0
    combined_avg: Optional[float] = None
    total_quantity: int = 0

    def to_dict(self) -> Dict[str, Any]:
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
    holdings: List[HoldingInfo] = field(default_factory=list)
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
    analysis_id: Optional[int] = None
    last_analysis_at: Optional[str] = None
    last_analysis_decision: Optional[str] = None
    analysis_confidence: Optional[int] = None
    # 거래 설정
    settings_quantity: Optional[float] = None
    settings_price_levels: Optional[int] = None
    settings_active: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
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
        self.broker_account_service = BrokerAccountService(db)

    @staticmethod
    def calculate_combined_avg(holdings: List[HoldingInfo]) -> float:
        """가중 평균 평단가 계산"""
        total_value = 0.0
        total_quantity = 0.0

        for h in holdings:
            total_value += h.quantity * h.avg_price
            total_quantity += h.quantity

        if total_quantity == 0:
            return 0.0

        return total_value / total_quantity

    async def get_reference_prices(
        self,
        user_id: int,
        ticker: str,
        market_type: MarketType,
        kis_holdings: Optional[Dict[str, Any]] = None,
    ) -> ReferencePrices:
        """특정 종목의 참조 평단가 정보 조회"""
        ref = ReferencePrices()
        holdings_list: List[HoldingInfo] = []

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
        kis_client: Optional[KISClient] = None,
    ) -> List[MergedHolding]:
        """국내주식 통합 포트폴리오 조회"""
        from app.services.stock_info_service import StockAnalysisService
        from app.services.symbol_trade_settings_service import SymbolTradeSettingsService

        merged: Dict[str, MergedHolding] = {}

        # 1. KIS 보유 종목 조회
        if kis_client is None:
            kis_client = KISClient()

        try:
            kis_stocks = await kis_client.fetch_my_stocks()
        except Exception as e:
            logger.error(f"Failed to fetch KIS stocks: {e}")
            kis_stocks = []

        for stock in kis_stocks:
            ticker = stock.get("pdno", "")
            name = stock.get("prdt_name", ticker)
            qty = int(stock.get("hldg_qty", 0))
            avg_price = float(stock.get("pchs_avg_pric", 0))
            current_price = float(stock.get("prpr", 0))
            evaluation = float(stock.get("evlu_amt", 0))
            profit_loss = float(stock.get("evlu_pfls_amt", 0))
            profit_rate = float(stock.get("evlu_pfls_rt", 0)) / 100.0

            if ticker not in merged:
                merged[ticker] = MergedHolding(
                    ticker=ticker,
                    name=name,
                    market_type=MarketType.KR.value,
                    current_price=current_price,
                )

            merged[ticker].kis_quantity = qty
            merged[ticker].kis_avg_price = avg_price
            merged[ticker].current_price = current_price
            merged[ticker].evaluation = evaluation
            merged[ticker].profit_loss = profit_loss
            merged[ticker].profit_rate = profit_rate
            merged[ticker].holdings.append(HoldingInfo(
                broker="kis", quantity=qty, avg_price=avg_price
            ))

        # 2. 수동 등록 보유 종목 (토스 등)
        manual_holdings = await self.manual_holdings_service.get_holdings_by_user(
            user_id, market_type=MarketType.KR
        )

        for holding in manual_holdings:
            ticker = holding.ticker
            broker_type = holding.broker_account.broker_type.value
            qty = int(holding.quantity)
            avg_price = float(holding.avg_price)
            name = holding.display_name or ticker

            if ticker not in merged:
                # KIS에 없는 종목 (토스에만 있는 경우)
                merged[ticker] = MergedHolding(
                    ticker=ticker,
                    name=name,
                    market_type=MarketType.KR.value,
                )
                # 현재가 조회 필요 시 여기에 추가

            if broker_type == "toss":
                merged[ticker].toss_quantity = qty
                merged[ticker].toss_avg_price = avg_price
            else:
                merged[ticker].other_quantity += qty
                # 다른 브로커 평단가는 가중평균으로 처리

            merged[ticker].holdings.append(HoldingInfo(
                broker=broker_type, quantity=qty, avg_price=avg_price
            ))

        # 3. 통합 계산
        for ticker, m in merged.items():
            m.total_quantity = sum(int(h.quantity) for h in m.holdings)
            m.combined_avg_price = self.calculate_combined_avg(m.holdings)

            # 통합 평단가 기준 수익률 재계산
            if m.combined_avg_price > 0 and m.current_price > 0:
                m.evaluation = m.current_price * m.total_quantity
                m.profit_loss = (m.current_price - m.combined_avg_price) * m.total_quantity
                m.profit_rate = (m.current_price - m.combined_avg_price) / m.combined_avg_price

        # 4. DB에서 분석 결과 조회
        stock_service = StockAnalysisService(self.db)
        settings_service = SymbolTradeSettingsService(self.db)

        tickers = list(merged.keys())
        analysis_map = await stock_service.get_latest_analysis_results_for_coins(tickers)

        for ticker, m in merged.items():
            analysis = analysis_map.get(ticker)
            if analysis:
                m.analysis_id = analysis.id
                m.last_analysis_at = (
                    analysis.created_at.isoformat() if analysis.created_at else None
                )
                m.last_analysis_decision = analysis.decision
                m.analysis_confidence = analysis.confidence

            # 거래 설정 조회
            settings = await settings_service.get_by_symbol(ticker)
            if settings and settings.is_active:
                m.settings_quantity = float(settings.buy_quantity_per_order)
                m.settings_price_levels = settings.buy_price_levels
                m.settings_active = settings.is_active

        return list(merged.values())

    async def get_merged_portfolio_overseas(
        self,
        user_id: int,
        kis_client: Optional[KISClient] = None,
    ) -> List[MergedHolding]:
        """해외주식 통합 포트폴리오 조회"""
        from app.services.stock_info_service import StockAnalysisService
        from app.services.symbol_trade_settings_service import SymbolTradeSettingsService

        merged: Dict[str, MergedHolding] = {}

        # 1. KIS 해외주식 보유 종목 조회
        if kis_client is None:
            kis_client = KISClient()

        try:
            kis_stocks = await kis_client.fetch_overseas_stocks()
        except Exception as e:
            logger.error(f"Failed to fetch KIS overseas stocks: {e}")
            kis_stocks = []

        for stock in kis_stocks:
            ticker = stock.get("ovrs_pdno", "")
            name = stock.get("ovrs_item_name", ticker)
            qty = int(float(stock.get("ovrs_cblc_qty", 0)))
            avg_price = float(stock.get("pchs_avg_pric", 0))
            current_price = float(stock.get("now_pric2", 0))
            evaluation = float(stock.get("ovrs_stck_evlu_amt", 0))
            profit_loss = float(stock.get("frcr_evlu_pfls_amt", 0))
            profit_rate = float(stock.get("evlu_pfls_rt", 0)) / 100.0

            if ticker not in merged:
                merged[ticker] = MergedHolding(
                    ticker=ticker,
                    name=name,
                    market_type=MarketType.US.value,
                    current_price=current_price,
                )

            merged[ticker].kis_quantity = qty
            merged[ticker].kis_avg_price = avg_price
            merged[ticker].current_price = current_price
            merged[ticker].evaluation = evaluation
            merged[ticker].profit_loss = profit_loss
            merged[ticker].profit_rate = profit_rate
            merged[ticker].holdings.append(HoldingInfo(
                broker="kis", quantity=qty, avg_price=avg_price
            ))

        # 2. 수동 등록 보유 종목 (토스 등)
        manual_holdings = await self.manual_holdings_service.get_holdings_by_user(
            user_id, market_type=MarketType.US
        )

        for holding in manual_holdings:
            ticker = holding.ticker
            broker_type = holding.broker_account.broker_type.value
            qty = int(holding.quantity)
            avg_price = float(holding.avg_price)
            name = holding.display_name or ticker

            if ticker not in merged:
                merged[ticker] = MergedHolding(
                    ticker=ticker,
                    name=name,
                    market_type=MarketType.US.value,
                )

            if broker_type == "toss":
                merged[ticker].toss_quantity = qty
                merged[ticker].toss_avg_price = avg_price
            else:
                merged[ticker].other_quantity += qty

            merged[ticker].holdings.append(HoldingInfo(
                broker=broker_type, quantity=qty, avg_price=avg_price
            ))

        # 3. 통합 계산
        for ticker, m in merged.items():
            m.total_quantity = sum(int(h.quantity) for h in m.holdings)
            m.combined_avg_price = self.calculate_combined_avg(m.holdings)

            if m.combined_avg_price > 0 and m.current_price > 0:
                m.evaluation = m.current_price * m.total_quantity
                m.profit_loss = (m.current_price - m.combined_avg_price) * m.total_quantity
                m.profit_rate = (m.current_price - m.combined_avg_price) / m.combined_avg_price

        # 4. DB에서 분석 결과 조회
        stock_service = StockAnalysisService(self.db)
        settings_service = SymbolTradeSettingsService(self.db)

        tickers = list(merged.keys())
        analysis_map = await stock_service.get_latest_analysis_results_for_coins(tickers)

        for ticker, m in merged.items():
            analysis = analysis_map.get(ticker)
            if analysis:
                m.analysis_id = analysis.id
                m.last_analysis_at = (
                    analysis.created_at.isoformat() if analysis.created_at else None
                )
                m.last_analysis_decision = analysis.decision
                m.analysis_confidence = analysis.confidence

            settings = await settings_service.get_by_symbol(ticker)
            if settings and settings.is_active:
                m.settings_quantity = float(settings.buy_quantity_per_order)
                m.settings_price_levels = settings.buy_price_levels
                m.settings_active = settings.is_active

        return list(merged.values())
