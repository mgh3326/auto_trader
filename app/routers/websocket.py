"""WebSocket router for real-time market data streaming."""

import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.services.upbit_market_websocket import UpbitPublicWebSocketClient
from app.services.websocket_connection_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])

_upbit_client: UpbitPublicWebSocketClient | None = None
_upbit_client_task: asyncio.Task | None = None
_upbit_client_lock = asyncio.Lock()


def _log_task_exception(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:
        logger.exception("Upbit public WebSocket task crashed")


async def get_upbit_client() -> UpbitPublicWebSocketClient:
    """Get or create global Upbit WebSocket client instance."""
    global _upbit_client
    global _upbit_client_task

    if _upbit_client is None:
        async with _upbit_client_lock:
            if _upbit_client is None:
                logger.info("Creating Upbit public WebSocket client...")
                _upbit_client = UpbitPublicWebSocketClient(
                    subscription_type="ticker",
                    codes=None,
                )
                _upbit_client_task = asyncio.create_task(_upbit_client.start())
                _upbit_client_task.add_done_callback(_log_task_exception)

    return _upbit_client


@router.websocket("/ws/market")
async def websocket_market_data(
    websocket: WebSocket,
    codes: str | None = Query(
        default=None,
        description="Comma-separated market codes (e.g., KRW-BTC,KRW-ETH)",
    ),
):
    """
    WebSocket endpoint for real-time market data streaming.

    Connects to this endpoint to receive real-time ticker data from Upbit.

    Query Parameters:
        codes: Optional comma-separated list of market codes to filter
                (e.g., KRW-BTC,KRW-ETH). If not provided, receives all markets.

    Example:
        ```python
        # Connect to receive all KRW market data
        ws = create_connection("ws://localhost:8000/api/v1/ws/market")

        # Connect to receive specific markets only
        ws = create_connection("ws://localhost:8000/api/v1/ws/market?codes=KRW-BTC,KRW-ETH")
        ```

    Message Format:
        Each message is a JSON object containing ticker data from Upbit:
        ```json
        {
            "type": "ticker",
            "code": "KRW-BTC",
            "opening_price": 95000000,
            "high_price": 96000000,
            "low_price": 94000000,
            "trade_price": 95500000,
            "prev_closing_price": 95000000,
            "change": "RISE",
            "change_price": 500000,
            "change_rate": 0.0053,
            "signed_change_price": 500000,
            "signed_change_rate": 0.0053,
            "trade_volume": 0.1,
            "acc_trade_volume": 100.5,
            "acc_trade_price": 9500000000,
            "timestamp": 1234567890123,
            "acc_ask_volume": 50.2,
            "acc_bid_volume": 50.3,
            "highest_52_week_price": 100000000,
            "highest_52_week_date": "2024-01-01",
            "lowest_52_week_price": 80000000,
            "lowest_52_week_date": "2023-06-01"
        }
        ```
    """
    await manager.connect(websocket)

    try:
        await get_upbit_client()

        if codes:
            market_list = [c.strip() for c in codes.split(",") if c.strip()]
            logger.info(f"Client filtering for markets: {market_list}")
        else:
            market_list = None
            logger.info("Client subscribing to all markets")

        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected normally")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        await manager.disconnect(websocket)


@router.get("/ws/connections")
async def get_connection_count():
    """Get the current number of active WebSocket connections."""
    count = await manager.get_connection_count()
    return {"active_connections": count}
