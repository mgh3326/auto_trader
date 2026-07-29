"""Append-only, GET-only US regular-session market-data evidence capture.

This module deliberately has no database or broker-execution dependencies.  It is
an operator-facing observation tool, not a trading surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from app.core.symbol import to_kis_symbol, to_yahoo_symbol

DEFAULT_SYMBOLS = ("AAPL", "IBM", "SPY")
_SENSITIVE = re.compile(
    r"(authorization|cookie|token|secret|api[-_]?key|password)", re.I
)
_VALUE_SECRET = re.compile(
    r"(?i)(bearer\s+)[^\s,;]+|((?:token|secret|api[-_]?key|password)=)[^&\s,;]+"
)
_NEW_YORK = ZoneInfo("America/New_York")
_SEOUL = ZoneInfo("Asia/Seoul")


class AsyncGetClient(Protocol):
    async def get(self, url: str, **kwargs: Any) -> httpx.Response: ...


@dataclass(frozen=True)
class CaptureRequest:
    provider: str
    product: str
    endpoint: str
    version: str
    symbol: str
    feed: str | None
    params: dict[str, str]
    headers: dict[str, str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_headers(headers: Any) -> dict[str, str]:
    return {
        str(key): "[REDACTED]"
        if _SENSITIVE.search(str(key))
        else _VALUE_SECRET.sub(r"\1[REDACTED]", str(value))
        for key, value in headers.items()
        if not _SENSITIVE.search(str(key))
    }


def _sanitize_body(raw: bytes) -> tuple[str, str]:
    """Return a secret-free textual body and its encoding label."""
    text = raw.decode("utf-8", errors="replace")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return _VALUE_SECRET.sub(r"\1[REDACTED]", text), "utf-8"

    def scrub(value: Any, key: str = "") -> Any:
        if _SENSITIVE.search(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            return _VALUE_SECRET.sub(r"\1[REDACTED]", value)
        return value

    return json.dumps(scrub(decoded), ensure_ascii=False, sort_keys=True), "json-utf-8"


def _classification(status: int | None, body: str, *, alpaca_sip: bool = False) -> str:
    lower = body.lower()
    if status is None:
        return "error"
    if status in {401}:
        return "auth"
    if status == 403:
        if alpaca_sip and any(
            token in lower
            for token in ("sip", "entitlement", "subscription", "not authorized")
        ):
            return "unavailable"
        return "auth"
    if not 200 <= status < 300:
        return "error"
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return "success" if body.strip() else "empty"
    if parsed in ({}, [], None) or (
        isinstance(parsed, dict) and not any(parsed.values())
    ):
        return "empty"
    return "success"


def _provider_timestamp(payload: str) -> Any:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        candidates = [data]
        for wrapper in ("quote", "bar", "chart", "output", "output1", "output2"):
            nested = data.get(wrapper)
            if isinstance(nested, dict):
                candidates.append(nested)
            elif isinstance(nested, list) and nested and isinstance(nested[0], dict):
                candidates.append(nested[0])
        found: dict[str, Any] = {}
        for candidate in candidates:
            found.update(
                {
                    key: candidate[key]
                    for key in ("timestamp", "t", "xymd", "xhms")
                    if key in candidate
                }
            )
        return found or None
    return None


def _u06_fields(payload: str, received_at: datetime) -> dict[str, Any] | None:
    try:
        quote = json.loads(payload).get("quote", {})
        nbb = quote.get("bp")
        nbb_number = float(nbb) if nbb is not None else None
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return {
        "sip_nbb": nbb_number,
        "hypothetical_limit_price": nbb_number * 0.998
        if nbb_number is not None
        else None,
        "participant_or_source_timestamp_raw": quote.get("t"),
        "local_receive_timestamp_utc": _iso(received_at),
        "quote_age_inputs": {
            "participant_or_source_timestamp_raw": quote.get("t"),
            "local_receive_timestamp_utc": _iso(received_at),
        },
        "statement": "Observation only; no submission, preview, automatic reference selection, or marketability assertion.",
    }


def _write_append_only(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as artifact:
        artifact.write(data)


async def _attempt(
    client: AsyncGetClient,
    request: CaptureRequest,
    *,
    artifact_dir: Path,
    session_label: str,
    u06_shadow: bool,
) -> Path:
    started = _utc_now()
    status: int | None = None
    response_headers: dict[str, str] = {}
    raw = b""
    transport_error: str | None = None
    try:
        response = await client.get(
            request.endpoint,
            params=request.params,
            headers=request.headers,
            timeout=15.0,
        )
        status, response_headers, raw = (
            response.status_code,
            _safe_headers(response.headers),
            response.content,
        )
    except httpx.HTTPError as exc:
        transport_error = f"{type(exc).__name__}: {str(exc)}"
    received = _utc_now()
    body, encoding = _sanitize_body(raw)
    body_bytes = body.encode("utf-8")
    outcome = _classification(
        status, body, alpaca_sip=request.provider == "alpaca" and request.feed == "sip"
    )
    record: dict[str, Any] = {
        "schema_version": "rob1161.raw-capture.v1",
        "provider": request.provider,
        "product": request.product,
        "endpoint": request.endpoint,
        "version": request.version or "UNKNOWN",
        "request_params": request.params,
        "response_status": status,
        "response_headers": response_headers,
        "provider_timestamp_raw": _provider_timestamp(body),
        "local_request_started_utc": _iso(started),
        "local_receive_completed_utc": _iso(received),
        "symbol": request.symbol,
        "feed": request.feed,
        "session_label": session_label,
        "outcome": outcome,
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "body_encoding": encoding,
        "body": body,
        "transport_error": transport_error,
    }
    if (
        u06_shadow
        and request.provider == "alpaca"
        and request.product == "latest_quote"
    ):
        record["u06_shadow"] = _u06_fields(body, received)
    if request.provider == "alpaca" and request.product == "historical_1m_bars":
        record["historical_reread"] = {
            "window_label": "09:30-09:36 America/New_York",
            "statement": "Historical reread only; it does not establish first appearance or decision-time availability.",
        }
    filename = (
        f"{request.provider}-{request.product}-{request.symbol}-{uuid.uuid4().hex}.json"
    )
    target = artifact_dir / filename
    _write_append_only(
        target,
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True).encode()
        + b"\n",
    )
    return target


def _requests_for(symbol: str, historical_sip_date: date) -> list[CaptureRequest]:
    alpaca_headers = {
        "APCA-API-KEY-ID": os.getenv("ALPACA_PAPER_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_PAPER_API_SECRET", ""),
    }
    kis_headers = {
        "appkey": os.getenv("KIS_APP_KEY", ""),
        "appsecret": os.getenv("KIS_APP_SECRET", ""),
        "authorization": f"Bearer {os.getenv('KIS_ACCESS_TOKEN', '')}",
        "tr_id": "HHDFS76950200",
    }
    kis_params = {
        "AUTH": "",
        "EXCD": "NAS",
        "SYMB": to_kis_symbol(symbol),
        "NMIN": "1",
        "PINC": "1",
        "NEXT": "",
        "NREC": "1",
        "FILL": "",
        "KEYB": "",
    }
    return [
        CaptureRequest(
            "alpaca",
            "latest_quote",
            f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest",
            "v2",
            symbol,
            "sip",
            {"feed": "sip"},
            alpaca_headers,
        ),
        CaptureRequest(
            "alpaca",
            "latest_bar",
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars/latest",
            "v2",
            symbol,
            "sip",
            {"feed": "sip"},
            alpaca_headers,
        ),
        CaptureRequest(
            "alpaca",
            "historical_1m_bars",
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
            "v2",
            symbol,
            "sip",
            historical_sip_opening_window_params(historical_sip_date),
            alpaca_headers,
        ),
        CaptureRequest(
            "kis",
            "overseas_1m",
            "https://openapi.koreainvestment.com:9443/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
            "HHDFS76950200",
            symbol,
            None,
            kis_params,
            kis_headers,
        ),
        CaptureRequest(
            "yahoo",
            "chart_1m",
            f"https://query1.finance.yahoo.com/v8/finance/chart/{to_yahoo_symbol(symbol)}",
            "v8",
            symbol,
            None,
            {"interval": "1m", "range": "1d"},
            {},
        ),
    ]


def default_session_label(now: datetime | None = None) -> str:
    current = (now or _utc_now()).astimezone(_NEW_YORK)
    return (
        "us_regular_session"
        if (current.hour, current.minute) >= (9, 30)
        and (current.hour, current.minute) < (16, 0)
        else "outside_us_regular_session"
    )


def kst_window_description() -> str:
    # ZoneInfo conversion intentionally avoids a hard-coded UTC offset across DST.
    date = datetime(2026, 7, 30, 4, 54, 50, tzinfo=_SEOUL)
    return _iso(date)


def historical_sip_opening_window_params(trading_date: date) -> dict[str, str]:
    """Build the 09:30–09:36 ET retrospective window without a fixed UTC offset.

    ``end`` is 09:37 so a provider with an end-exclusive bar range can return
    the seven 1-minute bars labelled 09:30 through 09:36.  This is a historical
    reread only, never evidence of first appearance or decision-time access.
    """
    start = datetime.combine(trading_date, time(9, 30), tzinfo=_NEW_YORK)
    end = datetime.combine(trading_date, time(9, 37), tzinfo=_NEW_YORK)
    return {
        "timeframe": "1Min",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "feed": "sip",
    }


async def capture(
    *,
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    artifact_root: Path = Path("artifacts/us-regular-session-raw-capture"),
    session_label: str | None = None,
    u06_shadow: bool = False,
    historical_sip_date: date | None = None,
    client: AsyncGetClient | None = None,
) -> list[Path]:
    """Perform independent GET observations and return append-only artifact paths."""
    label = session_label or ("u06_shadow" if u06_shadow else default_session_label())
    run_dir = (
        artifact_root
        / f"run-{_utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}"
    )
    opening_date = historical_sip_date or _utc_now().astimezone(_NEW_YORK).date()
    if client is not None:
        return [
            await _attempt(
                client,
                item,
                artifact_dir=run_dir,
                session_label=label,
                u06_shadow=u06_shadow,
            )
            for symbol in symbols
            for item in _requests_for(symbol, opening_date)
        ]
    async with httpx.AsyncClient(follow_redirects=False) as http_client:
        return [
            await _attempt(
                http_client,
                item,
                artifact_dir=run_dir,
                session_label=label,
                u06_shadow=u06_shadow,
            )
            for symbol in symbols
            for item in _requests_for(symbol, opening_date)
        ]
