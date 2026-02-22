from __future__ import annotations

from typing import Any

from app.core.async_rate_limiter import RateLimitExceededError
from app.integrations.kis import (
    KISClient,
    extract_domestic_cash_summary_from_integrated_margin,
)
from app.integrations.upbit import fetch_my_coins
from app.services.account.contracts import CashBalance, MarginSnapshot, Position
from app.services.domain_errors import (
    RateLimitError,
    UpstreamUnavailableError,
    ValidationError,
)


def _normalize_market(market: str | None) -> str | None:
    if market is None:
        return None
    normalized = str(market).strip().lower()
    aliases = {
        "kr": "equity_kr",
        "us": "equity_us",
        "crypto": "crypto",
        "equity_kr": "equity_kr",
        "equity_us": "equity_us",
    }
    resolved = aliases.get(normalized)
    if resolved is None:
        raise ValidationError(f"Unsupported market: {market}")
    return resolved


def _to_float(value: Any, *, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _map_error(exc: Exception) -> Exception:
    if isinstance(exc, (ValidationError, RateLimitError, UpstreamUnavailableError)):
        return exc
    if isinstance(exc, RateLimitExceededError):
        return RateLimitError(str(exc))
    return UpstreamUnavailableError(str(exc))


def _select_usd_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    usd_rows = [row for row in rows if str(row.get("crcy_cd", "")).upper() == "USD"]
    if not usd_rows:
        return None
    for row in usd_rows:
        natn_name = str(row.get("natn_name", "")).strip().casefold()
        if natn_name in {
            "미국",
            "us",
            "usa",
            "united states",
            "united states of america",
        }:
            return row
    return max(usd_rows, key=lambda row: _to_float(row.get("frcr_gnrl_ord_psbl_amt")))


async def get_cash(market: str | None = None) -> list[CashBalance]:
    resolved_market = _normalize_market(market)
    balances: list[CashBalance] = []

    try:
        if resolved_market in (None, "crypto"):
            summary = await fetch_my_coins()
            krw_row = next(
                (
                    row
                    for row in summary
                    if str(row.get("currency", "")).upper() == "KRW"
                ),
                None,
            )
            if krw_row is not None:
                orderable = _to_float(krw_row.get("balance"))
                locked = _to_float(krw_row.get("locked"))
                balances.append(
                    CashBalance(
                        market="crypto",
                        currency="KRW",
                        balance=orderable + locked,
                        orderable=orderable,
                        source="upbit",
                    )
                )

        if resolved_market in (None, "equity_kr", "equity_us"):
            kis = KISClient()

            if resolved_market in (None, "equity_kr"):
                margin = await kis.inquire_integrated_margin()
                domestic = extract_domestic_cash_summary_from_integrated_margin(margin)
                balances.append(
                    CashBalance(
                        market="equity_kr",
                        currency="KRW",
                        balance=_to_float(domestic.get("balance")),
                        orderable=_to_float(domestic.get("orderable")),
                        source="kis",
                    )
                )

            if resolved_market in (None, "equity_us"):
                margin_rows = await kis.inquire_overseas_margin()
                usd_row = _select_usd_row(margin_rows)
                if usd_row is not None:
                    balances.append(
                        CashBalance(
                            market="equity_us",
                            currency="USD",
                            balance=_to_float(
                                usd_row.get("frcr_dncl_amt1")
                                or usd_row.get("frcr_dncl_amt_2")
                            ),
                            orderable=_to_float(usd_row.get("frcr_gnrl_ord_psbl_amt")),
                            source="kis",
                        )
                    )

        return balances
    except Exception as exc:
        raise _map_error(exc) from exc


async def get_positions(market: str | None = None) -> list[Position]:
    resolved_market = _normalize_market(market)
    positions: list[Position] = []

    try:
        if resolved_market in (None, "crypto"):
            coins = await fetch_my_coins()
            for coin in coins:
                currency = str(coin.get("currency", "")).upper().strip()
                if not currency or currency == "KRW":
                    continue
                quantity = _to_float(coin.get("balance")) + _to_float(
                    coin.get("locked")
                )
                if quantity <= 0:
                    continue
                unit_currency = str(coin.get("unit_currency", "KRW")).upper().strip()
                positions.append(
                    Position(
                        symbol=f"{unit_currency}-{currency}",
                        market="crypto",
                        source="upbit",
                        quantity=quantity,
                        avg_price=_to_float(coin.get("avg_buy_price"), default=0.0),
                        current_price=None,
                        evaluation_amount=None,
                        profit_loss=None,
                        profit_rate=None,
                        name=None,
                    )
                )

        if resolved_market in (None, "equity_kr", "equity_us"):
            kis = KISClient()

            if resolved_market in (None, "equity_kr"):
                kr_stocks = await kis.fetch_my_stocks()
                for stock in kr_stocks:
                    quantity = _to_float(stock.get("hldg_qty"))
                    if quantity <= 0:
                        continue
                    positions.append(
                        Position(
                            symbol=str(stock.get("pdno", "")).strip().upper(),
                            market="equity_kr",
                            source="kis",
                            quantity=quantity,
                            avg_price=_to_float(stock.get("pchs_avg_pric")),
                            current_price=_to_float(stock.get("prpr"), default=0.0)
                            or None,
                            evaluation_amount=_to_float(stock.get("evlu_amt")),
                            profit_loss=_to_float(stock.get("evlu_pfls_amt")),
                            profit_rate=_to_float(stock.get("evlu_pfls_rt")),
                            name=str(stock.get("prdt_name") or "").strip() or None,
                        )
                    )

            if resolved_market in (None, "equity_us"):
                us_stocks = await kis.fetch_my_us_stocks()
                for stock in us_stocks:
                    quantity = _to_float(stock.get("ovrs_cblc_qty"))
                    if quantity <= 0:
                        continue
                    positions.append(
                        Position(
                            symbol=str(stock.get("ovrs_pdno", "")).strip().upper(),
                            market="equity_us",
                            source="kis",
                            quantity=quantity,
                            avg_price=_to_float(stock.get("pchs_avg_pric")),
                            current_price=_to_float(stock.get("now_pric2"), default=0.0)
                            or None,
                            evaluation_amount=_to_float(
                                stock.get("ovrs_stck_evlu_amt")
                            ),
                            profit_loss=_to_float(stock.get("frcr_evlu_pfls_amt")),
                            profit_rate=_to_float(stock.get("evlu_pfls_rt")),
                            name=str(stock.get("ovrs_item_name") or "").strip() or None,
                        )
                    )

        return positions
    except Exception as exc:
        raise _map_error(exc) from exc


async def get_margin(market: str) -> MarginSnapshot:
    resolved_market = _normalize_market(market)
    if resolved_market is None:
        raise ValidationError("market is required")

    try:
        if resolved_market == "equity_kr":
            kis = KISClient()
            margin = await kis.inquire_integrated_margin()
            return MarginSnapshot(market=resolved_market, source="kis", details=margin)

        if resolved_market == "equity_us":
            kis = KISClient()
            rows = await kis.inquire_overseas_margin()
            usd_row = _select_usd_row(rows)
            return MarginSnapshot(
                market=resolved_market,
                source="kis",
                details=usd_row or {},
            )

        balances = await get_cash("crypto")
        cash = (
            balances[0]
            if balances
            else CashBalance(
                market="crypto",
                currency="KRW",
                balance=0.0,
                orderable=0.0,
                source="upbit",
            )
        )
        return MarginSnapshot(
            market="crypto",
            source="upbit",
            details={
                "currency": cash.currency,
                "balance": cash.balance,
                "orderable": cash.orderable,
            },
        )
    except Exception as exc:
        raise _map_error(exc) from exc


__all__ = [
    "get_cash",
    "get_positions",
    "get_margin",
    "CashBalance",
    "Position",
    "MarginSnapshot",
]
