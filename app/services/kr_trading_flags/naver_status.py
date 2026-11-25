"""hk 6503 — Naver 관리·거래정지·경보 status list (intraday overlay).

Endpoint (public, read-only; observed 2026-11-25 in the at-gap reference):
  GET https://stock.naver.com/api/domestic/market/stock/default
      ?tradeType=KRX&marketType=ALL&orderType=statusTag&pageSize=N
The response is a JSON list of item objects. The reference fixture is
``tests/fixtures/kr_trading_flags/naver_management_sample.json``. A dict with a
``stocks`` list is also accepted, in case the endpoint wraps the list.

Fields used:
  manageStatusGb / managementDate / managementReasonCode  -> management
  tradeStopYn / tradingHaltDate / tradingHaltReasonCode   -> trading_halt
  marketAlertType                                          -> market_alert

Not used: tradableStatus*, marketStatus, tradingSessionType, prices. A list
item with no field for a category yields no observation for that category, so
the stored state stays as it was (unknown when never observed).

Overlay rule: the endpoint only lists symbols that carry a status. A symbol
missing from the list is NOT evidence that the flag is clear, so nothing is
written for it.

[unverified] ``manageStatusGb`` ``1`` = 관리 is inferred from the fixture (224060
is on the 관리 tab with ``managementDate`` and a reason code). Other values are
stored raw as ``unmapped``. ``marketAlertType`` ``02``/``03`` are unconfirmed.

Pure parser plus one GET helper. The parser does no network I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx

from app.services.kr_trading_flags import vocabulary as v

NAVER_STATUS_URL = "https://stock.naver.com/api/domestic/market/stock/default"
DEFAULT_PAGE_SIZE = 500
_TIMEOUT_SECONDS = 15.0


class NaverStatusShapeError(ValueError):
    """The payload is not the status list shape. Nothing may be written."""


def _parse_yyyymmdd(raw: object) -> date | None:
    text = str(raw or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _text_or_none(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _symbol_of(item: Mapping[str, Any]) -> str | None:
    symbol = str(item.get("itemcode") or "").strip().upper()
    if len(symbol) != 6 or not symbol.isalnum():
        return None
    return symbol


def _items_of(payload: object) -> list[object]:
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, Mapping) and isinstance(payload.get("stocks"), list):
        return list(payload["stocks"])
    raise NaverStatusShapeError("naver status payload is not a list of items")


def parse_naver_status_payload(payload: object) -> tuple[list[v.FlagObservation], int]:
    """Return ``(observations, skipped_items)`` for one status-list payload.

    ``skipped_items`` counts list entries that are not objects or have no valid
    6-character ``itemcode``. Raises :class:`NaverStatusShapeError` when the
    payload is not a list shape at all.
    """
    items = _items_of(payload)
    observations: list[v.FlagObservation] = []
    skipped = 0
    source = v.SOURCE_NAVER_INTRADAY
    for item in items:
        symbol = _symbol_of(item) if isinstance(item, Mapping) else None
        if symbol is None:
            skipped += 1
            continue

        if item.get("manageStatusGb") is not None:
            state, raw = v.map_naver_management_status(str(item["manageStatusGb"]))
            observations.append(
                v.FlagObservation(
                    symbol=symbol,
                    category=v.CATEGORY_MANAGEMENT,
                    state=state,
                    source=source,
                    raw_code=raw,
                    reason_code=_text_or_none(item.get("managementReasonCode")),
                    effective_date=_parse_yyyymmdd(item.get("managementDate")),
                )
            )

        if item.get("tradeStopYn") is not None:
            state, raw = v.map_trading_halt_yn(str(item["tradeStopYn"]))
            observations.append(
                v.FlagObservation(
                    symbol=symbol,
                    category=v.CATEGORY_TRADING_HALT,
                    state=state,
                    source=source,
                    raw_code=raw,
                    reason_code=_text_or_none(item.get("tradingHaltReasonCode")),
                    effective_date=_parse_yyyymmdd(item.get("tradingHaltDate")),
                )
            )

        if item.get("marketAlertType") is not None:
            state, raw = v.map_market_alert_code(str(item["marketAlertType"]))
            observations.append(
                v.FlagObservation(
                    symbol=symbol,
                    category=v.CATEGORY_MARKET_ALERT,
                    state=state,
                    source=source,
                    raw_code=raw,
                )
            )
    return observations, skipped


async def fetch_naver_status_payload(page_size: int = DEFAULT_PAGE_SIZE) -> object:
    """GET the status list. Redirects are not followed; a non-200 raises."""
    params = {
        "tradeType": "KRX",
        "marketType": "ALL",
        "orderType": "statusTag",
        "pageSize": str(page_size),
    }
    headers = {"Accept": "application/json", "User-Agent": "at-kr-trading-flags/1.0"}
    timeout = httpx.Timeout(_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, headers=headers
    ) as client:
        response = await client.get(NAVER_STATUS_URL, params=params)
        response.raise_for_status()
    return response.json()
