"""Pure normalizers from Benzinga calendar rows to market_events dicts (hk 6513).

No database access, no clock read: the caller passes ``first_received_at`` (our
clock at fetch time) and ``fetched_at`` so the same payload always normalizes
to the same dicts.

Storage rules these functions implement:

* ``source = "benzinga"``, ``market = "us"`` for every row (economics rows whose
  country is not the US are skipped, never coerced).
* The provider's own timestamps are kept twice: as given (``*_raw``) and as
  aware UTC (``provider_*_at``). Unparseable or missing values stay ``None``.
* ``first_received_at`` is our first observation and is never rewritten by a
  later ingest of the same record (the repository leaves it out of updates).
* ``raw_payload_json`` is bounded (long strings truncated) and redacted by the
  repository, so replacement comparisons keep the provider evidence without
  keeping an unbounded blob.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.core.symbol import to_db_symbol
from app.services.benzinga_http import parse_provider_time, parse_unix_timestamp

MAX_RAW_STRING = 2000
TRUNCATION_MARKER = "…(truncated)"
SOURCE = "benzinga"
MARKET = "us"

_EASTERN = ZoneInfo("America/New_York")

_EARNINGS_TIME_HINT = {
    "bmo": "before_open",
    "amc": "after_close",
    "dmh": "during_market",
    "dmt": "during_market",
}

_US_COUNTRY_TOKENS = frozenset({"USA", "US", "UNITED STATES"})


@dataclass(frozen=True)
class NormalizedRow:
    event: dict[str, Any]
    values: list[dict[str, Any]]


# -- small conversions ------------------------------------------------------


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _row_symbol(row: dict[str, Any], *, key: str = "ticker") -> str | None:
    raw = _text(row.get(key))
    if raw is None:
        return None
    return to_db_symbol(raw).upper()


def _parse_iso_date(value: Any) -> date | None:
    raw = _text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _json_safe(value: Any) -> Any:
    """Bound and JSONB-sanitize a provider payload (pure, recursive)."""
    if isinstance(value, str):
        if len(value) > MAX_RAW_STRING:
            return value[:MAX_RAW_STRING] + TRUNCATION_MARKER
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def content_hash(row: dict[str, Any]) -> str:
    """Canonical hash of one provider record.

    Two fetches of the same record hash equal (so a re-ingest writes nothing);
    a revised record (new ``updated`` or changed values) hashes differently
    (so it becomes a new revision).
    """
    canonical = json.dumps(
        _json_safe(row), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _provider_times(row: dict[str, Any], *, unix_updated: bool) -> dict[str, Any]:
    created_raw = _text(row.get("created"))
    updated_raw = _text(row.get("updated"))
    created_at: datetime | None = None
    updated_at: datetime | None = None
    if unix_updated:
        updated_at = parse_unix_timestamp(row.get("updated"))
        if updated_at is not None:
            updated_raw = updated_raw or str(row.get("updated"))
    else:
        created_at = parse_provider_time(row.get("created"))
        updated_at = parse_provider_time(row.get("updated"))
    return {
        "provider_created_at": created_at,
        "provider_updated_at": updated_at,
        "provider_created_raw": created_raw,
        "provider_updated_raw": updated_raw,
    }


def _base_event(
    *,
    category: str,
    row: dict[str, Any],
    source_event_id: str,
    event_date: date,
    symbol: str | None,
    company_name: str | None,
    title: str | None,
    status: str,
    time_hint: str = "unknown",
    importance: int | None = None,
    fiscal_year: int | None = None,
    fiscal_quarter: int | None = None,
    currency: str | None = None,
    country: str = "US",
    source_url: str | None = None,
    first_received_at: datetime,
    fetched_at: datetime,
    unix_updated: bool = True,
) -> dict[str, Any]:
    return {
        "category": category,
        "market": MARKET,
        "country": country,
        "currency": currency,
        "symbol": symbol,
        "company_name": company_name,
        "title": title,
        "event_date": event_date,
        "release_time_utc": None,
        "release_time_local": None,
        "source_timezone": "UTC",
        "time_hint": time_hint,
        "importance": importance,
        "status": status,
        "source": SOURCE,
        "source_event_id": source_event_id,
        "source_url": source_url,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "raw_payload_json": _json_safe(row),
        "fetched_at": fetched_at,
        "first_received_at": first_received_at,
        **_provider_times(row, unix_updated=unix_updated),
    }


def _event_id(category: str, row: dict[str, Any], fallback: str) -> str:
    raw_id = _text(str(row.get("id"))) if row.get("id") is not None else None
    return f"benzinga:{category}:{raw_id or fallback}"


def _period(year: Any, period: Any) -> tuple[int | None, int | None, str | None]:
    fiscal_year = _int(year)
    token = (_text(period) or "").upper()
    fiscal_quarter: int | None = None
    if len(token) == 2 and token.startswith("Q") and token[1].isdigit():
        candidate = int(token[1])
        if 1 <= candidate <= 4:
            fiscal_quarter = candidate
    label = None
    if token:
        label = f"{token}-{fiscal_year}" if fiscal_year is not None else token
    return fiscal_year, fiscal_quarter, label


def _normalize_earnings(
    row: dict[str, Any], *, first_received_at: datetime, fetched_at: datetime
) -> NormalizedRow | None:
    event_date = _parse_iso_date(row.get("date"))
    if event_date is None:
        raise ValueError("benzinga earnings row missing a valid date")
    symbol = _row_symbol(row)
    if symbol is None:
        raise ValueError("benzinga earnings row missing ticker")

    currency = _text(row.get("currency")) or "USD"
    fiscal_year, fiscal_quarter, period = _period(
        row.get("period_year"), row.get("period")
    )

    eps_actual = _decimal(row.get("eps"))
    eps_estimate = _decimal(row.get("eps_est"))
    revenue_actual = _decimal(row.get("revenue"))
    revenue_estimate = _decimal(row.get("revenue_est"))

    if eps_actual is not None or revenue_actual is not None:
        status = "released"
    elif _int(row.get("date_confirmed")) == 0:
        status = "tentative"
    else:
        status = "scheduled"

    time_hint = _EARNINGS_TIME_HINT.get(
        (_text(row.get("time")) or "").lower(), "unknown"
    )

    event = _base_event(
        category="earnings",
        row=row,
        source_event_id=_event_id("earnings", row, f"{symbol}:{event_date}"),
        event_date=event_date,
        symbol=symbol,
        company_name=_text(row.get("name")),
        title=f"{symbol} earnings release",
        status=status,
        time_hint=time_hint,
        importance=_int(row.get("importance")),
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        currency=currency,
        first_received_at=first_received_at,
        fetched_at=fetched_at,
    )

    values: list[dict[str, Any]] = []
    if any(
        v is not None
        for v in (eps_actual, eps_estimate, _decimal(row.get("eps_prior")))
    ):
        values.append(
            {
                "metric_name": "eps",
                "period": period,
                "actual": eps_actual,
                "forecast": eps_estimate,
                "previous": _decimal(row.get("eps_prior")),
                "surprise": _decimal(row.get("eps_surprise")),
                "surprise_pct": _decimal(row.get("eps_surprise_percent")),
                "unit": currency,
            }
        )
    if any(
        v is not None
        for v in (revenue_actual, revenue_estimate, _decimal(row.get("revenue_prior")))
    ):
        values.append(
            {
                "metric_name": "revenue",
                "period": period,
                "actual": revenue_actual,
                "forecast": revenue_estimate,
                "previous": _decimal(row.get("revenue_prior")),
                "surprise": _decimal(row.get("revenue_surprise")),
                "surprise_pct": _decimal(row.get("revenue_surprise_percent")),
                "unit": currency,
            }
        )
    return NormalizedRow(event=event, values=values)


def _normalize_rating(
    row: dict[str, Any], *, first_received_at: datetime, fetched_at: datetime
) -> NormalizedRow | None:
    event_date = _parse_iso_date(row.get("date"))
    if event_date is None:
        raise ValueError("benzinga ratings row missing a valid date")
    symbol = _row_symbol(row)
    if symbol is None:
        raise ValueError("benzinga ratings row missing ticker")

    firm = _text(row.get("analyst"))
    action = _text(row.get("action_company"))
    rating = _text(row.get("rating_current"))
    title_parts = [part for part in (firm, action, rating) if part]
    title = f"{symbol} " + " ".join(title_parts) if title_parts else f"{symbol} rating"

    currency = _text(row.get("currency")) or "USD"
    event = _base_event(
        category="analyst_rating",
        row=row,
        source_event_id=_event_id("analyst_rating", row, f"{symbol}:{event_date}"),
        event_date=event_date,
        symbol=symbol,
        company_name=_text(row.get("name")),
        title=title,
        status="released",
        importance=_int(row.get("importance")),
        currency=currency,
        source_url=_text(row.get("url")),
        first_received_at=first_received_at,
        fetched_at=fetched_at,
    )

    values: list[dict[str, Any]] = []
    pt_current = _decimal(row.get("pt_current"))
    pt_prior = _decimal(row.get("pt_prior"))
    if pt_current is not None or pt_prior is not None:
        values.append(
            {
                "metric_name": "price_target",
                "period": None,
                "actual": pt_current,
                "previous": pt_prior,
                "surprise_pct": _decimal(row.get("pt_pct_change")),
                "unit": currency,
            }
        )
    return NormalizedRow(event=event, values=values)


def _normalize_guidance(
    row: dict[str, Any], *, first_received_at: datetime, fetched_at: datetime
) -> NormalizedRow | None:
    event_date = _parse_iso_date(row.get("date"))
    if event_date is None:
        raise ValueError("benzinga guidance row missing a valid date")
    symbol = _row_symbol(row)
    if symbol is None:
        raise ValueError("benzinga guidance row missing ticker")

    currency = _text(row.get("currency")) or "USD"
    fiscal_year, fiscal_quarter, period = _period(
        row.get("period_year"), row.get("period")
    )
    event = _base_event(
        category="guidance",
        row=row,
        source_event_id=_event_id("guidance", row, f"{symbol}:{event_date}"),
        event_date=event_date,
        symbol=symbol,
        company_name=_text(row.get("name")),
        title=f"{symbol} guidance",
        status="released",
        importance=_int(row.get("importance")),
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        currency=currency,
        first_received_at=first_received_at,
        fetched_at=fetched_at,
    )

    values: list[dict[str, Any]] = []
    for metric, estimate_key, max_key, min_key, prior_max, prior_min in (
        (
            "eps_guidance",
            "eps_guidance_est",
            "eps_guidance_max",
            "eps_guidance_min",
            "eps_guidance_prior_max",
            "eps_guidance_prior_min",
        ),
        (
            "revenue_guidance",
            "revenue_guidance_est",
            "revenue_guidance_max",
            "revenue_guidance_min",
            "revenue_guidance_prior_max",
            "revenue_guidance_prior_min",
        ),
    ):
        if all(
            _decimal(row.get(key)) is None for key in (estimate_key, max_key, min_key)
        ):
            continue
        values.append(
            {
                "metric_name": f"{metric}_min",
                "period": period,
                "forecast": _decimal(row.get(min_key)),
                "previous": _decimal(row.get(prior_min)),
                "unit": currency,
            }
        )
        values.append(
            {
                "metric_name": f"{metric}_max",
                "period": period,
                "forecast": _decimal(row.get(max_key)),
                "previous": _decimal(row.get(prior_max)),
                "unit": currency,
            }
        )
        values.append(
            {
                "metric_name": f"{metric}_est",
                "period": period,
                "forecast": _decimal(row.get(estimate_key)),
                "unit": currency,
            }
        )
    return NormalizedRow(event=event, values=values)


def _normalize_dividend(
    row: dict[str, Any], *, first_received_at: datetime, fetched_at: datetime
) -> NormalizedRow | None:
    event_date = _parse_iso_date(row.get("date"))
    if event_date is None:
        raise ValueError("benzinga dividends row missing a valid date")
    symbol = _row_symbol(row)
    if symbol is None:
        raise ValueError("benzinga dividends row missing ticker")

    currency = _text(row.get("currency")) or "USD"
    confirmed = row.get("confirmed") is True
    dividend_type = _text(row.get("dividend_type"))
    title = f"{symbol} dividend" + (f" ({dividend_type})" if dividend_type else "")
    event = _base_event(
        category="dividend",
        row=row,
        source_event_id=_event_id("dividend", row, f"{symbol}:{event_date}"),
        event_date=event_date,
        symbol=symbol,
        company_name=_text(row.get("name")),
        title=title,
        status="released" if confirmed else "scheduled",
        importance=_int(row.get("importance")),
        currency=currency,
        first_received_at=first_received_at,
        fetched_at=fetched_at,
    )

    amount = _decimal(row.get("dividend"))
    values: list[dict[str, Any]] = []
    if amount is not None:
        values.append(
            {
                "metric_name": "dividend",
                "period": _text(row.get("period")),
                "forecast": amount,
                "previous": _decimal(row.get("dividend_prior")),
                "unit": currency,
            }
        )
    return NormalizedRow(event=event, values=values)


def _normalize_conference(
    row: dict[str, Any], *, first_received_at: datetime, fetched_at: datetime
) -> NormalizedRow | None:
    event_date = _parse_iso_date(row.get("date"))
    if event_date is None:
        raise ValueError("benzinga conference-calls row missing a valid date")
    symbol = _row_symbol(row)
    if symbol is None:
        raise ValueError("benzinga conference-calls row missing ticker")

    event = _base_event(
        category="conference",
        row=row,
        source_event_id=_event_id("conference", row, f"{symbol}:{event_date}"),
        event_date=event_date,
        symbol=symbol,
        company_name=_text(row.get("name")),
        title=f"{symbol} conference call",
        status="scheduled",
        importance=_int(row.get("importance")),
        source_url=_text(row.get("webcast_url")),
        first_received_at=first_received_at,
        fetched_at=fetched_at,
    )
    return NormalizedRow(event=event, values=[])


def _normalize_economics(
    row: dict[str, Any], *, first_received_at: datetime, fetched_at: datetime
) -> NormalizedRow | None:
    country = (_text(row.get("country")) or "").upper()
    if country not in _US_COUNTRY_TOKENS:
        # Economics is ingested for the US market only; a non-US row is not
        # coerced into it.
        return None
    event_date = _parse_iso_date(row.get("date"))
    if event_date is None:
        raise ValueError("benzinga economics row missing a valid date")
    title = _text(row.get("event_name"))
    if title is None:
        raise ValueError("benzinga economics row missing event_name")

    event = _base_event(
        category="economic",
        row=row,
        source_event_id=_event_id("economic", row, f"{title}:{event_date}"),
        event_date=event_date,
        symbol=None,
        company_name=None,
        title=title,
        status="released" if _decimal(row.get("actual")) is not None else "scheduled",
        importance=_int(row.get("importance")),
        country="US",
        first_received_at=first_received_at,
        fetched_at=fetched_at,
    )

    actual = _decimal(row.get("actual"))
    consensus = _decimal(row.get("consensus"))
    prior = _decimal(row.get("prior"))
    unit = (
        _text(row.get("actual_t"))
        or _text(row.get("consensus_t"))
        or _text(row.get("prior_t"))
    )
    values: list[dict[str, Any]] = []
    if any(value is not None for value in (actual, consensus, prior)):
        values.append(
            {
                "metric_name": "actual",
                "period": _text(row.get("event_period")) or event_date.isoformat(),
                "actual": actual,
                "forecast": consensus,
                "previous": prior,
                "unit": unit,
            }
        )
    return NormalizedRow(event=event, values=values)


def _normalize_short_interest(
    row: dict[str, Any], *, first_received_at: datetime, fetched_at: datetime
) -> NormalizedRow | None:
    event_date = _parse_iso_date(row.get("recordDate"))
    if event_date is None:
        raise ValueError("benzinga short-interest row missing a valid recordDate")
    symbol = _row_symbol(row, key="symbol")
    if symbol is None:
        raise ValueError("benzinga short-interest row missing symbol")

    event = _base_event(
        category="short_interest",
        row=row,
        source_event_id=_event_id(
            "short_interest", row, f"{symbol}:{event_date.isoformat()}"
        ),
        event_date=event_date,
        symbol=symbol,
        company_name=_text(row.get("company")),
        title=f"{symbol} short interest",
        status="released",
        first_received_at=first_received_at,
        fetched_at=fetched_at,
    )

    values: list[dict[str, Any]] = []
    for metric, key, unit in (
        ("short_interest", "totalShortInterest", "shares"),
        ("days_to_cover", "daysToCover", None),
        ("short_percent_of_float", "shortPercentOfFloat", "%"),
    ):
        number = _decimal(row.get(key))
        if number is None:
            continue
        values.append(
            {
                "metric_name": metric,
                "period": event_date.isoformat(),
                "actual": number,
                "unit": unit,
            }
        )
    prior = _decimal(row.get("shortPriorMo"))
    if prior is not None:
        values.append(
            {
                "metric_name": "short_interest_prior_month",
                "period": event_date.isoformat(),
                "previous": prior,
                "unit": "shares",
            }
        )
    return NormalizedRow(event=event, values=values)


def _first_stock(row: dict[str, Any]) -> str | None:
    stocks = row.get("stocks")
    if not isinstance(stocks, list):
        return None
    for stock in stocks:
        if isinstance(stock, dict):
            name = _text(stock.get("name"))
        else:
            name = _text(stock)
        if name:
            return to_db_symbol(name).upper()
    return None


def _normalize_wiim(
    row: dict[str, Any], *, first_received_at: datetime, fetched_at: datetime
) -> NormalizedRow | None:
    times = _provider_times(row, unix_updated=False)
    observed = times["provider_created_at"] or times["provider_updated_at"]
    if observed is None:
        raise ValueError("benzinga wiim row missing a parseable created/updated")
    # WIIM items are ET-timestamped (``-0400``/``-0500``), and after-hours
    # moving news is exactly this population. Bucketing by the UTC date would
    # put anything created after 20:00 ET on the next day's partition, where a
    # ``--date`` run drops it as out of window.
    event_date = observed.astimezone(_EASTERN).date()
    title = _text(row.get("title"))
    if title is None:
        raise ValueError("benzinga wiim row missing title")

    event = _base_event(
        category="wiim",
        row=row,
        source_event_id=_event_id("wiim", row, f"{title[:40]}:{event_date}"),
        event_date=event_date,
        symbol=_first_stock(row),
        company_name=None,
        title=title,
        status="released",
        source_url=_text(row.get("url")),
        first_received_at=first_received_at,
        fetched_at=fetched_at,
        unix_updated=False,
    )
    return NormalizedRow(event=event, values=[])


_NORMALIZERS = {
    "earnings": _normalize_earnings,
    "analyst_rating": _normalize_rating,
    "guidance": _normalize_guidance,
    "dividend": _normalize_dividend,
    "economic": _normalize_economics,
    "conference": _normalize_conference,
    "short_interest": _normalize_short_interest,
    "wiim": _normalize_wiim,
}


def normalize_row(
    category: str,
    row: dict[str, Any],
    *,
    first_received_at: datetime,
    fetched_at: datetime,
) -> NormalizedRow | None:
    """Normalize one provider record.

    Returns ``None`` when the record is legitimately outside the ingested
    scope (a non-US economics row). Raises ``ValueError`` when the record is
    malformed — the caller counts and reports those instead of writing a
    partial row.
    """
    try:
        normalizer = _NORMALIZERS[category]
    except KeyError:
        raise ValueError(f"unknown benzinga category: {category!r}") from None
    return normalizer(row, first_received_at=first_received_at, fetched_at=fetched_at)


__all__ = [
    "MAX_RAW_STRING",
    "MARKET",
    "NormalizedRow",
    "SOURCE",
    "content_hash",
    "normalize_row",
]
