"""Intraday provisional KR investor-flow MCP handler."""

from __future__ import annotations

import datetime
from typing import Any

from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.market_session import kr_market_data_state
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.services.brokers.kis.client import KISClient

DATA_STATE_INTRADAY_PROVISIONAL = "intraday_provisional"

_SLOT_TIMES: dict[str, str] = {
    "1": "09:30",
    "2": "10:00",
    "3": "11:20",
    "4": "13:20",
    "5": "14:30",
}

_PROVISIONAL_NOTE = (
    "KIS investor-trend-estimate is intraday provisional cumulative input, "
    "not a confirmed daily close figure."
)


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _slot_sort_key(row: dict[str, Any]) -> int:
    slot = str(row.get("slot") or "").strip()
    try:
        return int(slot)
    except ValueError:
        return -1


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    slot = str(row.get("bsop_hour_gb") or "").strip()
    return {
        "slot": slot or None,
        "as_of_time_kst": _SLOT_TIMES.get(slot),
        "foreign_net_qty": _to_int(row.get("frgn_fake_ntby_qty")),
        "institution_net_qty": _to_int(row.get("orgn_fake_ntby_qty")),
        "combined_net_qty": _to_int(row.get("sum_fake_ntby_qty")),
    }


def _as_of(slot_time: str | None) -> str | None:
    if slot_time is None:
        return None
    hour, minute = (int(part) for part in slot_time.split(":", maxsplit=1))
    dt = datetime.datetime.combine(
        now_kst().date(),
        datetime.time(hour=hour, minute=minute),
        tzinfo=KST,
    )
    return dt.isoformat()


async def handle_get_intraday_investor_flow(symbol: str) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Intraday investor flow is only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    try:
        raw_rows = await KISClient().investor_trend_estimate(symbol)
    except Exception as exc:
        return _error_payload(
            source="kis",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )

    rows = [_normalize_row(row) for row in raw_rows]
    rows.sort(key=_slot_sort_key)
    latest = rows[-1] if rows else None
    latest_time = latest.get("as_of_time_kst") if latest is not None else None

    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "source": "kis",
        "data_state": DATA_STATE_INTRADAY_PROVISIONAL,
        "market_session_state": kr_market_data_state(),
        "provisional": True,
        "as_of": _as_of(latest_time),
        "as_of_time_kst": latest_time,
        "foreign_net_qty": (
            latest.get("foreign_net_qty") if latest is not None else None
        ),
        "institution_net_qty": (
            latest.get("institution_net_qty") if latest is not None else None
        ),
        "combined_net_qty": (
            latest.get("combined_net_qty") if latest is not None else None
        ),
        "rows": rows,
        "note": (
            _PROVISIONAL_NOTE
            if rows
            else "No KIS provisional investor-flow rows were returned."
        ),
    }
