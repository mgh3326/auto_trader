"""Benzinga calendar client (hk 6513).

One bounded GET per page against the endpoints in
:mod:`app.services.benzinga_calendars.spec`. The key travels only in the
``token`` query parameter (the header form answers HTTP 401), is read from env
at call time and never appears in an error, a log line or a stored payload.

Safety: the caller must pass :func:`app.services.benzinga_http.
require_calendars_enabled` before constructing this client; the client itself
never reads the gate, so there is exactly one gate check per run and it happens
before any socket is opened.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.benzinga_calendars.spec import CalendarEndpoint, endpoint_for
from app.services.benzinga_http import (
    DEFAULT_TIMEOUT_S,
    BenzingaContractError,
    BenzingaPaginationCapExceeded,
    fetch_json,
)

DEFAULT_MAX_PAGES = 20


def _csv(value: str | None) -> str | None:
    if not value:
        return None
    items = [item.strip().upper() for item in str(value).split(",")]
    kept = [item for item in items if item]
    return ",".join(kept) or None


def build_params(
    endpoint: CalendarEndpoint,
    *,
    page: int,
    from_date: date,
    to_date: date,
    symbols: str | None,
) -> dict[str, Any]:
    """Query parameters for one page of one endpoint (pure)."""
    params: dict[str, Any] = {"page": page}
    if endpoint.requires_symbols:
        params["symbols"] = _csv(symbols) or ""
        params["from"] = from_date.isoformat()
        params["to"] = to_date.isoformat()
        params["pageSize"] = endpoint.page_size
    elif endpoint.row_key is None:
        # News/WIIM shape: /api/v2/news
        params["pageSize"] = endpoint.page_size
        params["dateFrom"] = from_date.isoformat()
        params["dateTo"] = to_date.isoformat()
        params.update(endpoint.extra_params)
        tickers = _csv(symbols)
        if tickers:
            params["primaryTickers"] = tickers
    else:
        params["pagesize"] = endpoint.page_size
        params["parameters[date_from]"] = from_date.isoformat()
        params["parameters[date_to]"] = to_date.isoformat()
        tickers = _csv(symbols)
        if tickers:
            params["parameters[tickers]"] = tickers
    return params


def _is_error_envelope(payload: Any) -> bool:
    if isinstance(payload, dict):
        if payload.get("ok") is False:
            return True
        if "errors" in payload and "data" not in payload:
            return True
        if any(key in payload for key in ("error", "errors")):
            return True
    return False


def extract_rows(endpoint: CalendarEndpoint, payload: Any) -> list[dict[str, Any]]:
    """Pull the documented row list out of one page, failing closed (pure).

    A provider error envelope, a missing key, a non-list payload or a row that
    is not an object fails the whole page — a silently skipped page would read
    downstream as "no events".
    """
    if _is_error_envelope(payload) and not (
        isinstance(payload, dict) and endpoint.row_key in payload
    ):
        raise BenzingaContractError(
            f"benzinga {endpoint.category} returned an error envelope "
            f"(path {endpoint.path})"
        )
    if endpoint.row_key is None:
        rows = payload
    else:
        if not isinstance(payload, dict):
            raise BenzingaContractError(
                f"benzinga {endpoint.category} returned non-object JSON "
                f"(path {endpoint.path})"
            )
        rows = payload.get(endpoint.row_key)
        if endpoint.nested_symbol_map and isinstance(rows, dict):
            flattened: list[dict[str, Any]] = []
            for symbol, block in rows.items():
                data = block.get("data") if isinstance(block, dict) else None
                if not isinstance(data, list):
                    raise BenzingaContractError(
                        f"benzinga {endpoint.category} block for {symbol!r} "
                        f"is not a data list (path {endpoint.path})"
                    )
                for row in data:
                    if isinstance(row, dict) and "symbol" not in row:
                        row = {**row, "symbol": symbol}
                    flattened.append(row)
            rows = flattened
    if not isinstance(rows, list):
        raise BenzingaContractError(
            f"benzinga {endpoint.category} returned no row list (path {endpoint.path})"
        )
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BenzingaContractError(
                f"benzinga {endpoint.category} row {row_index} is not an object "
                f"(path {endpoint.path})"
            )
    return rows


class BenzingaCalendarClient:
    def __init__(
        self,
        *,
        api_key: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_pages: int = DEFAULT_MAX_PAGES,
        client_factory: Any = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._max_pages = max(1, int(max_pages))
        self._client_factory = client_factory

    async def fetch(
        self,
        category: str,
        *,
        from_date: date,
        to_date: date,
        symbols: str | None = None,
    ) -> list[dict[str, Any]]:
        endpoint = endpoint_for(category)
        if endpoint.requires_symbols and not _csv(symbols):
            raise BenzingaContractError(
                f"benzinga {category} requires --symbols (path {endpoint.path})"
            )
        collected: list[dict[str, Any]] = []
        for page in range(self._max_pages):
            params = build_params(
                endpoint,
                page=page,
                from_date=from_date,
                to_date=to_date,
                symbols=symbols,
            )
            params["token"] = self._api_key
            payload = await fetch_json(
                endpoint.path,
                params,
                timeout_s=self._timeout_s,
                client_factory=self._client_factory,
            )
            rows = extract_rows(endpoint, payload)
            collected.extend(rows)
            if len(rows) < endpoint.page_size:
                return collected
        raise BenzingaPaginationCapExceeded(
            f"benzinga {category} reached the page cap ({self._max_pages}) with "
            f"a full page (path {endpoint.path})"
        )


__all__ = [
    "DEFAULT_MAX_PAGES",
    "BenzingaCalendarClient",
    "build_params",
    "extract_rows",
]
