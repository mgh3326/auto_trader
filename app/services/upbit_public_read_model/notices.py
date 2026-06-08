"""ROB-452 P1: Upbit notices (공지) read-only fetcher — listings / 유의(CAUTION) / 점검.

Keyless public endpoint, used as a crypto catalyst feed alongside the market-warnings
read model. The endpoint is UNOFFICIAL and its exact shape/host can change, so parsing
is defensive (locate the item list across known shapes) and the caller treats any
failure as a degraded ("unavailable") block — never a raise. Verify the live shape via
the operator smoke before relying on the field set.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

# Unofficial public announcements API (verified by ROB-452 live smoke via
# https://www.upbit.com/service_center/notice resource timing). Parsing tolerates
# shape drift because this is not a documented Open API surface.
_UPBIT_NOTICES_URL = "https://api-manager.upbit.com/api/v1/announcements"
_DEFAULT_PARAMS = {"os": "web", "page": "1", "per_page": "30", "category": "all"}
_TIMEOUT = httpx.Timeout(10.0)


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """Find the notices list across known/likely response shapes (defensive)."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        items: list[dict[str, Any]] = []
        for key in (
            # Upbit live shape returns fixed_notices + notices.
            "fixed_notices",
            "fixedNotices",
            "notice",
            "notices",
            "list",
            "items",
            "results",
        ):
            seq = data.get(key)
            if isinstance(seq, list):
                items.extend(x for x in seq if isinstance(x, dict))
        return items
    return []


def _item_date(item: dict[str, Any]) -> dt.datetime | None:
    for key in (
        "listed_at",
        "listedAt",
        "first_listed_at",
        "firstListedAt",
        "created_at",
        "createdAt",
        "updated_at",
        "updatedAt",
    ):
        raw = item.get(key)
        if not raw:
            continue
        try:
            return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
    return None


def _first_present(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    return None


def _first_listed_date(item: dict[str, Any]) -> dt.datetime | None:
    raw = _first_present(item, "first_listed_at", "firstListedAt")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def fetch_upbit_notices(
    *,
    days: int = 14,
    fetcher: Any = None,
) -> dict[str, Any]:
    """Return recent Upbit notices windowed to ``days``.

    Shape: ``{"state": "fresh"|"unavailable", "source": "upbit_notices",
    "fetched_at": iso, "items": [{title, category, listed_at}], "errorReason": str?}``.
    Fail-open: any network/shape error → state="unavailable" with errorReason.
    """
    now = dt.datetime.now(dt.UTC)
    try:
        if fetcher is not None:
            payload = await fetcher()
        else:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(_UPBIT_NOTICES_URL, params=_DEFAULT_PARAMS)
                response.raise_for_status()
                payload = response.json()
    except Exception as exc:  # noqa: BLE001 — unofficial endpoint; degrade gracefully
        return {
            "state": "unavailable",
            "source": "upbit_notices",
            "fetched_at": now.isoformat(),
            "items": [],
            "errorReason": str(exc),
        }

    cutoff = now - dt.timedelta(days=max(days, 1))
    items: list[dict[str, Any]] = []
    for raw in _extract_items(payload):
        listed = _item_date(raw)
        if listed is not None and listed.tzinfo is None:
            listed = listed.replace(tzinfo=dt.UTC)
        # keep within window when a parseable date exists; undated items pass through
        if listed is not None and listed < cutoff:
            continue
        first_listed = _first_listed_date(raw)
        if first_listed is not None and first_listed.tzinfo is None:
            first_listed = first_listed.replace(tzinfo=dt.UTC)
        items.append(
            {
                "id": _first_present(raw, "id", "notice_id", "noticeId"),
                "title": _first_present(raw, "title", "content"),
                "category": _first_present(raw, "category", "thread_name", "threadName"),
                "listed_at": listed.astimezone(dt.UTC).isoformat() if listed else None,
                "first_listed_at": (
                    first_listed.astimezone(dt.UTC).isoformat()
                    if first_listed
                    else None
                ),
                "need_new_badge": _first_present(
                    raw, "need_new_badge", "needNewBadge"
                ),
                "need_update_badge": _first_present(
                    raw, "need_update_badge", "needUpdateBadge"
                ),
            }
        )

    return {
        "state": "fresh",
        "source": "upbit_notices",
        "fetched_at": now.isoformat(),
        "items": items,
    }
