# tests/services/test_live_reconcile_metrics.py
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.live_reconcile_metrics import (
    _compute_metrics,
)


@pytest.mark.asyncio
async def test_unreconciled_live_order_metrics_age_brackets():
    ref_time = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

    # 30m old
    r_30m = MagicMock()
    r_30m.market = "us"
    r_30m.broker = "kis"
    r_30m.created_at = ref_time - timedelta(minutes=30)
    r_30m.trade_date = None

    # 2h old
    r_2h = MagicMock()
    r_2h.market = "us"
    r_2h.broker = "kis"
    r_2h.created_at = ref_time - timedelta(hours=2)
    r_2h.trade_date = None

    # 25h old
    r_25h = MagicMock()
    r_25h.market = "crypto"
    r_25h.broker = "upbit"
    r_25h.created_at = ref_time - timedelta(hours=25)
    r_25h.trade_date = None

    # 75h old (KR)
    r_75h = MagicMock()
    r_75h.broker = "kis"
    r_75h.created_at = ref_time - timedelta(hours=75)
    r_75h.trade_date = None

    mock_db = AsyncMock()

    res_live = MagicMock()
    res_live.scalars.return_value.all.return_value = [r_30m, r_2h, r_25h]

    res_kis = MagicMock()
    res_kis.scalars.return_value.all.return_value = [r_75h]

    mock_db.execute.side_effect = [res_live, res_kis]

    metrics = await _compute_metrics(mock_db, ref_time)

    assert metrics["total_unreconciled"] == 4
    assert metrics["by_bracket"]["1h_plus"] == 3  # 2h, 25h, 75h
    assert metrics["by_bracket"]["24h_plus"] == 2  # 25h, 75h
    assert metrics["by_bracket"]["72h_plus"] == 1  # 75h

    assert metrics["by_market_broker"]["us:kis"]["total"] == 2
    assert metrics["by_market_broker"]["us:kis"]["1h_plus"] == 1
    assert metrics["by_market_broker"]["us:kis"]["24h_plus"] == 0

    assert metrics["by_market_broker"]["crypto:upbit"]["total"] == 1
    assert metrics["by_market_broker"]["crypto:upbit"]["24h_plus"] == 1

    assert metrics["by_market_broker"]["kr:kis"]["total"] == 1
    assert metrics["by_market_broker"]["kr:kis"]["72h_plus"] == 1
