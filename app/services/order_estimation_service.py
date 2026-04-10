"""Order Estimation Service — 주문 비용 추정 공통 로직"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_BUY_PRICE_FIELDS = [
    "appropriate_buy_min",
    "appropriate_buy_max",
    "buy_hope_min",
    "buy_hope_max",
]


def extract_buy_prices_from_analysis(analysis: Any) -> list[dict[str, Any]]:
    """분석 결과에서 매수 가격 목록 추출

    Args:
        analysis: StockAnalysisResult 객체 (appropriate_buy_min/max, buy_hope_min/max 속성)

    Returns:
        [{"price_name": "appropriate_buy_min", "price": 50000.0}, ...]
    """
    buy_prices: list[dict[str, Any]] = []
    for field in _BUY_PRICE_FIELDS:
        value = getattr(analysis, field, None)
        if value is not None:
            buy_prices.append({"price_name": field, "price": float(value)})
    return buy_prices


def calculate_estimated_order_cost(
    symbol: str,
    buy_prices: list[dict[str, float]],
    quantity_per_order: float,
    currency: str = "KRW",
    *,
    amount_based: bool = False,
) -> dict[str, Any]:
    """예상 주문 비용 계산

    Args:
        symbol: 종목 코드
        buy_prices: 매수 가격 목록 [{"price_name": "...", "price": 50000}, ...]
        quantity_per_order: 주문당 수량 (amount_based=True일 때는 주문당 금액)
        currency: 통화 (KRW, USD)
        amount_based: True이면 금액 기반 계산 (암호화폐용).
            각 가격대마다 동일 금액(quantity_per_order)을 매수하고,
            수량은 금액/가격으로 역산.

    Returns:
        {
            "symbol": "005930",
            "quantity_per_order": 2,
            "buy_prices": [{"price_name": ..., "price": ..., "quantity": ..., "cost": ...}],
            "total_orders": 2,
            "total_quantity": 4,
            "total_cost": 196000,
            "currency": "KRW"
        }
    """
    result_prices = []
    total_quantity = 0.0
    total_cost = 0.0

    for price_info in buy_prices:
        price = price_info["price"]
        price_name = price_info["price_name"]

        if amount_based:
            qty = quantity_per_order / price if price > 0 else 0
            cost = quantity_per_order
        elif currency == "KRW":
            qty = int(quantity_per_order)
            cost = price * qty
        else:
            qty = quantity_per_order
            cost = price * qty

        result_prices.append(
            {
                "price_name": price_name,
                "price": price,
                "quantity": qty,
                "cost": cost,
            }
        )

        total_quantity += qty
        total_cost += cost

    return {
        "symbol": symbol,
        "quantity_per_order": quantity_per_order,
        "buy_prices": result_prices,
        "total_orders": len(buy_prices),
        "total_quantity": total_quantity,
        "total_cost": total_cost,
        "currency": currency,
    }


async def fetch_pending_domestic_buy_cost() -> float:
    """미체결 국내 매수 주문 총액 조회

    KIS API를 호출하여 국내 미체결 매수 주문의 총 금액을 반환.
    실패 시 0.0 반환 (warning 로그).
    """
    from app.services.brokers.kis.client import KISClient

    try:
        kis = KISClient()
        pending_orders = await kis.inquire_korea_orders()
        cost = 0.0
        for order in pending_orders:
            if order.get("sll_buy_dvsn_cd") == "02":
                qty = int(order.get("ord_qty", 0))
                price = int(order.get("ord_unpr", 0))
                cost += qty * price
        return cost
    except Exception as e:
        logger.warning(f"미체결 주문 조회 실패 (계속 진행): {e}")
        return 0.0


async def fetch_pending_overseas_buy_cost() -> float:
    """미체결 해외 매수 주문 총액 조회

    KIS API를 호출하여 해외(NASD) 미체결 매수 주문의 총 금액을 반환.
    실패 시 0.0 반환 (warning 로그).
    """
    from app.services.brokers.kis.client import KISClient

    try:
        kis = KISClient()
        pending_orders = await kis.inquire_overseas_orders(exchange_code="NASD")
        cost = 0.0
        for order in pending_orders:
            if order.get("sll_buy_dvsn_cd") == "02":
                qty = float(order.get("ft_ord_qty", 0))
                price = float(order.get("ft_ord_unpr3", 0))
                cost += qty * price
        return cost
    except Exception as e:
        logger.warning(f"해외 미체결 주문 조회 실패 (계속 진행): {e}")
        return 0.0


async def fetch_pending_crypto_buy_cost() -> float:
    """미체결 암호화폐 매수 주문 총액 조회

    Upbit API를 호출하여 미체결 매수 주문의 총 금액을 반환.
    시장가(price) 주문: price가 주문 금액.
    지정가(limit) 주문: price * remaining_volume.
    실패 시 0.0 반환 (warning 로그).
    """
    import app.services.brokers.upbit.client as upbit

    try:
        pending_orders = await upbit.fetch_open_orders()
        cost = 0.0
        for order in pending_orders:
            if order.get("side") == "bid":
                ord_type = order.get("ord_type", "")
                if ord_type == "price":
                    cost += float(order.get("price", 0))
                else:
                    price_val = float(order.get("price", 0))
                    remaining = float(order.get("remaining_volume", 0))
                    cost += price_val * remaining
        return cost
    except Exception as e:
        logger.warning(f"Upbit 미체결 주문 조회 실패 (계속 진행): {e}")
        return 0.0
