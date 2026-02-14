"""Tests for Sentry integration in upbit websocket monitor entrypoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from upbit_websocket_monitor import main


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_captures_fatal_exception():
    mock_analyzer = AsyncMock()
    mock_analyzer.close = AsyncMock()

    mock_service = AsyncMock()
    mock_service.start_monitoring = AsyncMock(side_effect=RuntimeError("fatal"))
    mock_service.stop_monitoring = AsyncMock()

    with (
        patch("upbit_websocket_monitor.init_sentry") as mock_init_sentry,
        patch("upbit_websocket_monitor.capture_exception") as mock_capture_exception,
        patch(
            "upbit_websocket_monitor.upbit_pairs.prime_upbit_constants",
            new=AsyncMock(),
        ),
        patch("upbit_websocket_monitor.UpbitAnalyzer", return_value=mock_analyzer),
        patch(
            "upbit_websocket_monitor.UpbitOrderAnalysisService",
            return_value=mock_service,
        ),
    ):
        await main()

    mock_init_sentry.assert_called_once_with(service_name="auto-trader-upbit-ws")
    mock_capture_exception.assert_called_once()
    mock_service.stop_monitoring.assert_awaited_once()
    mock_analyzer.close.assert_awaited_once()
