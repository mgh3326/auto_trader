"""ROB-595: Toss orderbook balance rate and AI signal (KR).

Safety:
  * Live fetch is OFF by default (``settings.toss_consumer_signals_enabled``).
    Off → status="disabled".
  * AGGREGATE ONLY: buyBalanceRate, sellBalanceRate, foreignerRatio, signalDirection, reasoning, relatedReasoning.
  * No DB. Live per-call.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.config import settings
from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.services.toss_consumer.client import TossConsumerClient, _to_product_code


async def handle_get_toss_buy_balance(symbol: str) -> dict[str, Any]:
    """Toss orderbook balance rate (buyBalanceRate/sellBalanceRate) and foreigner ratio. Live per-call, operator-gated."""
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Toss buy balance rate is only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    now = dt.datetime.now(dt.UTC)
    base = {
        "symbol": symbol,
        "source": "toss_consumer",
        "market": "kr",
        "observed_at": now.isoformat(),
    }

    # Pattern B gate
    if not settings.toss_consumer_signals_enabled:
        return {
            **base,
            "status": "disabled",
            "note": (
                "Toss consumer signals are disabled (set TOSS_CONSUMER_SIGNALS_ENABLED=true after ToS review)"
            ),
        }

    try:
        product_code = _to_product_code(symbol)
        client = TossConsumerClient()
        result = await client.fetch_buy_balance(product_code)
        return {
            **base,
            "status": "ok",
            **result,
        }
    except Exception as exc:  # noqa: BLE001
        return _error_payload(
            source="toss_consumer",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )


async def handle_get_toss_ai_signal(symbol: str) -> dict[str, Any]:
    """Toss AI signal (direction + reasoning). Live per-call, operator-gated."""
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Toss AI signal is only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    now = dt.datetime.now(dt.UTC)
    base = {
        "symbol": symbol,
        "source": "toss_consumer",
        "market": "kr",
        "observed_at": now.isoformat(),
    }

    # Pattern B gate
    if not settings.toss_consumer_signals_enabled:
        return {
            **base,
            "status": "disabled",
            "note": (
                "Toss consumer signals are disabled (set TOSS_CONSUMER_SIGNALS_ENABLED=true after ToS review)"
            ),
        }

    try:
        product_code = _to_product_code(symbol)
        client = TossConsumerClient()
        result = await client.fetch_ai_signal(product_code)
        return {
            **base,
            "status": "ok",
            **result,
        }
    except Exception as exc:  # noqa: BLE001
        return _error_payload(
            source="toss_consumer",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )
