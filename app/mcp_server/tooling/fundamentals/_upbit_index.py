"""Handlers for get_upbit_index and get_upbit_altseason tools (ROB-381 PR2).

Read-only public market-data. Indices from robots-clean datalab-static; 24h
breadth from the official Open API ticker. 4-layer fail-open: validate →
normalize → fetch → catch & return error_payload. No broker/order/account API.
"""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.services.external import upbit_index

_ALLOWED_CATEGORIES = {"market", "sector", "strategy", "theme"}


async def handle_get_upbit_index(
    category: str | None = None,
) -> dict[str, Any]:
    normalized_category: str | None = None
    if category is not None and category.strip():
        normalized_category = category.strip().lower()
        if normalized_category not in _ALLOWED_CATEGORIES:
            raise ValueError(
                f"category must be one of: {', '.join(sorted(_ALLOWED_CATEGORIES))}"
            )

    try:
        payload = await upbit_index.fetch_upbit_indices()
        if payload is None:
            return _error_payload(
                source="upbit_datalab",
                message="Upbit index data unavailable",
                instrument_type="crypto",
            )
        if normalized_category is None:
            return payload
        filtered = {
            code: row
            for code, row in payload.get("indices", {}).items()
            if row.get("category_type") == normalized_category
        }
        return {**payload, "indices": filtered, "category": normalized_category}
    except Exception as exc:
        return _error_payload(
            source="upbit_datalab",
            message=str(exc),
            instrument_type="crypto",
        )


async def handle_get_upbit_altseason(
    include_constituents: bool = False,
    constituents_limit: int = 50,
) -> dict[str, Any]:
    limit = max(1, min(int(constituents_limit), 200))
    try:
        payload = await upbit_index.fetch_upbit_altseason(
            include_constituents=include_constituents,
            constituents_limit=limit,
        )
        if payload is None:
            return _error_payload(
                source="upbit_datalab+upbit_open_api",
                message="Upbit altseason data unavailable",
                instrument_type="crypto",
            )
        return payload
    except Exception as exc:
        return _error_payload(
            source="upbit_datalab+upbit_open_api",
            message=str(exc),
            instrument_type="crypto",
        )

