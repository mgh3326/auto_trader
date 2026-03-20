"""
Toss Manual Trading Notification Service

토스 보유 종목에 대한 AI 분석 결과를 텔레그램으로 알림
"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import MarketType
from app.monitoring.trade_notifier import get_trade_notifier
from app.services.merged_portfolio_service import (
    MergedPortfolioService,
    ReferencePrices,
)

logger = logging.getLogger(__name__)


@dataclass
class TossNotificationData:
    """토스 알림 데이터"""

    ticker: str
    name: str
    current_price: float
    toss_quantity: int
    toss_avg_price: float
    kis_quantity: int | None = None
    kis_avg_price: float | None = None
    recommended_price: float = 0.0
    recommended_quantity: int = 1
    expected_profit: float = 0.0
    profit_percent: float = 0.0
    currency: str = "원"
    market_type: str = "국내주식"


class TossNotificationService:
    """토스 보유 종목 알림 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.portfolio_service = MergedPortfolioService(db)

    async def should_notify_toss(
        self,
        user_id: int,
        ticker: str,
        market_type: MarketType,
        kis_holdings: dict | None = None,
    ) -> tuple[bool, ReferencePrices | None]:
        """토스 알림을 보내야 하는지 확인

        Args:
            user_id: 사용자 ID
            ticker: 종목 코드
            market_type: 시장 유형
            kis_holdings: KIS 보유 정보 (있으면 전달)

        Returns:
            tuple[bool, ReferencePrices | None]: (알림 여부, 참조 가격 정보)
        """
        try:
            ref = await self.portfolio_service.get_reference_prices(
                user_id, ticker, market_type, kis_holdings
            )

            # 토스 보유분이 있으면 알림 대상
            if ref.toss_quantity > 0:
                return True, ref

            return False, None

        except Exception as e:
            logger.error(f"Failed to check Toss holdings for {ticker}: {e}")
            return False, None

    async def notify_buy_recommendation(
        self,
        data: TossNotificationData,
    ) -> bool:
        """토스 매수 추천 알림 발송

        Args:
            data: 알림 데이터

        Returns:
            bool: 성공 여부
        """
        if data.toss_quantity <= 0:
            logger.debug(
                f"Skipping Toss buy notification for {data.ticker}: no Toss holdings"
            )
            return False

        try:
            notifier = get_trade_notifier()
            return await notifier.notify_toss_buy_recommendation(
                symbol=data.ticker,
                korean_name=data.name,
                current_price=data.current_price,
                toss_quantity=data.toss_quantity,
                toss_avg_price=data.toss_avg_price,
                kis_quantity=data.kis_quantity,
                kis_avg_price=data.kis_avg_price,
                recommended_price=data.recommended_price,
                recommended_quantity=data.recommended_quantity,
                currency=data.currency,
                market_type=data.market_type,
            )
        except Exception as e:
            logger.error(f"Failed to send Toss buy notification: {e}")
            return False

    async def notify_sell_recommendation(
        self,
        data: TossNotificationData,
    ) -> bool:
        """토스 매도 추천 알림 발송

        Args:
            data: 알림 데이터

        Returns:
            bool: 성공 여부
        """
        if data.toss_quantity <= 0:
            logger.debug(
                f"Skipping Toss sell notification for {data.ticker}: no Toss holdings"
            )
            return False

        try:
            notifier = get_trade_notifier()
            return await notifier.notify_toss_sell_recommendation(
                symbol=data.ticker,
                korean_name=data.name,
                current_price=data.current_price,
                toss_quantity=data.toss_quantity,
                toss_avg_price=data.toss_avg_price,
                kis_quantity=data.kis_quantity,
                kis_avg_price=data.kis_avg_price,
                recommended_price=data.recommended_price,
                recommended_quantity=data.recommended_quantity,
                expected_profit=data.expected_profit,
                profit_percent=data.profit_percent,
                currency=data.currency,
                market_type=data.market_type,
            )
        except Exception as e:
            logger.error(f"Failed to send Toss sell notification: {e}")
            return False

    async def process_analysis_result(
        self,
        user_id: int,
        ticker: str,
        name: str,
        market_type: MarketType,
        decision: str,
        current_price: float,
        recommended_buy_price: float | None = None,
        recommended_sell_price: float | None = None,
        recommended_quantity: int = 1,
        kis_holdings: dict | None = None,
    ) -> bool:
        """AI 분석 결과를 처리하여 토스 알림 발송

        Args:
            user_id: 사용자 ID
            ticker: 종목 코드
            name: 종목명
            market_type: 시장 유형
            decision: AI 결정 (buy/sell/hold)
            current_price: 현재가
            recommended_buy_price: 추천 매수가
            recommended_sell_price: 추천 매도가
            recommended_quantity: 추천 수량
            kis_holdings: KIS 보유 정보

        Returns:
            bool: 알림 발송 여부
        """
        # hold면 알림 안 함
        if decision.lower() == "hold":
            return False

        # 토스 보유 확인
        should_notify, ref = await self.should_notify_toss(
            user_id, ticker, market_type, kis_holdings
        )

        if not should_notify or not ref:
            return False

        # 통화 및 시장 유형 설정
        currency = "$" if market_type == MarketType.US else "원"
        market_type_str = "해외주식" if market_type == MarketType.US else "국내주식"

        data = TossNotificationData(
            ticker=ticker,
            name=name,
            current_price=current_price,
            toss_quantity=ref.toss_quantity,
            toss_avg_price=ref.toss_avg or 0,
            kis_quantity=ref.kis_quantity if ref.kis_quantity > 0 else None,
            kis_avg_price=ref.kis_avg if ref.kis_avg else None,
            recommended_quantity=recommended_quantity,
            currency=currency,
            market_type=market_type_str,
        )

        if decision.lower() == "buy" and recommended_buy_price:
            data.recommended_price = recommended_buy_price
            return await self.notify_buy_recommendation(data)

        elif decision.lower() == "sell" and recommended_sell_price:
            data.recommended_price = recommended_sell_price

            # 예상 수익 계산 (토스 평단가 기준)
            if ref.toss_avg and ref.toss_avg > 0:
                data.profit_percent = (
                    (recommended_sell_price - ref.toss_avg) / ref.toss_avg * 100
                )
                data.expected_profit = (
                    recommended_sell_price - ref.toss_avg
                ) * recommended_quantity

            return await self.notify_sell_recommendation(data)

        return False


async def send_toss_notification_if_needed(
    db: AsyncSession,
    user_id: int,
    ticker: str,
    name: str,
    market_type: MarketType,
    decision: str,
    current_price: float,
    recommended_buy_price: float | None = None,
    recommended_sell_price: float | None = None,
    recommended_quantity: int = 1,
    kis_holdings: dict | None = None,
) -> bool:
    """토스 알림 발송 헬퍼 함수

    AI 분석 후 호출하여 토스 보유 종목이면 알림 발송

    Returns:
        bool: 알림 발송 여부
    """
    service = TossNotificationService(db)
    return await service.process_analysis_result(
        user_id=user_id,
        ticker=ticker,
        name=name,
        market_type=market_type,
        decision=decision,
        current_price=current_price,
        recommended_buy_price=recommended_buy_price,
        recommended_sell_price=recommended_sell_price,
        recommended_quantity=recommended_quantity,
        kis_holdings=kis_holdings,
    )
