"""Portfolio cash-balance helper utilities."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.shared import to_float


def is_us_nation_name(value: Any) -> bool:
    normalized = str(value or "").strip().casefold()
    return normalized in {
        "미국",
        "us",
        "usa",
        "united states",
        "united states of america",
    }


def extract_usd_orderable_from_row(row: dict[str, Any] | None) -> float:
    if not isinstance(row, dict):
        return 0.0
    return to_float(row.get("frcr_gnrl_ord_psbl_amt"), default=0.0)


def select_usd_row_for_us_order(
    rows: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not rows:
        return None

    usd_rows = [
        row for row in rows if str(row.get("crcy_cd", "")).strip().upper() == "USD"
    ]
    if not usd_rows:
        return None

    us_row = next((row for row in usd_rows if is_us_nation_name(row.get("natn_name"))), None)
    if us_row is not None:
        return us_row

    return max(usd_rows, key=extract_usd_orderable_from_row)


__all__ = [
    "is_us_nation_name",
    "extract_usd_orderable_from_row",
    "select_usd_row_for_us_order",
]
