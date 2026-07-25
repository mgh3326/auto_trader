"""Handler for get_market_index tool."""

from __future__ import annotations

import asyncio
from datetime import datetime
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
)
from app.mcp_server.tooling.shared import error_payload as _error_payload

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
    return (
        _is_zero(index.get("change"))
        and _is_zero(index.get("change_pct"))
        and _has_distinct_prices(index.get("open"), index.get("current"))
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
                index["as_of"] = now_kst().isoformat()
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
            if meta["source"] == "naver":
                current_data, history = await asyncio.gather(
                    _fetch_index_kr_current(meta["naver_code"], meta["name"]),
                    _fetch_index_kr_history(meta["naver_code"], capped_count, period),
                )
                return {
                    "indices": [_tag_kr_index_data_state(current_data)],
                    "history": history,
                }
            if meta["source"] == "coingecko":
                current_data = await _fetch_index_crypto_current(
                    meta["cg_metric"], meta["name"], sym
                )
                return {"indices": [current_data], "history": []}
            current_data, history = await asyncio.gather(
                _fetch_index_us_current(meta["yf_ticker"], meta["name"], sym),
                _fetch_index_us_history(meta["yf_ticker"], capped_count, period),
            )
            return {"indices": [current_data], "history": history}
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
            if _INDEX_META[_DEFAULT_INDICES[i]]["source"] == "naver":
                _tag_kr_index_data_state(r)
            indices.append(r)
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
