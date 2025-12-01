"""
Trading Price Service

매수/매도 가격 전략 계산 서비스
"""
import enum
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Any

from app.services.merged_portfolio_service import ReferencePrices

logger = logging.getLogger(__name__)


class PriceStrategy(str, enum.Enum):
    """가격 전략"""
    # 공통
    current = "current"  # 현재가
    manual = "manual"    # 직접 입력

    # 매수 전략
    kis_avg = "kis_avg"              # 한투 평단가
    toss_avg = "toss_avg"            # 토스 평단가
    combined_avg = "combined_avg"    # 통합 평단가
    lowest_avg = "lowest_avg"        # 최저 평단가
    lowest_minus_percent = "lowest_minus_percent"  # 최저 평단가 -N%

    # 매도 전략
    kis_avg_plus = "kis_avg_plus"        # 한투 평단가 +N%
    toss_avg_plus = "toss_avg_plus"      # 토스 평단가 +N%
    combined_avg_plus = "combined_avg_plus"  # 통합 평단가 +N%


@dataclass
class ExpectedProfit:
    """예상 수익 정보"""
    amount: float
    percent: float

    def to_dict(self) -> Dict[str, float]:
        return {"amount": self.amount, "percent": self.percent}


@dataclass
class PriceCalculationResult:
    """가격 계산 결과"""
    price: float
    price_source: str
    reference_prices: ReferencePrices

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price": self.price,
            "price_source": self.price_source,
            "reference_prices": self.reference_prices.to_dict(),
        }


class TradingPriceService:
    """매수/매도 가격 계산 서비스"""

    @staticmethod
    def get_lowest_avg(ref: ReferencePrices) -> Optional[float]:
        """가장 낮은 평단가 반환"""
        prices = []
        if ref.kis_avg and ref.kis_avg > 0:
            prices.append(ref.kis_avg)
        if ref.toss_avg and ref.toss_avg > 0:
            prices.append(ref.toss_avg)
        # 다른 브로커 추가 시 여기에

        return min(prices) if prices else None

    def calculate_buy_price(
        self,
        reference_prices: ReferencePrices,
        current_price: float,
        strategy: PriceStrategy,
        discount_percent: float = 0.0,
        manual_price: Optional[float] = None,
    ) -> PriceCalculationResult:
        """매수 가격 계산

        Args:
            reference_prices: 참조 평단가 정보
            current_price: 현재가
            strategy: 가격 전략
            discount_percent: 할인율 (lowest_minus_percent 전략용)
            manual_price: 수동 입력 가격

        Returns:
            PriceCalculationResult: 계산된 가격 및 출처
        """
        ref = reference_prices
        price = 0.0
        source = ""

        if strategy == PriceStrategy.current:
            price = current_price
            source = "현재가"

        elif strategy == PriceStrategy.manual:
            if manual_price is None or manual_price <= 0:
                raise ValueError("수동 가격을 입력해주세요")
            price = manual_price
            source = "직접 입력"

        elif strategy == PriceStrategy.kis_avg:
            if ref.kis_avg is None or ref.kis_avg <= 0:
                raise ValueError("한투 평단가 정보가 없습니다")
            price = ref.kis_avg
            source = "한투 평단가"

        elif strategy == PriceStrategy.toss_avg:
            if ref.toss_avg is None or ref.toss_avg <= 0:
                raise ValueError("토스 평단가 정보가 없습니다")
            price = ref.toss_avg
            source = "토스 평단가"

        elif strategy == PriceStrategy.combined_avg:
            if ref.combined_avg is None or ref.combined_avg <= 0:
                raise ValueError("통합 평단가 정보가 없습니다")
            price = ref.combined_avg
            source = "통합 평단가"

        elif strategy == PriceStrategy.lowest_avg:
            lowest = self.get_lowest_avg(ref)
            if lowest is None:
                raise ValueError("평단가 정보가 없습니다")
            price = lowest
            source = "최저 평단가"

        elif strategy == PriceStrategy.lowest_minus_percent:
            lowest = self.get_lowest_avg(ref)
            if lowest is None:
                raise ValueError("평단가 정보가 없습니다")
            price = lowest * (1 - discount_percent / 100)
            source = f"최저 평단가 -{discount_percent}%"

        else:
            raise ValueError(f"지원하지 않는 매수 전략: {strategy}")

        return PriceCalculationResult(
            price=round(price, 2),
            price_source=source,
            reference_prices=ref,
        )

    def calculate_sell_price(
        self,
        reference_prices: ReferencePrices,
        current_price: float,
        strategy: PriceStrategy,
        profit_percent: float = 5.0,
        manual_price: Optional[float] = None,
    ) -> PriceCalculationResult:
        """매도 가격 계산

        Args:
            reference_prices: 참조 평단가 정보
            current_price: 현재가
            strategy: 가격 전략
            profit_percent: 목표 수익률 (avg_plus 전략용)
            manual_price: 수동 입력 가격

        Returns:
            PriceCalculationResult: 계산된 가격 및 출처
        """
        ref = reference_prices
        price = 0.0
        source = ""

        if strategy == PriceStrategy.current:
            price = current_price
            source = "현재가"

        elif strategy == PriceStrategy.manual:
            if manual_price is None or manual_price <= 0:
                raise ValueError("수동 가격을 입력해주세요")
            price = manual_price
            source = "직접 입력"

        elif strategy == PriceStrategy.kis_avg_plus:
            if ref.kis_avg is None or ref.kis_avg <= 0:
                raise ValueError("한투 평단가 정보가 없습니다")
            price = ref.kis_avg * (1 + profit_percent / 100)
            source = f"한투 평단가 +{profit_percent}%"

        elif strategy == PriceStrategy.toss_avg_plus:
            if ref.toss_avg is None or ref.toss_avg <= 0:
                raise ValueError("토스 평단가 정보가 없습니다")
            price = ref.toss_avg * (1 + profit_percent / 100)
            source = f"토스 평단가 +{profit_percent}%"

        elif strategy == PriceStrategy.combined_avg_plus:
            if ref.combined_avg is None or ref.combined_avg <= 0:
                raise ValueError("통합 평단가 정보가 없습니다")
            price = ref.combined_avg * (1 + profit_percent / 100)
            source = f"통합 평단가 +{profit_percent}%"

        else:
            raise ValueError(f"지원하지 않는 매도 전략: {strategy}")

        return PriceCalculationResult(
            price=round(price, 2),
            price_source=source,
            reference_prices=ref,
        )

    def calculate_expected_profit(
        self,
        quantity: int,
        sell_price: float,
        reference_prices: ReferencePrices,
    ) -> Dict[str, ExpectedProfit]:
        """예상 수익 계산

        Args:
            quantity: 매도 수량
            sell_price: 매도 가격
            reference_prices: 참조 평단가 정보

        Returns:
            Dict[str, ExpectedProfit]: 각 평단가 기준별 예상 수익
        """
        ref = reference_prices
        results = {}

        if ref.kis_avg and ref.kis_avg > 0:
            profit = (sell_price - ref.kis_avg) * quantity
            percent = (sell_price - ref.kis_avg) / ref.kis_avg * 100
            results["based_on_kis_avg"] = ExpectedProfit(
                amount=round(profit, 2),
                percent=round(percent, 2),
            )

        if ref.toss_avg and ref.toss_avg > 0:
            profit = (sell_price - ref.toss_avg) * quantity
            percent = (sell_price - ref.toss_avg) / ref.toss_avg * 100
            results["based_on_toss_avg"] = ExpectedProfit(
                amount=round(profit, 2),
                percent=round(percent, 2),
            )

        if ref.combined_avg and ref.combined_avg > 0:
            profit = (sell_price - ref.combined_avg) * quantity
            percent = (sell_price - ref.combined_avg) / ref.combined_avg * 100
            results["based_on_combined_avg"] = ExpectedProfit(
                amount=round(profit, 2),
                percent=round(percent, 2),
            )

        return results

    def validate_sell_quantity(
        self,
        kis_quantity: int,
        requested_quantity: int,
    ) -> Tuple[bool, Optional[str]]:
        """매도 수량 검증 (KIS 보유분 내에서만 매도 가능)

        Args:
            kis_quantity: KIS 보유 수량
            requested_quantity: 요청 매도 수량

        Returns:
            Tuple[bool, Optional[str]]: (유효 여부, 경고 메시지)
        """
        if requested_quantity <= 0:
            return False, "매도 수량은 0보다 커야 합니다"

        if kis_quantity <= 0:
            return False, "KIS 보유분이 없어 매도할 수 없습니다"

        if requested_quantity > kis_quantity:
            return False, f"KIS 보유 수량({kis_quantity}주)을 초과할 수 없습니다"

        return True, None
