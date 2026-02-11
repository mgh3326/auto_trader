"""Upbit Orderbook Service

Fetches real-time orderbook data from Upbit API.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

UPBIT_REST = "https://api.upbit.com/v1"


async def fetch_orderbook(market: str = "KRW-BTC") -> dict[str, Any]:
    """
    호가(오더북) 정보를 가져옵니다.

    Args:
        market: 마켓 코드 (예: "KRW-BTC", "KRW-ETH")

    Returns:
        dict: 호가 정보
        - timestamp: 요청 체결 시간 (Unix timestamp)
        - orderbook_units: 호가 유닛 리스트
          - ask_price: 매도 호가
          - bid_price: 매수 호가
          - ask_size: 매도 잔량
          - bid_size: 매수 잔량
    """
    url = f"{UPBIT_REST}/orderbook"
    params = {"markets": market}

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            if not data or len(data) == 0:
                logger.warning(f"마켓 {market}에 대한 호가 데이터가 없습니다.")
                return {}

            orderbook_data = data[0]
            return {
                "market": orderbook_data.get("market"),
                "timestamp": orderbook_data.get("timestamp"),
                "total_ask_size": orderbook_data.get("total_ask_size"),
                "total_bid_size": orderbook_data.get("total_bid_size"),
                "orderbook_units": orderbook_data.get("orderbook_units", []),
            }
    except httpx.HTTPStatusError as e:
        logger.error(f"Upbit API 호출 실패: {e.response.status_code}")
        raise
    except httpx.RequestError as e:
        logger.error(f"요청 에러: {e}")
        raise
    except Exception as e:
        logger.error(f"호가 데이터 가져오기 실패: {e}")
        raise


async def fetch_multiple_orderbooks(markets: list[str]) -> dict[str, dict[str, Any]]:
    """
    여러 마켓의 호가 정보를 가져옵니다.

    Args:
        markets: 마켓 코드 리스트 (예: ["KRW-BTC", "KRW-ETH"])

    Returns:
        dict: 마켓별 호가 정보
        {
            "KRW-BTC": { ... },
            "KRW-ETH": { ... }
        }
    """
    if not markets:
        return {}

    results = {}
    for market in markets:
        try:
            orderbook = await fetch_orderbook(market)
            if orderbook:
                results[market] = orderbook
        except Exception as e:
            logger.error(f"{market} 호가 가져오기 실패: {e}")
            results[market] = {}

    return results
