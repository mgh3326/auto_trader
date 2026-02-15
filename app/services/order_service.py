"""
Order-related helper functions shared between router endpoints and TaskIQ tasks.
"""

from __future__ import annotations

from app.core.db import AsyncSessionLocal
from app.services import upbit
from app.services.stock_info_service import StockAnalysisService


async def cancel_existing_buy_orders(market: str) -> None:
    """Cancel any existing bid orders for the given market."""
    try:
        open_orders = await upbit.fetch_open_orders(market)
        buy_orders = [order for order in open_orders if order.get("side") == "bid"]

        if buy_orders:
            order_uuids = [order["uuid"] for order in buy_orders]
            await upbit.cancel_orders(order_uuids)
    except Exception as exc:  # pragma: no cover - defensive logging
        print(f"⚠️ {market} 매수 주문 취소 실패: {exc}")


async def cancel_existing_sell_orders(market: str) -> None:
    """Cancel any existing ask orders for the given market."""
    try:
        open_orders = await upbit.fetch_open_orders(market)
        sell_orders = [order for order in open_orders if order.get("side") == "ask"]

        if sell_orders:
            order_uuids = [order["uuid"] for order in sell_orders]
            await upbit.cancel_orders(order_uuids)
    except Exception as exc:  # pragma: no cover - defensive logging
        print(f"⚠️ {market} 매도 주문 취소 실패: {exc}")


async def get_sell_prices_for_coin(
    currency: str,
    avg_buy_price: float,
    current_price: float,
) -> list[float]:
    """Fetch recommended sell prices for a coin based on the latest analysis."""
    try:
        symbol = f"KRW-{currency}"

        async with AsyncSessionLocal() as db:
            service = StockAnalysisService(db)
            analysis = await service.get_latest_analysis_by_symbol(symbol)

        if not analysis:
            return []

        sell_prices: list[float] = []

        if analysis.appropriate_sell_min is not None:
            sell_prices.append(analysis.appropriate_sell_min)
        if analysis.appropriate_sell_max is not None:
            sell_prices.append(analysis.appropriate_sell_max)
        if analysis.sell_target_min is not None:
            sell_prices.append(analysis.sell_target_min)
        if analysis.sell_target_max is not None:
            sell_prices.append(analysis.sell_target_max)

        min_sell_price = avg_buy_price * 1.01
        valid_prices = [
            price
            for price in sell_prices
            if price >= min_sell_price and price >= current_price
        ]
        valid_prices.sort()
        return valid_prices
    except Exception:  # pragma: no cover - defensive fallback
        return []


async def place_multiple_sell_orders(
    market: str,
    balance: float,
    sell_prices: list[float],
    currency: str,
) -> dict:
    """Submit split sell orders across the provided price levels."""

    def format_price(value: float) -> str:
        return f"{value:,.0f}"

    if not sell_prices:
        return {"success": False, "message": "매도 가격이 없습니다", "orders_placed": 0}

    orders_placed = 0

    if len(sell_prices) == 1:
        target_price = sell_prices[0]
        print(
            f"💰 전량 매도 주문 시도: {format_price(target_price)}원, 수량 {balance:.8f}"
        )
        result = await place_sell_order_single(market, balance, target_price)
        if result:
            print("✅ 전량 매도 주문 성공")
            return {
                "success": True,
                "message": "전량 매도 주문 완료",
                "orders_placed": 1,
            }
        print("❌ 전량 매도 주문 실패")
        return {"success": False, "message": "매도 주문 실패", "orders_placed": 0}

    sell_prices_sorted = sorted(sell_prices)
    split_ratio = 1.0 / len(sell_prices_sorted)
    min_split_volume = balance * split_ratio
    first_sell_price = sell_prices_sorted[0]
    split_amount = (balance * split_ratio) * first_sell_price

    if min_split_volume < 0.00000001 or split_amount < 10000:
        lowest_price = sell_prices_sorted[0]
        print("⚠️ 분할 매도 불가: 최소 분할 수량/금액 미충족, 전량 매도로 전환")
        print(
            f"💰 전량 매도 주문 시도: {format_price(lowest_price)}원, 수량 {balance:.8f}"
        )
        result = await place_sell_order_single(market, balance, lowest_price)
        if result:
            print("✅ 전량 매도 주문 성공")
            return {
                "success": True,
                "message": "분할 불가능하여 전량 매도",
                "orders_placed": 1,
            }
        print("❌ 전량 매도 주문 실패 (분할 불가)")
        return {
            "success": False,
            "message": "매도 주문 실패 (분할 불가)",
            "orders_placed": 0,
        }

    split_prices = sell_prices_sorted[:-1]
    highest_price = sell_prices_sorted[-1]

    print(f"🎯 총 {len(sell_prices_sorted)}개 가격에서 매도 주문 실행:")
    for index, sell_price in enumerate(split_prices, 1):
        try:
            split_volume = balance * split_ratio
            if split_volume < 0.00000001:
                continue

            adjusted_sell_price = upbit.adjust_price_to_upbit_unit(sell_price)
            volume_str = f"{split_volume:.8f}"
            price_str = f"{adjusted_sell_price}"

            print(
                f"[{index}/{len(sell_prices_sorted)}] {format_price(adjusted_sell_price)}원 매도 주문, 수량 {split_volume:.8f}"
            )
            result = await upbit.place_sell_order(market, volume_str, price_str)
            if result:
                orders_placed += 1
                print("    ✅ 매도 주문 성공")
            else:
                print("    ❌ 매도 주문 실패")
        except Exception as exc:
            print(f"    ❌ 분할 매도 주문 실패: {exc}")

    try:
        current_coins = await upbit.fetch_my_coins()
        current_balance = 0.0
        for coin in current_coins:
            if coin.get("currency") == currency:
                current_balance = float(coin["balance"])
                break

        if current_balance >= 0.00000001:
            adjusted_highest_price = upbit.adjust_price_to_upbit_unit(highest_price)
            volume_str = f"{current_balance:.8f}"
            price_str = f"{adjusted_highest_price}"

            print(
                f"[마지막] 잔량 전량 매도: {format_price(adjusted_highest_price)}원, 수량 {current_balance:.8f}"
            )
            result = await upbit.place_sell_order(market, volume_str, price_str)
            if result:
                orders_placed += 1
                print("    ✅ 잔량 매도 주문 성공")
            else:
                print("    ❌ 잔량 매도 주문 실패")
    except Exception as exc:
        print(f"❌ 잔량 매도 주문 실패: {exc}")

    if orders_placed > 0:
        return {
            "success": True,
            "message": f"{orders_placed}단계 분할 매도 완료",
            "orders_placed": orders_placed,
        }
    return {"success": False, "message": "모든 매도 주문 실패", "orders_placed": 0}


async def place_sell_order_single(market: str, balance: float, sell_price: float):
    """Submit a single sell order."""
    try:
        volume_str = f"{balance:.8f}"
        adjusted_sell_price = upbit.adjust_price_to_upbit_unit(sell_price)
        price_str = f"{adjusted_sell_price}"

        result = await upbit.place_sell_order(market, volume_str, price_str)
        return result
    except Exception as exc:
        print(f"매도 주문 실패: {exc}")
        return None


__all__ = [
    "cancel_existing_buy_orders",
    "cancel_existing_sell_orders",
    "get_sell_prices_for_coin",
    "place_multiple_sell_orders",
    "place_sell_order_single",
]
