"""Orderbook Router

Provides WebSocket endpoints for real-time orderbook data broadcasting.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.templates import templates
from app.services import upbit_orderbook
from app.services.websocket_connection_manager import manager
from data.coins_info import upbit_pairs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orderbook", tags=["Orderbook"])

UPDATE_INTERVAL = 1


@router.get("/", response_class=HTMLResponse)
async def orderbook_dashboard(request: Request):
    """Orderbook dashboard page"""
    return templates.TemplateResponse(
        "orderbook_dashboard.html",
        {"request": request},
    )


@router.get("/api/markets")
async def get_available_markets(db: AsyncSession = Depends(get_db)):
    """
    Get list of available KRW markets.

    Returns:
        - markets: list of market codes (e.g. ["KRW-BTC", "KRW-ETH"])
    """
    try:
        await upbit_pairs.prime_upbit_constants()
        markets = sorted(upbit_pairs.KRW_TRADABLE_COINS)
        return {"markets": markets}
    except Exception as e:
        logger.error(f"Failed to get markets: {e}")
        return {"markets": []}


@router.get("/api/orderbook/{market}")
async def get_orderbook(market: str):
    """
    Get orderbook data for a specific market.

    Args:
        market: market code (e.g. "KRW-BTC")

    Returns:
        - market: market code
        - timestamp: timestamp
        - orderbook_units: orderbook units list
    """
    try:
        orderbook = await upbit_orderbook.fetch_orderbook(market)
        return orderbook
    except Exception as e:
        logger.error(f"Failed to fetch orderbook ({market}): {e}")
        return {"error": str(e)}


@router.websocket("/ws")
async def websocket_orderbook(websocket: WebSocket):
    """
    WebSocket endpoint - real-time orderbook data broadcasting

    When a client connects, periodically fetches orderbook data
    and broadcasts to all connected clients.
    """
    await manager.connect(websocket)
    logger.info("WebSocket connected (orderbook)")

    try:
        initial_data = await fetch_all_orderbooks()
        if initial_data:
            await websocket.send_json(initial_data)

        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message.get("type") == "subscribe":
                markets = message.get("markets", [])
                logger.info(f"Subscription request: {markets}")

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected (orderbook)")
        await manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await manager.disconnect(websocket)


async def fetch_all_orderbooks():
    """
    Fetch orderbook data for all KRW markets.

    Returns:
        dict: orderbook data per market
    """
    try:
        await upbit_pairs.prime_upbit_constants()
        markets = upbit_pairs.KRW_TRADABLE_COINS

        orderbooks = await upbit_orderbook.fetch_multiple_orderbooks(markets)

        return {
            "type": "orderbook",
            "timestamp": int(asyncio.get_event_loop().time()),
            "data": orderbooks,
        }
    except Exception as e:
        logger.error(f"Failed to fetch orderbook data: {e}")
        return {
            "type": "error",
            "message": str(e),
        }


async def start_orderbook_broadcast():
    """
    Background task that periodically broadcasts orderbook data.

    Fetches orderbook data for all KRW markets every second
    and sends to all connected clients.
    """
    while True:
        try:
            data = await fetch_all_orderbooks()
            if data.get("type") == "orderbook":
                connection_count = await manager.get_connection_count()
                if connection_count > 0:
                    await manager.broadcast(data)
                    logger.debug(
                        f"Orderbook broadcast completed "
                        f"({len(data.get('data', {}))} markets, {connection_count} connections)"
                    )
        except Exception as e:
            logger.error(f"Orderbook broadcast failed: {e}")

        await asyncio.sleep(UPDATE_INTERVAL)
