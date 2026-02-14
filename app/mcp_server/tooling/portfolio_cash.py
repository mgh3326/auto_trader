"""Portfolio cash-balance helper utilities."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.shared import (
    normalize_account_filter as _normalize_account_filter,
)
from app.mcp_server.tooling.shared import (
    to_float,
)
from app.services import upbit as upbit_service
from app.services.kis import KISClient


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


async def get_cash_balance_impl(account: str | None = None) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    total_krw = 0.0
    total_usd = 0.0

    account_filter = _normalize_account_filter(account)
    strict_mode = account_filter is not None

    if account_filter is None or account_filter in ("upbit",):
        try:
            krw_balance = await upbit_service.fetch_krw_balance()
            accounts.append(
                {
                    "account": "upbit",
                    "account_name": "기본 계좌",
                    "broker": "upbit",
                    "currency": "KRW",
                    "balance": krw_balance,
                    "formatted": f"{int(krw_balance):,} KRW",
                }
            )
            total_krw += krw_balance
        except Exception as exc:
            errors.append({"source": "upbit", "market": "crypto", "error": str(exc)})

    if account_filter is None or account_filter in ("kis", "kis_domestic", "kis_overseas"):
        kis = KISClient()

        if account_filter is None or account_filter in ("kis", "kis_domestic"):
            try:
                domestic_data = await kis.inquire_domestic_cash_balance()
                dncl_amt = float(domestic_data.get("dnca_tot_amt", 0) or 0)
                orderable = float(domestic_data.get("stck_cash_ord_psbl_amt", 0) or 0)
                accounts.append(
                    {
                        "account": "kis_domestic",
                        "account_name": "기본 계좌",
                        "broker": "kis",
                        "currency": "KRW",
                        "balance": dncl_amt,
                        "orderable": orderable,
                        "formatted": f"{int(dncl_amt):,} KRW",
                    }
                )
                total_krw += dncl_amt
            except Exception as exc:
                if strict_mode:
                    raise RuntimeError(
                        f"KIS domestic cash balance query failed: {exc}"
                    ) from exc
                errors.append({"source": "kis", "market": "kr", "error": str(exc)})

        if account_filter is None or account_filter in ("kis", "kis_overseas"):
            try:
                overseas_margin_data = await kis.inquire_overseas_margin()
                usd_margin = select_usd_row_for_us_order(overseas_margin_data)
                if usd_margin is None:
                    raise RuntimeError(
                        "USD margin data not found in KIS overseas margin"
                    )

                balance = to_float(
                    usd_margin.get("frcr_dncl_amt1") or usd_margin.get("frcr_dncl_amt_2"),
                    default=0.0,
                )
                orderable = extract_usd_orderable_from_row(usd_margin)

                accounts.append(
                    {
                        "account": "kis_overseas",
                        "account_name": "기본 계좌",
                        "broker": "kis",
                        "currency": "USD",
                        "balance": balance,
                        "orderable": orderable,
                        "exchange_rate": None,
                        "formatted": f"${balance:.2f} USD",
                    }
                )
                total_usd += balance
            except Exception as exc:
                if strict_mode:
                    raise RuntimeError(
                        f"KIS overseas cash balance query failed: {exc}"
                    ) from exc
                errors.append({"source": "kis", "market": "us", "error": str(exc)})

    return {
        "accounts": accounts,
        "summary": {
            "total_krw": total_krw,
            "total_usd": total_usd,
        },
        "errors": errors,
    }


__all__ = [
    "get_cash_balance_impl",
    "is_us_nation_name",
    "extract_usd_orderable_from_row",
    "select_usd_row_for_us_order",
]
