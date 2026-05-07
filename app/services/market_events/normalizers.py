"""Pure-function normalizers from external source rows to MarketEvent dicts (ROB-128).

These functions never touch the database. They produce dicts shaped to be passed
to MarketEventsRepository.upsert_event_with_values.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

_FINNHUB_HOUR_TO_TIME_HINT = {
    "bmo": "before_open",
    "amc": "after_close",
    "dmh": "during_market",
    "dmt": "during_market",
}


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _classify_finnhub_status(eps_actual: Any, revenue_actual: Any) -> str:
    if eps_actual is not None or revenue_actual is not None:
        return "released"
    return "scheduled"


def normalize_finnhub_earnings_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one Finnhub `earningsCalendar` item.

    Returns (event_dict, [value_dict, ...]) ready for upsert.
    """
    symbol = (row.get("symbol") or "").strip().upper()
    raw_date = row.get("date")
    if not symbol or not raw_date:
        raise ValueError("finnhub row missing symbol or date")

    event_date = date.fromisoformat(raw_date)
    hour = (row.get("hour") or "").strip().lower()
    time_hint = _FINNHUB_HOUR_TO_TIME_HINT.get(hour, "unknown")
    eps_actual = row.get("eps_actual")
    revenue_actual = row.get("revenue_actual")

    event = {
        "category": "earnings",
        "market": "us",
        "country": "US",
        "symbol": symbol,
        "company_name": None,
        "title": f"{symbol} earnings release",
        "event_date": event_date,
        "release_time_utc": None,
        "release_time_local": None,
        "source_timezone": "America/New_York",
        "time_hint": time_hint,
        "importance": None,
        "status": _classify_finnhub_status(eps_actual, revenue_actual),
        "source": "finnhub",
        "source_event_id": None,
        "source_url": None,
        "fiscal_year": row.get("year"),
        "fiscal_quarter": row.get("quarter"),
        "raw_payload_json": dict(row),
    }

    period = None
    if event["fiscal_year"] is not None and event["fiscal_quarter"] is not None:
        period = f"Q{event['fiscal_quarter']}-{event['fiscal_year']}"

    values: list[dict[str, Any]] = []
    eps_forecast = row.get("eps_estimate")
    if eps_actual is not None or eps_forecast is not None:
        values.append(
            {
                "metric_name": "eps",
                "period": period,
                "actual": _to_decimal(eps_actual),
                "forecast": _to_decimal(eps_forecast),
                "unit": "USD",
            }
        )
    rev_forecast = row.get("revenue_estimate")
    if revenue_actual is not None or rev_forecast is not None:
        values.append(
            {
                "metric_name": "revenue",
                "period": period,
                "actual": _to_decimal(revenue_actual),
                "forecast": _to_decimal(rev_forecast),
                "unit": "USD",
            }
        )

    return event, values


_DART_EARNINGS_KEYWORDS = (
    "분기보고서",
    "반기보고서",
    "사업보고서",
    "영업실적",
    "잠정실적",
    "매출액또는손익구조",
    "영업손실",
    "영업이익",
    "실적",
    "전망",
)


def classify_dart_category(report_nm: str) -> str:
    """Map a DART report_nm string to our category taxonomy."""
    if not report_nm:
        return "disclosure"
    for kw in _DART_EARNINGS_KEYWORDS:
        if kw in report_nm:
            return "earnings"
    return "disclosure"


def normalize_dart_disclosure_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one DART `list_date` row to a MarketEvent.

    DART rows expose at minimum: rcept_no, rcept_dt, corp_name, corp_code, report_nm.
    URL builds from rcept_no.
    """
    rcept_no = (row.get("rcept_no") or row.get("rcp_no") or "").strip()
    rcept_dt = (row.get("rcept_dt") or row.get("date") or "").strip()
    corp_name = (row.get("corp_name") or "").strip()
    report_nm = (row.get("report_nm") or "").strip()
    corp_code = (row.get("corp_code") or "").strip() or None

    if not rcept_no or not rcept_dt:
        raise ValueError("dart row missing rcept_no or rcept_dt")

    if len(rcept_dt) >= 8 and rcept_dt[:8].isdigit():
        event_date = date(int(rcept_dt[:4]), int(rcept_dt[4:6]), int(rcept_dt[6:8]))
    else:
        event_date = date.fromisoformat(rcept_dt)

    category = classify_dart_category(report_nm)

    event = {
        "category": category,
        "market": "kr",
        "country": "KR",
        "symbol": corp_code,
        "company_name": corp_name,
        "title": report_nm or None,
        "event_date": event_date,
        "release_time_utc": None,
        "release_time_local": None,
        "source_timezone": "Asia/Seoul",
        "time_hint": "unknown",
        "importance": None,
        "status": "released",
        "source": "dart",
        "source_event_id": rcept_no,
        "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
        "fiscal_year": None,
        "fiscal_quarter": None,
        "raw_payload_json": dict(row),
    }
    return event, []
