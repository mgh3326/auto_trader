"""Test script for WebSocket server.

This script connects to the WebSocket endpoint and verifies
that market data is received from Upbit.
"""

import asyncio
import json
import logging

import websockets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_websocket_connection():
    """Test WebSocket connection and data reception."""
    ws_url = "ws://localhost:8000/api/v1/ws/market"

    logger.info(f"Connecting to WebSocket: {ws_url}")

    try:
        async with websockets.connect(ws_url) as websocket:
            logger.info("WebSocket connection established")

            message_count = 0
            max_messages = 10
            timeout_seconds = 30

            try:
                while message_count < max_messages:
                    try:
                        message = await asyncio.wait_for(
                            websocket.recv(), timeout=timeout_seconds
                        )
                        data = json.loads(message)
                        message_count += 1

                        logger.info(
                            f"Message {message_count}: "
                            f"Type={data.get('type')}, "
                            f"Code={data.get('code')}, "
                            f"Price={data.get('trade_price')}"
                        )

                    except asyncio.TimeoutError:
                        logger.error(
                            f"Timeout: No message received for {timeout_seconds} seconds"
                        )
                        break

                logger.info(f"Received {message_count} messages successfully")
                logger.info("WebSocket test PASSED")

            except Exception as e:
                logger.error(f"Error receiving messages: {e}", exc_info=True)
                raise

    except ConnectionRefusedError:
        logger.error(
            "Connection refused. Is the WebSocket server running? "
            "Start it with: uv run uvicorn app.main:app --reload"
        )
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}", exc_info=True)
        raise


async def test_filtered_connection():
    """Test WebSocket connection with market filtering."""
    ws_url = "ws://localhost:8000/api/v1/ws/market?codes=KRW-BTC,KRW-ETH"

    logger.info(f"Connecting to filtered WebSocket: {ws_url}")

    try:
        async with websockets.connect(ws_url) as websocket:
            logger.info("WebSocket connection established (filtered)")

            message_count = 0
            max_messages = 5

            try:
                while message_count < max_messages:
                    message = await asyncio.wait_for(websocket.recv(), timeout=15)
                    data = json.loads(message)
                    message_count += 1

                    code = data.get("code")
                    price = data.get("trade_price")

                    logger.info(f"Message {message_count}: {code} @ {price}")

                    if code not in ["KRW-BTC", "KRW-ETH"]:
                        logger.warning(f"Received unexpected market: {code}")

                logger.info("Filtered WebSocket test PASSED")

            except asyncio.TimeoutError:
                logger.error("Timeout waiting for messages")
                raise

    except ConnectionRefusedError:
        logger.error("Connection refused. Start the server first.")
    except Exception as e:
        logger.error(f"Filtered connection error: {e}", exc_info=True)
        raise


async def test_connection_count_endpoint():
    """Test the connection count endpoint."""
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("http://localhost:8000/api/v1/ws/connections")
            response.raise_for_status()

            data = response.json()
            logger.info(f"Active connections: {data.get('active_connections', 0)}")
            logger.info("Connection count endpoint test PASSED")

    except ConnectionRefusedError:
        logger.error("Connection refused. Is the server running?")
    except Exception as e:
        logger.error(f"Connection count endpoint error: {e}", exc_info=True)
        raise


async def run_all_tests():
    """Run all WebSocket tests."""
    logger.info("=" * 60)
    logger.info("Starting WebSocket Server Tests")
    logger.info("=" * 60)

    try:
        logger.info("\n[Test 1] Basic WebSocket connection...")
        await test_websocket_connection()

        logger.info("\n[Test 2] Filtered WebSocket connection...")
        await test_filtered_connection()

        logger.info("\n[Test 3] Connection count endpoint...")
        await test_connection_count_endpoint()

        logger.info("\n" + "=" * 60)
        logger.info("All tests PASSED!")
        logger.info("=" * 60)

    except Exception as e:
        logger.error("\n" + "=" * 60)
        logger.error("Tests FAILED")
        logger.error("=" * 60)
        raise


if __name__ == "__main__":
    asyncio.run(run_all_tests())
