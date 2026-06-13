"""Intraday provisional KR investor-flow MCP handler."""

from __future__ import annotations

import datetime
from typing import Any

from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    is_kr_session_day,
    kr_market_data_state,
    previous_kr_session,
)
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

_PRIOR_SESSION_NOTE = (
    " Rows likely belong to the previous trading session (the KIS payload "
    "carries no date field), so as_of is null."
)

# ROB-542: machine-readable confidence labels for the session attribution.
CONFIDENCE_OBSERVED = "observed"
CONFIDENCE_INFERRED = "inferred"
CONFIDENCE_CARRY_OVER = "carry_over"

_CARRY_OVER_WARNING_CODE = "prior_session_carry_over"
_CARRY_OVER_WARNING_MESSAGE = (
    "KIS investor-trend-estimate rows carry no date and appear to belong to a "
    "previous trading session (future slot or non-session day). as_of is null; "
    "as_of_date is the previous XKRX session DATE only, not a stamped time."
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


def _classify_session(
    slot_time: str | None,
) -> tuple[str | None, str | None, str | None, bool]:
    """Classify the latest KIS slot's session attribution.

    The KIS payload carries no date field, and outside trading hours KIS keeps
    serving the prior session's rows. Returns
    ``(as_of_datetime_or_null, as_of_date, confidence, is_prior_session)``:

    - ``observed`` — KRX regular session is live (``kr_market_data_state ==
      "fresh"``) and the slot is not in the future on a session day. ``as_of``
      is the today-stamped slot datetime; ``as_of_date`` is today.
    - ``inferred`` — after the close on a session day (slot not in the future,
      session day, but state not "fresh"). The same-date stamp is correct, but
      flagged as inferred because the payload itself does not date the rows.
    - ``carry_over`` — future slot or non-session day. The rows almost certainly
      belong to a previous session, so ``as_of`` (the precise datetime) is
      **null** and ``as_of_date`` is the previous XKRX session DATE only — never
      a fabricated prior-day timestamp from the ``_SLOT_TIMES`` map (the spec
      notes the real slot boundaries may vary). ``is_prior_session`` is True.

    When ``slot_time`` is None (no rows) there is nothing to classify, so the
    function returns ``(None, None, None, False)``.
    """
    if slot_time is None:
        return None, None, None, False

    now = now_kst()
    hour, minute = (int(part) for part in slot_time.split(":", maxsplit=1))
    dt = datetime.datetime.combine(
        now.date(),
        datetime.time(hour=hour, minute=minute),
        tzinfo=KST,
    )

    slot_in_future = dt > now
    session_day = is_kr_session_day(now.date())

    if slot_in_future or not session_day:
        # Carry-over: do not stamp a precise prior-day time. Date-only.
        prior = previous_kr_session(now.date())
        return None, prior.isoformat(), CONFIDENCE_CARRY_OVER, True

    # Same session day, slot already elapsed → today's date is the honest stamp.
    as_of_date = now.date().isoformat()
    if kr_market_data_state() == DATA_STATE_FRESH:
        return dt.isoformat(), as_of_date, CONFIDENCE_OBSERVED, False
    return dt.isoformat(), as_of_date, CONFIDENCE_INFERRED, False


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
    as_of, as_of_date, confidence, is_prior_session = _classify_session(latest_time)

    warning = (
        {
            "code": _CARRY_OVER_WARNING_CODE,
            "message": _CARRY_OVER_WARNING_MESSAGE,
        }
        if is_prior_session
        else None
    )

    if not rows:
        note = "No KIS provisional investor-flow rows were returned."
    elif is_prior_session:
        note = _PROVISIONAL_NOTE + _PRIOR_SESSION_NOTE
    else:
        note = _PROVISIONAL_NOTE

    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "source": "kis",
        "data_state": DATA_STATE_INTRADAY_PROVISIONAL,
        "market_session_state": kr_market_data_state(),
        "provisional": True,
        "as_of": as_of,
        "as_of_date": as_of_date,
        "confidence": confidence,
        "is_prior_session": is_prior_session,
        "warning": warning,
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
        "note": note,
    }
