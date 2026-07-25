# tests/test_live_reconcile_backfill_report_cli.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.live_reconcile_backfill_report import (
    _classify_lookback_window,
    generate_backfill_report,
)


def test_classify_lookback_window():
    # KIS <= 90d -> within_window
    assert _classify_lookback_window("us", "kis", 30.0) == "within_window (<=90d)"
    assert _classify_lookback_window("kr", "kis", 89.9) == "within_window (<=90d)"

    # KIS > 90d -> outside_window
    assert _classify_lookback_window("us", "kis", 91.0) == "outside_window (>90d)"
    assert _classify_lookback_window("kr", "kis", 120.0) == "outside_window (>90d)"

    # Upbit / Crypto -> always UUID lookup
    assert (
        _classify_lookback_window("crypto", "upbit", 150.0)
        == "within_window (UUID lookup)"
    )


@pytest.mark.asyncio
async def test_generate_backfill_report_runs_read_only():
    mock_db = AsyncMock()

    res_live = MagicMock()
    r_live = MagicMock()
    r_live.id = 1
    r_live.market = "us"
    r_live.broker = "kis"
    r_live.symbol = "AAPL"
    r_live.side = "buy"
    r_live.order_no = "12345"
    r_live.status = "accepted"
    r_live.created_at = None
    r_live.trade_date = None
    res_live.scalars.return_value.all.return_value = [r_live]

    res_kis = MagicMock()
    r_kis = MagicMock()
    r_kis.id = 2
    r_kis.broker = "kis"
    r_kis.symbol = "005930"
    r_kis.side = "sell"
    r_kis.order_no = "67890"
    r_kis.status = "accepted"
    r_kis.created_at = None
    r_kis.trade_date = None
    res_kis.scalars.return_value.all.return_value = [r_kis]

    mock_db.execute.side_effect = [res_live, res_kis]

    with patch(
        "scripts.live_reconcile_backfill_report._order_session_factory"
    ) as mock_factory:
        mock_factory.return_value.return_value.__aenter__.return_value = mock_db
        report = await generate_backfill_report("all")

    assert report["total_backlog"] == 2
    assert "us" in report["summary_by_market"]
    assert "kr" in report["summary_by_market"]
