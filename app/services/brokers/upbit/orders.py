"""Upbit order-related API functions.

Handles order placement, cancellation, modification, and order queries.
Market data functions remain in client.py.
"""

from __future__ import annotations

import logging
from typing import Any

import app.services.brokers.upbit.client as _client

logger = logging.getLogger(__name__)


async def fetch_open_orders(market: str | None = None) -> list[dict[str, Any]]:
    """체결 대기 중인 주문 목록을 조회합니다.

    Parameters
    ----------
    market : str, optional
        특정 마켓만 조회하려면 마켓 코드 지정 (예: "KRW-BTC")
        None이면 모든 마켓의 주문을 조회

    Returns
    -------
    list[dict]
        체결 대기 중인 주문 목록
    """
    url = f"{_client.UPBIT_REST}/orders"
    params: dict[str, Any] = {}
    if market:
        params["market"] = market
    params["state"] = "wait"

    return await _client._request_with_auth("GET", url, query_params=params)


async def cancel_orders(order_uuids: list[str]) -> list[dict[str, Any]]:
    """주문을 취소합니다.

    Parameters
    ----------
    order_uuids : list[str]
        취소할 주문들의 UUID 리스트

    Returns
    -------
    list[dict]
        취소된 주문들의 정보 리스트
    """
    results = []

    for order_uuid in order_uuids:
        url = f"{_client.UPBIT_REST}/order"
        params = {"uuid": order_uuid}

        try:
            result = await _client._request_with_auth(
                "DELETE", url, query_params=params
            )
            results.append(result)
        except Exception as e:
            print(f"주문 {order_uuid} 취소 실패: {e}")
            results.append({"uuid": order_uuid, "error": str(e)})

    return results


async def place_sell_order(market: str, volume: str, price: str) -> dict[str, Any]:
    """지정가 매도 주문을 넣습니다.

    Parameters
    ----------
    market : str
        마켓 코드 (예: "KRW-BTC")
    volume : str
        매도할 수량 (문자열로 전달)
    price : str
        매도 가격 (문자열로 전달)

    Returns
    -------
    dict
        주문 결과 정보
    """
    url = f"{_client.UPBIT_REST}/orders"

    body_params = {
        "market": market,
        "side": "ask",  # 매도
        "volume": volume,
        "price": price,
        "ord_type": "limit",  # 지정가 주문
    }

    return await _client._request_with_auth("POST", url, body_params=body_params)


async def place_market_sell_order(market: str, volume: str) -> dict[str, Any]:
    """시장가 전량 매도 주문을 넣습니다.

    Parameters
    ----------
    market : str
        마켓 코드 (예: "KRW-BTC")
    volume : str
        매도할 수량 (문자열로 전달, 보유 전량)

    Returns
    -------
    dict
        주문 결과 정보
    """
    url = f"{_client.UPBIT_REST}/orders"

    body_params = {
        "market": market,
        "side": "ask",  # 매도
        "volume": volume,
        "ord_type": "market",  # 시장가 주문 (즉시 체결)
    }

    return await _client._request_with_auth("POST", url, body_params=body_params)


async def place_buy_order(
    market: str,
    price: str,
    volume: str | None = None,
    ord_type: str = "limit",
) -> dict[str, Any]:
    """매수 주문을 넣습니다.

    Parameters
    ----------
    market : str
        마켓 코드 (예: "KRW-BTC")
    price : str
        매수 가격 (지정가) 또는 매수 금액 (시장가)
    volume : str, optional
        매수할 수량 (지정가일 때 필요)
    ord_type : str, default "limit"
        주문 타입 ("limit": 지정가, "price": 시장가 매수)

    Returns
    -------
    dict
        주문 결과 정보
    """
    url = f"{_client.UPBIT_REST}/orders"

    body_params: dict[str, Any] = {
        "market": market,
        "side": "bid",  # 매수
        "ord_type": ord_type,
    }

    if ord_type == "limit":
        # 지정가 매수: 수량과 가격 모두 필요
        if not volume:
            raise ValueError("지정가 매수는 volume이 필요합니다")
        body_params["volume"] = volume
        body_params["price"] = price
    elif ord_type == "price":
        # 시장가 매수: 매수 금액만 필요
        body_params["price"] = price
    else:
        raise ValueError("ord_type은 'limit' 또는 'price'여야 합니다")

    return await _client._request_with_auth("POST", url, body_params=body_params)


async def place_market_buy_order(market: str, price: str) -> dict[str, Any]:
    """시장가 매수 주문을 넣습니다 (지정 금액만큼 매수).

    Parameters
    ----------
    market : str
        마켓 코드 (예: "KRW-BTC")
    price : str
        매수할 금액 (문자열로 전달)

    Returns
    -------
    dict
        주문 결과 정보
    """
    return await place_buy_order(market, price, ord_type="price")


async def fetch_closed_orders(
    market: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """체결 완료 주문 목록 조회.

    Args:
        market: 마켓코드 필터 (옵션), None이면 전체 조회
        limit: 반환할 건수 (기본값 20)

    Returns:
        체결 주문 목록 (list of dict)
    """
    url = f"{_client.UPBIT_REST}/orders/closed"
    params: dict[str, Any] = {"states[]": ["done", "cancel"], "limit": limit}
    if market:
        params["market"] = market

    return await _client._request_with_auth("GET", url, query_params=params)


async def fetch_order_detail(order_uuid: str) -> dict[str, Any]:
    """단건 주문 상세 조회.

    Args:
        order_uuid: 주문 고유 ID

    Returns:
        주문 상세 정보
    """
    url = f"{_client.UPBIT_REST}/order"
    params = {"uuid": order_uuid}

    return await _client._request_with_auth("GET", url, query_params=params)


def adjust_price_to_upbit_unit(price: float) -> float:
    """업비트 가격 단위에 맞게 가격을 조정합니다.

    업비트 가격 단위 규칙:
    - 2,000,000원 이상: 1,000원 단위
    - 1,000,000원 이상 ~ 2,000,000원 미만: 500원 단위
    - 500,000원 이상 ~ 1,000,000원 미만: 100원 단위
    - 100,000원 이상 ~ 500,000원 미만: 50원 단위
    - 10,000원 이상 ~ 100,000원 미만: 10원 단위
    - 1,000원 이상 ~ 10,000원 미만: 5원 단위
    - 100원 이상 ~ 1,000원 미만: 1원 단위
    - 10원 이상 ~ 100원 미만: 0.1원 단위
    - 1원 이상 ~ 10원 미만: 0.01원 단위
    - 0.1원 이상 ~ 1원 미만: 0.001원 단위
    - 0.01원 이상 ~ 0.1원 미만: 0.0001원 단위
    - 0.01원 미만: 0.00001원 단위
    """
    if price >= 2_000_000:
        return round(price / 1000) * 1000
    elif price >= 1_000_000:
        return round(price / 500) * 500
    elif price >= 500_000:
        return round(price / 100) * 100
    elif price >= 100_000:
        return round(price / 50) * 50
    elif price >= 10_000:
        return round(price / 10) * 10
    elif price >= 1000:
        return round(price / 5) * 5
    elif price >= 100:
        return round(price)
    elif price >= 10:
        return round(price, 1)
    elif price >= 1:
        return round(price, 2)
    elif price >= 0.1:
        return round(price, 3)
    elif price >= 0.01:
        return round(price, 4)
    else:
        return round(price, 5)


async def cancel_and_reorder(
    order_uuid: str,
    new_price: float,
    new_quantity: float | None = None,
) -> dict[str, Any]:
    """주문 취소 후 재주문 (지정가 대기주문만 지원).

    Args:
        order_uuid: 취소 후 재주문할 주문 UUID
        new_price: 새 주문가격
        new_quantity: 새 주문수량 (None이면 잔량 유지)

    Returns:
        ``{"original_order": ..., "cancel_result": ..., "new_order": ...}``
    """
    # 1. 원주문 조회
    original_order = await fetch_order_detail(order_uuid)

    # 2. 지원 조건 확인
    if original_order.get("state") != "wait":
        return {
            "original_order": original_order,
            "cancel_result": {
                "success": False,
                "error": "Only wait-state orders can be modified",
            },
            "new_order": None,
        }

    if original_order.get("ord_type") != "limit":
        return {
            "original_order": original_order,
            "cancel_result": {
                "success": False,
                "error": "Only limit orders can be modified",
            },
            "new_order": None,
        }

    # 3. 새 수량 결정 및 유효성 검사
    if new_quantity is None:
        new_quantity = float(original_order.get("remaining_volume", 0))

    # 수량 유효성 검사: 0 이하면 즉시 실패
    if new_quantity <= 0:
        return {
            "original_order": original_order,
            "cancel_result": {
                "success": False,
                "error": "Invalid new quantity: must be positive",
            },
            "new_order": None,
        }

    # 4. 가격 보정 (업비트 단위에 맞춰)
    side = original_order.get("side")
    market = original_order.get("market")
    if not isinstance(market, str) or not market:
        return {
            "original_order": original_order,
            "cancel_result": {
                "success": False,
                "error": "Original order is missing market",
            },
            "new_order": None,
        }
    adjusted_price = adjust_price_to_upbit_unit(new_price)

    # 5. 취소 후 재주문
    cancel_result = await cancel_orders([order_uuid])

    if cancel_result and len(cancel_result) > 0 and "error" not in cancel_result[0]:
        # 취소 성공하면 재주문
        volume_str = f"{new_quantity:.8f}" if new_quantity else ""
        price_str = f"{adjusted_price:.5f}".rstrip("0").rstrip(".") if new_price else ""

        try:
            # side에 따라 적절한 메서드 호출
            if side == "bid":
                new_order = await place_buy_order(
                    market, price_str, volume_str, "limit"
                )
            else:
                new_order = await place_sell_order(market, volume_str, price_str)

            return {
                "original_order": original_order,
                "cancel_result": cancel_result[0],
                "new_order": new_order,
            }
        except Exception as e:
            return {
                "original_order": original_order,
                "cancel_result": cancel_result[0],
                "new_order": {"error": str(e)},
            }
    else:
        return {
            "original_order": original_order,
            "cancel_result": cancel_result[0]
            if cancel_result
            else {"success": False, "error": "cancel failed"},
            "new_order": None,
        }
