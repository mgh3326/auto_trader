"""Handler for get_market_index tool."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.fundamentals_sources_indices import (
    _DEFAULT_INDICES,
    _INDEX_META,
    _fetch_index_crypto_current,
    _fetch_index_kr_current,
    _fetch_index_kr_history,
    _fetch_index_us_current,
    _fetch_index_us_history,
)
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_STALE,
    kr_market_data_state,
    us_market_session,
)
from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.services.brokers.kis.futures_quotes import (
    GATE_ENV,
    FuturesQuotesGateOff,
    gate_check,
)

_KR_INDEX_LAGGING_REASON = "kr_index_fresh_clock_payload_lagging"

# ROB-731: during an OPEN KRX session the Naver basic payload can lag real time.
# Near flat, a stale quote inverts the sign of change_pct vs live (KOSDAQ +0.18
# vs −0.46 at 09:10 KST 2026-07-06). Naver stamps the quote it derives the
# change from at minute granularity, so allow one minute of natural granularity
# plus a small margin before calling the quote stale. Tunable pending live
# measurement of the real intraday lag distribution.
_KR_INDEX_QUOTE_LAG_STALE_SECONDS = 120
_KR_INDEX_QUOTE_LAG_REASON = "kr_index_quote_lagging"


def _parse_quote_asof(value: Any) -> datetime | None:
    """Parse a Naver ``localTradedAt`` quote timestamp into a tz-aware datetime.

    Naver returns a full ISO timestamp with a ``+09:00`` offset during the
    session (e.g. ``2026-07-06T11:19:00+09:00``). Date-only strings (the daily
    price rows) and unparseable values yield ``None`` — a missing timestamp
    cannot be used to assess lag.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A bare date carries no intraday time → not usable for lag detection.
    if len(value) <= 10:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _kr_index_quote_lag_seconds(quote_asof: Any) -> int | None:
    """Seconds the Naver quote lags ``now_kst`` (None if unknown/in the future)."""
    parsed = _parse_quote_asof(quote_asof)
    if parsed is None:
        return None
    lag = (now_kst() - parsed).total_seconds()
    if lag < 0:
        return None
    return int(lag)


def _is_zero(value: Any) -> bool:
    return isinstance(value, (int, float)) and value == 0


def _has_distinct_prices(left: Any, right: Any) -> bool:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    return left != right


def _is_fresh_clock_lagging_kr_index(index: dict[str, Any]) -> bool:
    # When the OHLC is from an earlier session its top-level open is null (date
    # separation). Compare against that session's open, the value this predicate
    # read before the split, so the frozen-payload check still fires.
    previous = index.get("previous_session")
    open_price = index.get("open")
    if open_price is None and isinstance(previous, dict):
        open_price = previous.get("open")
    return (
        _is_zero(index.get("change"))
        and _is_zero(index.get("change_pct"))
        and _has_distinct_prices(open_price, index.get("current"))
    )


def _tag_kr_index_data_state(index: Any) -> Any:
    """ROB-464: tag KR (naver) index dicts with the KRX session data_state.

    Pre-market / closed sessions otherwise return change_pct=0 (frozen at the
    prior close), which reads as a real flat session.
    """
    if isinstance(index, dict) and "error" not in index:
        data_state = kr_market_data_state()
        if data_state == DATA_STATE_FRESH:
            if _is_fresh_clock_lagging_kr_index(index):
                # ROB-464: all-zero change frozen at the prior close.
                data_state = DATA_STATE_STALE
                index["data_state_reason"] = _KR_INDEX_LAGGING_REASON
                index["as_of"] = _to_utc_iso(now_kst())
                index["as_of_basis"] = "fetched_at"
            else:
                # ROB-731: minute-granular quote lag. The signed change_pct is
                # only as fresh as the quote it was derived from; when that lags
                # real time the near-flat sign can be inverted vs live.
                lag = _kr_index_quote_lag_seconds(index.get("quote_asof"))
                if lag is not None and lag > _KR_INDEX_QUOTE_LAG_STALE_SECONDS:
                    data_state = DATA_STATE_STALE
                    index["data_state_reason"] = _KR_INDEX_QUOTE_LAG_REASON
                    index["quote_lag_seconds"] = lag
        index["data_state"] = data_state
    return index


def _to_utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _decorate_index_row(row: Any, meta: dict[str, str] | None) -> Any:
    """Add ``as_of`` (UTC), ``as_of_basis`` and the session/contract/delay labels.

    ``as_of_basis`` is ``quote_time`` when the row carries the exchange quote
    timestamp (Naver ``localTradedAt``) and ``fetched_at`` otherwise (Yahoo and
    CoinGecko rows carry no quote time, so the fetch time is the only honest
    stamp). An ``as_of`` already set upstream (ROB-464) is kept as-is.
    Error rows are returned untouched.
    """
    if not isinstance(row, dict) or "error" in row:
        return row
    now = now_kst()
    quote = _parse_quote_asof(row.get("quote_asof"))
    if "as_of" not in row:
        if quote is not None:
            row["as_of"] = _to_utc_iso(quote)
            row["as_of_basis"] = "quote_time"
        else:
            row["as_of"] = _to_utc_iso(now)
            row["as_of_basis"] = "fetched_at"
    if meta is None or row.get("unsupported"):
        return row
    kind = meta.get("kind")
    if meta.get("source") == "yfinance":
        if kind == "continuous_future":
            row["session_state"] = "continuous_futures"
            row["contract"] = {
                "kind": "continuous",
                "yahoo_symbol": meta["yf_ticker"],
                "note": "Yahoo continuous front-month; the underlying contract rolls without notice",
            }
            row["delayed"] = None
            row["delay_basis"] = "not_verified"
        elif kind == "fx":
            row["session_state"] = "continuous_fx"
        else:
            row["session_state"] = us_market_session(now)
    return row


def _unsupported_row(symbol: str, meta: dict[str, str], reason: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": meta["name"],
        "current": None,
        "change": None,
        "change_pct": None,
        "source": "kis_futures_quotes",
        "unsupported": True,
        "unsupported_reason": reason,
    }


async def _fetch_kospi200_night_futures(
    symbol: str, meta: dict[str, str]
) -> dict[str, Any]:
    """KOSPI200 night futures: always unsupported in this repo.

    KIS documents no night-session futures quote TR (hk 6148). The only price TR the
    futures quotes view has (``inquire-price``, market division ``F``) returns the
    regular-session quote, so reporting it under a night symbol would present day
    data as a night price. The gate only changes the reason. No KIS call is made.
    """
    try:
        gate_check()
    except FuturesQuotesGateOff:
        return _unsupported_row(
            symbol,
            meta,
            f"{GATE_ENV} is not true; night-session futures quotes are gated (hk 6148)",
        )
    return _unsupported_row(
        symbol,
        meta,
        "KIS documents no night-session futures quote TR; the regular F-market price "
        "is the day session and is not reported as a night price (hk 6148)",
    )


async def handle_get_market_index(
    symbol: str | None = None,
    period: str = "day",
    count: int = 20,
) -> dict[str, Any]:
    period = (period or "day").strip().lower()
    if period not in ("day", "week", "month"):
        raise ValueError("period must be 'day', 'week', or 'month'")

    capped_count = min(max(count, 1), 100)

    if symbol:
        sym = symbol.strip().upper()
        meta = _INDEX_META.get(sym)
        if meta is None:
            raise ValueError(
                f"Unknown index symbol '{sym}'. Supported: {', '.join(sorted(_INDEX_META))}"
            )

        try:
            if meta["source"] == "kis_futures":
                row = await _fetch_kospi200_night_futures(sym, meta)
                return {"indices": [_decorate_index_row(row, meta)], "history": []}
            if meta["source"] == "naver":
                current_data, history = await asyncio.gather(
                    _fetch_index_kr_current(meta["naver_code"], meta["name"]),
                    _fetch_index_kr_history(meta["naver_code"], capped_count, period),
                )
                # The row names the requested symbol (KOSPI200 is Naver code KPI200).
                current_data["symbol"] = sym
                return {
                    "indices": [
                        _decorate_index_row(
                            _tag_kr_index_data_state(current_data), meta
                        )
                    ],
                    "history": history,
                }
            if meta["source"] == "coingecko":
                current_data = await _fetch_index_crypto_current(
                    meta["cg_metric"], meta["name"], sym
                )
                return {
                    "indices": [_decorate_index_row(current_data, meta)],
                    "history": [],
                }
            current_data, history = await asyncio.gather(
                _fetch_index_us_current(meta["yf_ticker"], meta["name"], sym),
                _fetch_index_us_history(meta["yf_ticker"], capped_count, period),
            )
            return {
                "indices": [_decorate_index_row(current_data, meta)],
                "history": history,
            }
        except Exception as exc:
            return _error_payload(source=meta["source"], message=str(exc), symbol=sym)

    # _DEFAULT_INDICES is equity-only (naver/yfinance) — coingecko symbols
    # (CRYPTO/BTC.D) are fetched explicitly via the single-symbol path above and
    # must never appear here (guarded by test_crypto_not_in_default_indices), so
    # the naver/else(yfinance) split below is exhaustive for the default batch.
    tasks = []
    for idx_sym in _DEFAULT_INDICES:
        meta = _INDEX_META[idx_sym]
        if meta["source"] == "naver":
            tasks.append(_fetch_index_kr_current(meta["naver_code"], meta["name"]))
        else:
            tasks.append(
                _fetch_index_us_current(meta["yf_ticker"], meta["name"], idx_sym)
            )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    indices: list[dict[str, Any]] = []
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            indices.append({"symbol": _DEFAULT_INDICES[i], "error": str(r)})
        elif isinstance(r, dict):
            meta_i = _INDEX_META[_DEFAULT_INDICES[i]]
            if meta_i["source"] == "naver":
                _tag_kr_index_data_state(r)
            indices.append(_decorate_index_row(r, meta_i))
        else:
            indices.append({"symbol": _DEFAULT_INDICES[i], "error": str(r)})

    return {"indices": indices}


async def handle_get_market_index_current_only(symbol: str) -> dict[str, Any]:
    """ROB-689: current-quote-only index fetch (drops the unused history page).

    market-parity's get_index_quote reads only the current row, but the shared
    handle_get_market_index also fetches a full history page per call. This sibling
    returns the same current-row shape ({"indices": [row]}) WITHOUT the history
    fetch. _fetch_index_kr_current (basic + 1-row price page) is kept intact so the
    'open' field is present and _tag_kr_index_data_state can still apply the ROB-464
    freshness override. The shared handle_get_market_index is intentionally NOT
    modified (its other callers consume the history).
    """
    sym = (symbol or "").strip().upper()
    meta = _INDEX_META.get(sym)
    if meta is None:
        raise ValueError(
            f"Unknown index symbol '{sym}'. Supported: {', '.join(sorted(_INDEX_META))}"
        )
    try:
        if meta["source"] == "naver":
            current_data = await _fetch_index_kr_current(
                meta["naver_code"], meta["name"]
            )
            return {"indices": [_tag_kr_index_data_state(current_data)]}
        if meta["source"] == "coingecko":
            current_data = await _fetch_index_crypto_current(
                meta["cg_metric"], meta["name"], sym
            )
            return {"indices": [current_data]}
        current_data = await _fetch_index_us_current(
            meta["yf_ticker"], meta["name"], sym
        )
        return {"indices": [current_data]}
    except Exception as exc:
        return _error_payload(source=meta["source"], message=str(exc), symbol=sym)
