"""ROB-595: Naver analyst consensus tool handler (KR)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.services.naver_finance.consensus import fetch_analyst_consensus


async def handle_get_analyst_consensus(symbol: str) -> dict[str, Any]:
    """Get analyst consensus (recommendation mean and price target mean) for a Korean stock. Live per-call."""
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Analyst consensus is only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    now = dt.datetime.now(dt.UTC)
    base = {
        "symbol": symbol,
        "source": "naver_integration",
        "market": "kr",
        "observed_at": now.isoformat(),
    }

    try:
        clean_code = symbol
        if len(clean_code) == 7 and clean_code.upper().startswith("A"):
            clean_code = clean_code[1:]

        result = await fetch_analyst_consensus(clean_code)
        return {
            **base,
            "status": "ok",
            **result,
        }
    except Exception as exc:  # noqa: BLE001
        return _error_payload(
            source="naver_integration",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )
