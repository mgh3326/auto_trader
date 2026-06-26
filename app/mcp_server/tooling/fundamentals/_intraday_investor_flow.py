"""Intraday provisional KR investor-flow MCP handler."""

from __future__ import annotations

import datetime
from typing import Any

from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.fundamentals._investor_flow_common import (
    build_confirmed_block,
)
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

# Latest publish slot (14:30). Past it, a stale full set is indistinguishable
# from a fresh one, so we refuse to claim `observed`.
_LAST_SLOT_TIME = max(
    datetime.time(int(_h), int(_m))
    for _h, _m in (t.split(":") for t in _SLOT_TIMES.values())
)

_PROVISIONAL_NOTE = (
    "KIS investor-trend-estimate is intraday provisional cumulative input, "
    "not a confirmed daily close figure."
)

_PRIOR_SESSION_NOTE = (
    " Rows likely belong to the previous trading session (the KIS payload "
    "carries no date field), so as_of is null."
)

_UNCONFIRMED_NOTE = (
    " Today's data could not be positively confirmed (rows may belong to the "
    "current OR a prior session); as_of is null. See `confirmed` for the most "
    "recent confirmed daily series."
)

# ROB-542: machine-readable confidence labels for the session attribution.
CONFIDENCE_OBSERVED = "observed"
CONFIDENCE_INFERRED = "inferred"
CONFIDENCE_CARRY_OVER = "carry_over"
CONFIDENCE_PROVISIONAL_UNCONFIRMED = "provisional_unconfirmed"

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
    *,
    now: datetime.datetime,
    market_state: str,
    last_confirmed_date: str | None,
) -> tuple[str | None, str | None, str | None, bool, bool]:
    """Deterministic session attribution for the latest KIS slot.

    Returns ``(as_of, as_of_date, confidence, is_prior_session,
    today_available)``. All time-dependent inputs are passed in (a single
    captured ``now``, the resolved ``market_state``, and the Naver-confirmed
    ``last_confirmed_date``), so the label is a pure function of its arguments —
    identical inputs always yield identical output, and a stale prior-session
    payload is never labeled ``observed``.

    Rules (first match wins):
      1. no rows → all null.
      2. not a session day → carry_over (prior-session leftover).
      3. latest slot in the future (incl. pre-open) → carry_over.
      4. not-future AND Naver already confirmed today → inferred.
      5. not-future, today unconfirmed, market fresh AND now < 14:30 → observed.
      6. otherwise (≥14:30 live full-set, or after-close unconfirmed) →
         provisional_unconfirmed (refuse to claim today).
    """
    if slot_time is None:
        return None, None, None, False, False

    today = now.date()
    hour, minute = (int(part) for part in slot_time.split(":", maxsplit=1))
    slot_dt = datetime.datetime.combine(
        today, datetime.time(hour=hour, minute=minute), tzinfo=KST
    )

    # Rule 2: weekend/holiday → rows belong to the prior session.
    if not is_kr_session_day(today):
        prior = previous_kr_session(today)
        return None, prior.isoformat(), CONFIDENCE_CARRY_OVER, True, False

    # Rule 3: future slot (a stale full set in the morning, or pre-open) cannot
    # be today's data.
    if slot_dt > now:
        prior = previous_kr_session(today)
        return None, prior.isoformat(), CONFIDENCE_CARRY_OVER, True, False

    today_iso = today.isoformat()

    # Rule 4: Naver already posted today's confirmed row → today, inferred.
    if last_confirmed_date == today_iso:
        return slot_dt.isoformat(), today_iso, CONFIDENCE_INFERRED, False, True

    # Rule 5: live session before the last slot → a stale full set would have
    # been caught as "future" above, so this is genuine-today.
    max_slot_dt = datetime.datetime.combine(today, _LAST_SLOT_TIME, tzinfo=KST)
    if market_state == DATA_STATE_FRESH and now < max_slot_dt:
        return slot_dt.isoformat(), today_iso, CONFIDENCE_OBSERVED, False, True

    # Rule 6: irreducibly ambiguous — refuse to claim today.
    return None, None, CONFIDENCE_PROVISIONAL_UNCONFIRMED, False, False


async def handle_get_intraday_investor_flow(symbol: str) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Intraday investor flow is only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    now = now_kst()  # single capture, threaded through all time logic

    try:
        raw_rows = await KISClient().investor_trend_estimate(symbol)
    except Exception as exc:
        return _error_payload(
            source="kis",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )

    # Best-effort confirmed-daily anchor + embed (Naver). Never fails the tool.
    confirmed_block, last_confirmed_date = await build_confirmed_block(symbol, days=5)

    market_state = kr_market_data_state(now)

    rows = [_normalize_row(row) for row in raw_rows]
    rows.sort(key=_slot_sort_key)
    latest = rows[-1] if rows else None
    latest_time = latest.get("as_of_time_kst") if latest is not None else None
    (
        as_of,
        as_of_date,
        confidence,
        is_prior_session,
        today_available,
    ) = _classify_session(
        latest_time,
        now=now,
        market_state=market_state,
        last_confirmed_date=last_confirmed_date,
    )

    # Always-populated floor: Naver-recent if available, else previous session.
    if last_confirmed_date is not None:
        last_confirmed_session_date = last_confirmed_date
    elif latest_time is not None:
        last_confirmed_session_date = previous_kr_session(now.date()).isoformat()
    else:
        last_confirmed_session_date = None

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
    elif confidence == CONFIDENCE_PROVISIONAL_UNCONFIRMED:
        note = _PROVISIONAL_NOTE + _UNCONFIRMED_NOTE
    else:
        note = _PROVISIONAL_NOTE

    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "source": "kis",
        "data_state": DATA_STATE_INTRADAY_PROVISIONAL,
        "market_session_state": market_state,
        "provisional": True,
        "as_of": as_of,
        "as_of_date": as_of_date,
        "confidence": confidence,
        "is_prior_session": is_prior_session,
        "today_available": today_available,
        "last_confirmed_session_date": last_confirmed_session_date,
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
        "confirmed": confirmed_block,
        "note": note,
    }
