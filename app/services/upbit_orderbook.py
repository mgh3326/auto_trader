"""Upbit Orderbook Service

Fetches real-time orderbook data from Upbit API.
"""

import asyncio
import logging
from typing import Any

import httpx
from httpx import HTTPStatusError

from data.coins_info import upbit_pairs

logger = logging.getLogger(__name__)

UPBIT_REST = "https://api.upbit.com/v1"
UPBIT_ORDERBOOK_URL = f"{UPBIT_REST}/orderbook"
ORDERBOOK_TIMEOUT_SECONDS = 5
# 업비트 orderbook 조회를 한 번에 너무 크게 보내면 429가 자주 발생하므로 청크 단위로 분할
MAX_MARKETS_PER_REQUEST = 30
INTER_CHUNK_DELAY_SECONDS = 0.12


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


async def _normalize_market_code(market: str) -> str | None:
    market_code = market.strip().upper()
    if not market_code:
        return None

    if market_code.startswith("KRW-"):
        return market_code

    await upbit_pairs.prime_upbit_constants()
    return upbit_pairs.COIN_TO_PAIR.get(market_code)


def _build_orderbook_result(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for orderbook_data in rows:
        market = orderbook_data.get("market")
        if not market:
            continue
        result[market] = {
            "market": market,
            "timestamp": orderbook_data.get("timestamp"),
            "total_ask_size": orderbook_data.get("total_ask_size"),
            "total_bid_size": orderbook_data.get("total_bid_size"),
            "orderbook_units": orderbook_data.get("orderbook_units", []),
        }
    return result


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
    normalized_market = await _normalize_market_code(market)
    if not normalized_market:
        logger.warning("지원하지 않는 마켓 코드: %s", market)
        return {}

    try:
        async with httpx.AsyncClient(timeout=ORDERBOOK_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{UPBIT_ORDERBOOK_URL}?markets={normalized_market}"
            )
            response.raise_for_status()
            data = response.json()

            if not data or len(data) == 0:
                logger.warning("마켓 %s에 대한 호가 데이터가 없습니다.", normalized_market)
                return {}

            return _build_orderbook_result(data).get(normalized_market, {})
    except httpx.HTTPStatusError as e:
        logger.error("Upbit API 호출 실패 (%s): %s", normalized_market, e.response.status_code)
        raise
    except httpx.RequestError as e:
        logger.error("요청 에러 (%s): %s", normalized_market, e)
        raise
    except Exception as e:
        logger.error("호가 데이터 가져오기 실패 (%s): %s", normalized_market, e)
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

    normalized_markets: list[str] = []
    seen: set[str] = set()
    for market in markets:
        normalized = await _normalize_market_code(market)
        if not normalized:
            logger.warning("지원하지 않는 마켓 코드: %s", market)
            continue
        if normalized not in seen:
            seen.add(normalized)
            normalized_markets.append(normalized)

    if not normalized_markets:
        return {}

    results: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=ORDERBOOK_TIMEOUT_SECONDS) as client:
        market_chunks = _chunked(normalized_markets, MAX_MARKETS_PER_REQUEST)
        for index, chunk in enumerate(market_chunks):
            markets_query = ",".join(chunk)
            try:
                response = await client.get(f"{UPBIT_ORDERBOOK_URL}?markets={markets_query}")
                response.raise_for_status()
            except HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    logger.warning(
                        "Upbit orderbook rate limit 도달: %s개 중 %s개 조회 후 중단",
                        len(normalized_markets),
                        len(results),
                    )
                    break
                logger.error("Upbit API 호출 실패 (batch=%s): %s", markets_query, status)
                continue
            except httpx.RequestError as exc:
                logger.error("Upbit orderbook 요청 에러 (batch=%s): %s", markets_query, exc)
                continue

            rows = response.json()
            if not isinstance(rows, list):
                logger.warning("Upbit orderbook 응답 형식 이상 (batch=%s)", markets_query)
                continue

            results.update(_build_orderbook_result(rows))
            if index < len(market_chunks) - 1:
                await asyncio.sleep(INTER_CHUNK_DELAY_SECONDS)

    return results
