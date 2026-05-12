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
    Prefer stock_code for symbol matching; corp_code stays in raw_payload_json.
    URL builds from rcept_no.
    """
    rcept_no = (row.get("rcept_no") or row.get("rcp_no") or "").strip()
    rcept_dt = (row.get("rcept_dt") or row.get("date") or "").strip()
    corp_name = (row.get("corp_name") or "").strip()
    report_nm = (row.get("report_nm") or "").strip()
    stock_code = (row.get("stock_code") or row.get("corp_cls_stock_code") or "").strip()
    symbol = stock_code if stock_code and stock_code.isdigit() else None

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
        "symbol": symbol,
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


_FF_IMPORTANCE_MAP = {"low": 1, "medium": 2, "high": 3}


def _strip_unit_and_decimal(value: Any) -> tuple[Decimal | None, str | None]:
    """Return (Decimal, unit) parsed from strings like '1.25%', '50K', '2.4'.

    Returns (None, None) for empty/None inputs.
    """
    if value is None:
        return None, None
    s = str(value).strip()
    if not s:
        return None, None
    unit: str | None = None
    if s.endswith("%"):
        unit = "%"
        s = s[:-1].strip()
    elif s.endswith(("K", "M", "B", "T")):
        unit = s[-1]
        s = s[:-1].strip()
    try:
        return Decimal(s), unit
    except Exception:
        return None, unit


def _row_to_jsonable(row: dict[str, Any]) -> dict[str, Any]:
    """Strip / stringify values inside `row` so the dict is JSONB-serializable.

    Datetimes -> ISO strings; date -> ISO string; Decimals -> str.
    """
    import datetime as _dt

    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, _dt.datetime):
            out[k] = v.isoformat()
        elif isinstance(v, _dt.date):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def normalize_forexfactory_event_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one ForexFactory event row into MarketEvent + values dicts.

    The row shape is produced by `app/services/market_events/forexfactory_helpers.py`.
    """
    title = (row.get("title") or "").strip()
    event_date = row.get("event_date")
    if not title or event_date is None:
        raise ValueError("forexfactory row missing title or event_date")

    impact = (row.get("impact") or "").strip().lower()
    importance = _FF_IMPORTANCE_MAP.get(impact)

    actual_raw = row.get("actual")
    forecast_raw = row.get("forecast")
    previous_raw = row.get("previous")
    status = "released" if actual_raw not in (None, "") else "scheduled"

    event = {
        "category": "economic",
        "market": "global",
        "country": row.get("country"),
        "currency": row.get("currency"),
        "symbol": None,
        "company_name": None,
        "title": title,
        "event_date": event_date,
        "release_time_utc": row.get("release_time_utc"),
        "release_time_local": row.get("release_time_local"),
        "source_timezone": "America/New_York",
        "time_hint": row.get("time_hint_raw") or "unknown",
        "importance": importance,
        "status": status,
        "source": "forexfactory",
        "source_event_id": row.get("source_event_id"),
        "source_url": None,
        "fiscal_year": None,
        "fiscal_quarter": None,
        "raw_payload_json": _row_to_jsonable(row),
    }

    actual_dec, actual_unit = _strip_unit_and_decimal(actual_raw)
    forecast_dec, forecast_unit = _strip_unit_and_decimal(forecast_raw)
    previous_dec, previous_unit = _strip_unit_and_decimal(previous_raw)
    unit = actual_unit or forecast_unit or previous_unit

    values: list[dict[str, Any]] = []
    if any(v is not None for v in (actual_dec, forecast_dec, previous_dec)):
        values.append(
            {
                "metric_name": "actual",
                "period": event_date.isoformat(),
                "actual": actual_dec,
                "forecast": forecast_dec,
                "previous": previous_dec,
                "unit": unit,
            }
        )

    return event, values


_WISEFN_TIME_HINT_ALLOWED = {"before_open", "after_close", "during_market", "unknown"}
_WISEFN_RELEASE_TYPE_TO_STATUS = {
    "scheduled": "scheduled",
    "released": "released",
    "revised": "revised",
    "cancelled": "cancelled",
    "tentative": "tentative",
}


def _wisefn_source_event_id(
    symbol: str,
    event_date: date,
    fiscal_year: Any,
    fiscal_quarter: Any,
) -> str:
    """Deterministic ID for idempotent upserts on (source, source_event_id)."""
    fy = "" if fiscal_year is None else str(fiscal_year)
    fq = "" if fiscal_quarter is None else str(fiscal_quarter)
    return f"wisefn::{symbol}::{event_date.isoformat()}::{fy}::{fq}"


def normalize_wisefn_earnings_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one WiseFn KR earnings calendar row to a MarketEvent dict.

    Required fields: stock_code (6-digit numeric), release_date (ISO date),
    corp_name. Optional: fiscal_year, fiscal_quarter, release_type, title,
    time_hint.

    No metric values are produced — WiseFn rows describe the schedule, not
    realized eps/revenue. (Realized values are a follow-up that would join
    DART quarterly filings.)
    """
    stock_code = (row.get("stock_code") or "").strip()
    if not stock_code or not stock_code.isdigit():
        raise ValueError(
            f"wisefn row missing/invalid stock_code (must be numeric): {row.get('stock_code')!r}"
        )

    raw_date = row.get("release_date") or row.get("date")
    if not raw_date:
        raise ValueError("wisefn row missing release_date")
    try:
        event_date = date.fromisoformat(str(raw_date))
    except ValueError as exc:
        raise ValueError(f"wisefn row release_date not ISO: {raw_date!r}") from exc

    corp_name = (row.get("corp_name") or "").strip() or None
    title = (row.get("title") or "").strip() or None
    fiscal_year = row.get("fiscal_year")
    fiscal_quarter = row.get("fiscal_quarter")

    raw_hint = (row.get("time_hint") or "").strip().lower()
    time_hint = raw_hint if raw_hint in _WISEFN_TIME_HINT_ALLOWED else "unknown"

    raw_status = (row.get("release_type") or "").strip().lower()
    status = _WISEFN_RELEASE_TYPE_TO_STATUS.get(raw_status, "scheduled")

    source_event_id = _wisefn_source_event_id(
        stock_code, event_date, fiscal_year, fiscal_quarter
    )

    event = {
        "category": "earnings",
        "market": "kr",
        "country": "KR",
        "symbol": stock_code,
        "company_name": corp_name,
        "title": title,
        "event_date": event_date,
        "release_time_utc": None,
        "release_time_local": None,
        "source_timezone": "Asia/Seoul",
        "time_hint": time_hint,
        "importance": None,
        "status": status,
        "source": "wisefn",
        "source_event_id": source_event_id,
        "source_url": None,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "raw_payload_json": _row_to_jsonable(row),
    }
    return event, []


_TV_IMPORTANCE_MAP: dict[int, int] = {1: 1, 2: 2, 3: 3}


def normalize_tradingview_event_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one TradingView economic-calendar row into MarketEvent + values dicts.

    The row shape is produced by
    `app.services.market_events.tradingview_helpers.fetch_tradingview_events_for_date`.
    Fields: id/title/country/date_utc/period/actual/forecast/previous/unit/
            source/source_url/ticker/importance/_raw
    """
    title = (row.get("title") or "").strip()
    date_utc = row.get("date_utc")
    if not title or date_utc is None:
        raise ValueError("tradingview row missing title or date_utc")

    event_date = date_utc.date()
    country = row.get("country")

    importance_raw = row.get("importance")
    importance: int | None = None
    if importance_raw is not None:
        try:
            importance = _TV_IMPORTANCE_MAP.get(int(importance_raw))
        except (ValueError, TypeError):
            importance = None

    event_id = row.get("id")
    if event_id:
        source_event_id = str(event_id)
    else:
        ts = date_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        source_event_id = f"tv::{country}::{title}::{ts}"

    actual_raw = row.get("actual")
    status = "released" if actual_raw not in (None, "", "-") else "scheduled"

    raw_payload_json: dict[str, Any] = dict(row.get("_raw") or row)
    raw_payload_json.pop("_raw", None)
    raw_payload_json.pop("date_utc", None)

    event = {
        "category": "economic",
        "market": "global",
        "country": country,
        "currency": None,
        "symbol": row.get("ticker") or None,
        "company_name": None,
        "title": title,
        "event_date": event_date,
        "release_time_utc": date_utc,
        "release_time_local": None,
        "source_timezone": "UTC",
        "time_hint": "unknown",
        "importance": importance,
        "status": status,
        "source": "tradingview",
        "source_event_id": source_event_id,
        "source_url": row.get("source_url"),
        "fiscal_year": None,
        "fiscal_quarter": None,
        "raw_payload_json": raw_payload_json,
    }

    period = row.get("period") or None
    unit = row.get("unit") or None
    actual_dec = _to_decimal(actual_raw)
    forecast_dec = _to_decimal(row.get("forecast"))
    previous_dec = _to_decimal(row.get("previous"))

    values: list[dict[str, Any]] = []
    if any(v is not None for v in (actual_dec, forecast_dec, previous_dec)):
        values.append(
            {
                "metric_name": "actual",
                "period": period,
                "actual": actual_dec,
                "forecast": forecast_dec,
                "previous": previous_dec,
                "unit": unit,
            }
        )

    return event, values
