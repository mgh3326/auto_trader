"""Portfolio cash-balance helper utilities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import app.services.brokers.upbit.client as upbit_service
from app.core.timezone import now_kst
from app.mcp_server.tooling.shared import (
    logger,
    to_float,
)
from app.mcp_server.tooling.shared import (
    normalize_account_filter as _normalize_account_filter,
)
from app.mcp_server.tooling.user_settings_tools import get_manual_cash_setting
from app.services.brokers.kis import (
    KISClient,
    extract_domestic_cash_summary_from_integrated_margin,
)
from app.services.exchange_rate_service import get_usd_krw_rate as _get_usd_krw_rate


async def _get_kis_domestic_pending_buy_amount(kis: KISClient) -> float:
    total = 0.0
    open_orders = await kis.inquire_korea_orders()
    for order in open_orders:
        if str(order.get("sll_buy_dvsn_cd", "")).strip() != "02":
            continue
        price = to_float(order.get("ord_unpr"), default=0.0)
        qty = to_float(
            order.get("nccs_qty") or order.get("ord_qty"),
            default=0.0,
        )
        total += price * qty
    return total


async def _get_kis_overseas_pending_buy_amount_usd(kis: KISClient) -> float:
    total = 0.0
    # KIS documents NASD as a US-wide open-order lookup. Querying NYSE/AMEX as
    # well can double-count the same locked cash when orders are mirrored there.
    open_orders = await kis.inquire_overseas_orders("NASD")
    for order in open_orders:
        if str(order.get("sll_buy_dvsn_cd", "")).strip() != "02":
            continue
        price = to_float(order.get("ft_ord_unpr3"), default=0.0)
        qty = to_float(
            order.get("nccs_qty") or order.get("ft_ord_qty"),
            default=0.0,
        )
        total += price * qty
    return total


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

    us_row = next(
        (row for row in usd_rows if is_us_nation_name(row.get("natn_name"))), None
    )
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
            summary = await upbit_service.fetch_krw_cash_summary()
            krw_balance = float(summary.get("balance", 0.0))
            krw_orderable = float(summary.get("orderable", 0.0))
            accounts.append(
                {
                    "account": "upbit",
                    "account_name": "기본 계좌",
                    "broker": "upbit",
                    "currency": "KRW",
                    "balance": krw_balance,
                    "orderable": krw_orderable,
                    "formatted": f"{int(krw_balance):,} KRW",
                }
            )
            total_krw += krw_balance
        except Exception as exc:
            errors.append({"source": "upbit", "market": "crypto", "error": str(exc)})

    if account_filter is None or account_filter in (
        "kis",
        "kis_domestic",
        "kis_overseas",
    ):
        kis = KISClient()

        if account_filter is None or account_filter in ("kis", "kis_domestic"):
            try:
                margin_data = await kis.inquire_integrated_margin()
                domestic_cash = extract_domestic_cash_summary_from_integrated_margin(
                    margin_data
                )
                dncl_amt = float(domestic_cash.get("balance", 0) or 0)
                raw_orderable = float(domestic_cash.get("orderable", 0) or 0)
                orderable = raw_orderable

                try:
                    pending_buy_amount = await _get_kis_domestic_pending_buy_amount(kis)
                    orderable = max(0.0, raw_orderable - pending_buy_amount)
                except Exception as exc:
                    logger.warning(
                        "KR pending order deduction failed, using raw orderable: %s",
                        exc,
                    )

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
                    usd_margin.get("frcr_dncl_amt1")
                    or usd_margin.get("frcr_dncl_amt_2"),
                    default=0.0,
                )
                raw_orderable = extract_usd_orderable_from_row(usd_margin)
                orderable = raw_orderable

                try:
                    pending_usd = await _get_kis_overseas_pending_buy_amount_usd(kis)
                    orderable = max(0.0, raw_orderable - pending_usd)
                except Exception as exc:
                    logger.warning(
                        "USD pending order deduction failed, using raw orderable: %s",
                        exc,
                    )

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


async def get_usd_krw_rate() -> float:
    """Get the current USD to KRW exchange rate."""
    try:
        return await _get_usd_krw_rate()
    except Exception as exc:
        logger.warning("Failed to fetch USD/KRW rate: %s", exc)
        return 1300.0


def _is_stale_manual_cash(updated_at_iso: str | None) -> bool:
    """Check if manual cash is stale (older than 3 days)."""
    if not updated_at_iso:
        return True
    try:
        updated_at = datetime.fromisoformat(updated_at_iso)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        cutoff = now_kst() - timedelta(days=3)
        return updated_at < cutoff
    except (ValueError, TypeError):
        return True


async def get_available_capital_impl(
    account: str | None = None,
    include_manual: bool = True,
) -> dict[str, Any]:
    """Query orderable capital across KIS, Upbit, and manual cash.

    Args:
        account: Optional account filter (upbit, kis, kis_domestic, kis_overseas, toss)
        include_manual: Whether to include manual cash in the aggregation

    Returns:
        Dict with accounts, manual_cash, summary, and errors
    """
    account_filter = _normalize_account_filter(account)
    errors: list[dict[str, Any]] = []

    cash_result = await get_cash_balance_impl(account=account)
    accounts = cash_result.get("accounts", [])
    errors.extend(cash_result.get("errors", []))

    has_usd_account = any(acc.get("currency") == "USD" for acc in accounts)
    exchange_rate = None
    if has_usd_account:
        try:
            exchange_rate = await get_usd_krw_rate()
        except Exception as exc:
            logger.warning("Failed to get exchange rate: %s", exc)
            errors.append({"source": "exchange_rate", "error": str(exc)})
            exchange_rate = 1300.0

    total_orderable_krw = 0.0
    processed_accounts: list[dict[str, Any]] = []

    for acc in accounts:
        processed_acc = dict(acc)
        currency = acc.get("currency", "KRW")
        orderable = float(acc.get("orderable", 0.0) or 0.0)

        if currency == "KRW":
            total_orderable_krw += orderable
        elif currency == "USD" and exchange_rate is not None:
            krw_equivalent = orderable * exchange_rate
            processed_acc["krw_equivalent"] = krw_equivalent
            total_orderable_krw += krw_equivalent

        processed_accounts.append(processed_acc)

    manual_cash_result: dict[str, Any] | None = None
    if include_manual:
        try:
            manual_setting = await get_manual_cash_setting()
            if manual_setting is not None:
                value = manual_setting.get("value", {})
                amount = (
                    float(value.get("amount", 0.0)) if isinstance(value, dict) else 0.0
                )
                updated_at = manual_setting.get("updated_at")
                stale_warning = _is_stale_manual_cash(updated_at)

                manual_cash_result = {
                    "amount": amount,
                    "updated_at": updated_at,
                    "stale_warning": stale_warning,
                }
                total_orderable_krw += amount
        except Exception as exc:
            logger.warning("Failed to get manual cash setting: %s", exc)
            errors.append({"source": "manual_cash", "error": str(exc)})

    return {
        "accounts": processed_accounts,
        "manual_cash": manual_cash_result,
        "summary": {
            "total_orderable_krw": total_orderable_krw,
            "exchange_rate_usd_krw": exchange_rate,
            "as_of": now_kst().isoformat(),
        },
        "errors": errors,
    }


__all__ = [
    "get_cash_balance_impl",
    "get_available_capital_impl",
    "get_usd_krw_rate",
    "is_us_nation_name",
    "extract_usd_orderable_from_row",
    "select_usd_row_for_us_order",
]
