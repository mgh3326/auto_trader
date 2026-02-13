"""Portfolio tool registration for MCP."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.core.db import AsyncSessionLocal
from app.mcp_server.env_utils import _env_int
from app.mcp_server.tick_size import adjust_tick_size_kr
from app.mcp_server.tooling.fundamentals import _get_support_resistance_impl
from app.mcp_server.tooling.market_data import (
    _compute_dca_price_levels,
    _compute_indicators,
    _compute_rsi_weights,
    _fetch_ohlcv_for_indicators,
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.orders import (
    _place_order_impl,
)
from app.mcp_server.tooling.shared import (
    _DEFAULT_MINIMUM_VALUES,
    _INSTRUMENT_TO_MARKET,
    _MCP_DCA_USER_ID,
    _MCP_USER_ID,
    _UPBIT_TICKER_BATCH_SIZE,
    _build_holdings_summary,
    _canonical_account_id,
    _format_filter_threshold,
    _instrument_to_manual_market_type,
    _is_position_symbol_match,
    _manual_market_to_instrument_type,
    _match_account_filter,
    _normalize_account_filter,
    _normalize_position_symbol,
    _parse_holdings_market_filter,
    _position_to_output,
    _recalculate_profit_fields,
    _resolve_market_type,
    _to_float,
    _value_for_minimum_filter,
    logger,
)
from app.models.dca_plan import DcaPlan, DcaPlanStep, DcaStepStatus
from app.services import upbit as upbit_service
from app.services.dca_service import DcaService
from app.services.kis import KISClient
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.screenshot_holdings_service import ScreenshotHoldingsService
from data.coins_info import get_or_refresh_maps

if TYPE_CHECKING:
    from fastmcp import FastMCP

PORTFOLIO_TOOL_NAMES: set[str] = {
    "get_holdings",
    "get_position",
    "get_cash_balance",
    "simulate_avg_cost",
    "update_manual_holdings",
    "create_dca_plan",
    "get_dca_status",
}


def _is_us_nation_name(value: Any) -> bool:
    normalized = str(value or "").strip().casefold()
    return normalized in {
        "미국",
        "us",
        "usa",
        "united states",
        "united states of america",
    }


def _extract_usd_orderable_from_row(row: dict[str, Any] | None) -> float:
    if not isinstance(row, dict):
        return 0.0
    return _to_float(row.get("frcr_gnrl_ord_psbl_amt"), default=0.0)


def _select_usd_row_for_us_order(
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
        (row for row in usd_rows if _is_us_nation_name(row.get("natn_name"))), None
    )
    if us_row is not None:
        return us_row

    return max(usd_rows, key=_extract_usd_orderable_from_row)


async def _collect_kis_positions(
    market_filter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if market_filter == "crypto":
        return [], []

    positions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    kis = KISClient()

    if market_filter in (None, "equity_kr"):
        try:
            kr_stocks = await kis.fetch_my_stocks()
            for stock in kr_stocks:
                quantity = _to_float(stock.get("hldg_qty"))
                if quantity <= 0:
                    continue

                positions.append(
                    {
                        "account": "kis",
                        "account_name": "기본 계좌",
                        "broker": "kis",
                        "source": "kis_api",
                        "instrument_type": "equity_kr",
                        "market": "kr",
                        "symbol": _normalize_position_symbol(
                            str(stock.get("pdno", "")),
                            "equity_kr",
                        ),
                        "name": stock.get("prdt_name") or stock.get("pdno"),
                        "quantity": quantity,
                        "avg_buy_price": _to_float(stock.get("pchs_avg_pric")),
                        "current_price": _to_float(stock.get("prpr"), default=0.0)
                        or None,
                        "evaluation_amount": _to_float(stock.get("evlu_amt")),
                        "profit_loss": _to_float(stock.get("evlu_pfls_amt")),
                        "profit_rate": _to_float(stock.get("evlu_pfls_rt")),
                    }
                )
        except Exception as exc:
            errors.append({"source": "kis", "market": "kr", "error": str(exc)})

    if market_filter in (None, "equity_us"):
        try:
            us_stocks = await kis.fetch_my_us_stocks()
            for stock in us_stocks:
                quantity = _to_float(stock.get("ovrs_cblc_qty"))
                if quantity <= 0:
                    continue

                positions.append(
                    {
                        "account": "kis",
                        "account_name": "기본 계좌",
                        "broker": "kis",
                        "source": "kis_api",
                        "instrument_type": "equity_us",
                        "market": "us",
                        "symbol": _normalize_position_symbol(
                            str(stock.get("ovrs_pdno", "")),
                            "equity_us",
                        ),
                        "name": stock.get("ovrs_item_name") or stock.get("ovrs_pdno"),
                        "quantity": quantity,
                        "avg_buy_price": _to_float(stock.get("pchs_avg_pric")),
                        "current_price": _to_float(stock.get("now_pric2"), default=0.0)
                        or None,
                        "evaluation_amount": _to_float(stock.get("ovrs_stck_evlu_amt")),
                        "profit_loss": _to_float(stock.get("frcr_evlu_pfls_amt")),
                        "profit_rate": _to_float(stock.get("evlu_pfls_rt")),
                    }
                )
        except Exception as exc:
            errors.append({"source": "kis", "market": "us", "error": str(exc)})

    return positions, errors


async def _collect_upbit_positions(
    market_filter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if market_filter not in (None, "crypto"):
        return [], []

    positions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        coin_name_map: dict[str, str] = {}
        try:
            crypto_maps = await get_or_refresh_maps()
            coin_name_map = crypto_maps.get("COIN_TO_NAME_KR", {}) or {}
        except Exception:
            coin_name_map = {}

        coins = await upbit_service.fetch_my_coins()
        for coin in coins:
            currency = str(coin.get("currency", "")).upper().strip()
            if not currency or currency == "KRW":
                continue

            quantity = _to_float(coin.get("balance")) + _to_float(coin.get("locked"))
            if quantity <= 0:
                continue

            unit_currency = str(coin.get("unit_currency", "KRW")).upper().strip()
            symbol = _normalize_position_symbol(
                f"{unit_currency or 'KRW'}-{currency}",
                "crypto",
            )

            positions.append(
                {
                    "account": "upbit",
                    "account_name": "기본 계좌",
                    "broker": "upbit",
                    "source": "upbit_api",
                    "instrument_type": "crypto",
                    "market": "crypto",
                    "symbol": symbol,
                    "name": coin_name_map.get(currency, symbol),
                    "quantity": quantity,
                    "avg_buy_price": _to_float(coin.get("avg_buy_price")),
                    "current_price": None,
                    "evaluation_amount": None,
                    "profit_loss": None,
                    "profit_rate": None,
                }
            )
    except Exception as exc:
        errors.append({"source": "upbit", "market": "crypto", "error": str(exc)})

    return positions, errors


async def _collect_manual_positions(
    *,
    user_id: int,
    market_filter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    positions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        manual_market = _instrument_to_manual_market_type(market_filter)
        async with AsyncSessionLocal() as db:
            service = ManualHoldingsService(db)
            holdings = await service.get_holdings_by_user(
                user_id=user_id,
                market_type=manual_market,
            )

        for holding in holdings:
            instrument_type = _manual_market_to_instrument_type(holding.market_type)
            symbol = _normalize_position_symbol(holding.ticker, instrument_type)
            quantity = _to_float(holding.quantity)
            if quantity <= 0:
                continue

            broker = holding.broker_account.broker_type
            account_name = holding.broker_account.account_name
            account = _canonical_account_id(broker, account_name)

            positions.append(
                {
                    "account": account,
                    "account_name": account_name or "기본 계좌",
                    "broker": broker,
                    "source": "manual",
                    "instrument_type": instrument_type,
                    "market": _INSTRUMENT_TO_MARKET[instrument_type],
                    "symbol": symbol,
                    "name": holding.display_name or symbol,
                    "quantity": quantity,
                    "avg_buy_price": _to_float(holding.avg_price),
                    "current_price": None,
                    "evaluation_amount": None,
                    "profit_loss": None,
                    "profit_rate": None,
                }
            )
    except Exception as exc:
        errors.append({"source": "manual_holdings", "error": str(exc)})

    return positions, errors


async def _fetch_price_map_for_positions(
    positions: list[dict[str, Any]],
) -> tuple[
    dict[tuple[str, str], float], list[dict[str, Any]], dict[tuple[str, str], str]
]:
    price_map: dict[tuple[str, str], float] = {}
    price_errors: list[dict[str, Any]] = []
    error_map: dict[tuple[str, str], str] = {}

    crypto_symbols = sorted(
        {
            _normalize_position_symbol(position["symbol"], "crypto")
            for position in positions
            if position["instrument_type"] == "crypto"
        }
    )

    if crypto_symbols:
        valid_symbols = list(crypto_symbols)
        try:
            tradable_markets = await upbit_service.fetch_all_market_codes(fiat=None)
            tradable_set = {str(market).upper() for market in tradable_markets}
            valid_symbols = [
                symbol for symbol in crypto_symbols if symbol.upper() in tradable_set
            ]
        except Exception as exc:
            price_errors.append(
                {
                    "source": "upbit",
                    "market": "crypto",
                    "stage": "current_price",
                    "error": f"failed to load tradable market list: {exc}",
                }
            )

        for offset in range(0, len(valid_symbols), _UPBIT_TICKER_BATCH_SIZE):
            batch_symbols = valid_symbols[offset : offset + _UPBIT_TICKER_BATCH_SIZE]
            try:
                prices = await upbit_service.fetch_multiple_current_prices(batch_symbols)
                for symbol in batch_symbols:
                    price = prices.get(symbol)
                    if price is not None:
                        price_map[("crypto", symbol.upper())] = float(price)
                missing_symbols = [
                    symbol
                    for symbol in batch_symbols
                    if ("crypto", symbol.upper()) not in price_map
                ]
                for symbol in missing_symbols:
                    error_msg = "price missing in batch ticker response"
                    error_map[("crypto", symbol.upper())] = error_msg
                    price_errors.append(
                        {
                            "source": "upbit",
                            "market": "crypto",
                            "symbol": symbol,
                            "stage": "current_price",
                            "error": error_msg,
                        }
                    )
            except Exception as exc:
                for symbol in batch_symbols:
                    if ("crypto", symbol.upper()) in price_map:
                        continue
                    error_msg = str(exc)
                    error_map[("crypto", symbol.upper())] = error_msg
                    price_errors.append(
                        {
                            "source": "upbit",
                            "market": "crypto",
                            "symbol": symbol,
                            "stage": "current_price",
                            "error": error_msg,
                        }
                    )

        if price_errors:
            deduped: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for item in price_errors:
                key = (item.get("symbol", ""), item.get("stage", ""))
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            price_errors = deduped

    async def fetch_equity_price(
        instrument_type: str, symbol: str
    ) -> tuple[str, str, float | None, str | None]:
        try:
            if instrument_type == "equity_kr":
                quote = await _fetch_quote_equity_kr(symbol)
            else:
                quote = await _fetch_quote_equity_us(symbol)
            price = quote.get("price")
            return (
                instrument_type,
                symbol,
                float(price) if price is not None else None,
                None,
            )
        except Exception as exc:
            error_msg = str(exc)
            logger.debug("Failed to fetch equity price for %s: %s", symbol, error_msg)
            return instrument_type, symbol, None, error_msg

    equity_tasks = [
        fetch_equity_price(instrument_type, symbol)
        for instrument_type, symbol in sorted(
            {
                (position["instrument_type"], position["symbol"])
                for position in positions
                if position["instrument_type"] in {"equity_kr", "equity_us"}
            }
        )
    ]

    if equity_tasks:
        results = await asyncio.gather(*equity_tasks)
        for instrument_type, symbol, price, error in results:
            if price is not None:
                price_map[(instrument_type, symbol)] = price
            elif error is not None:
                error_map[(instrument_type, symbol)] = error
                price_errors.append(
                    {
                        "source": "yahoo" if instrument_type == "equity_us" else "kis",
                        "market": "us" if instrument_type == "equity_us" else "kr",
                        "symbol": symbol,
                        "stage": "current_price",
                        "error": error,
                    }
                )

    return price_map, price_errors, error_map


async def _collect_portfolio_positions(
    *,
    account: str | None,
    market: str | None,
    include_current_price: bool,
    account_name: str | None = None,
    user_id: int = _MCP_USER_ID,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
    market_filter = _parse_holdings_market_filter(market)
    account_filter = _normalize_account_filter(account)

    tasks: list[Any] = []
    if market_filter != "crypto":
        tasks.append(_collect_kis_positions(market_filter))
    if market_filter in (None, "crypto"):
        tasks.append(_collect_upbit_positions(market_filter))
    tasks.append(_collect_manual_positions(user_id=user_id, market_filter=market_filter))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    positions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for result in results:
        if isinstance(result, Exception):
            errors.append({"source": "holdings", "error": str(result)})
            continue
        source_positions, source_errors = result
        positions.extend(source_positions)
        errors.extend(source_errors)

    if market_filter:
        positions = [
            position
            for position in positions
            if position["instrument_type"] == market_filter
        ]

    if account_filter:
        positions = [
            position
            for position in positions
            if _match_account_filter(position, account_filter)
        ]

    if account_name:
        account_name_filter = account_name.strip().lower()
        positions = [
            position
            for position in positions
            if account_name_filter in str(position.get("account_name", "")).lower()
        ]

    if include_current_price and positions:
        price_map, price_errors, error_map = await _fetch_price_map_for_positions(
            positions
        )
        errors.extend(price_errors)
        for position in positions:
            key = (position["instrument_type"], position["symbol"])
            price = price_map.get(key)
            if price is not None:
                position["current_price"] = price
                _recalculate_profit_fields(position)
            else:
                error = error_map.get(key)
                if error is not None:
                    position["price_error"] = error
    else:
        for position in positions:
            position["current_price"] = None
            position["evaluation_amount"] = None
            position["profit_loss"] = None
            position["profit_rate"] = None

    positions.sort(
        key=lambda position: (
            position["account"],
            position["market"],
            position["symbol"],
        )
    )
    return positions, errors, market_filter, account_filter


async def _get_indicators_impl(
    symbol: str,
    indicators: list[str],
    market: str | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    if not indicators:
        raise ValueError("indicators list is required and cannot be empty")

    market_type, symbol = _resolve_market_type(symbol, market)
    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
    source = source_map[market_type]

    try:
        df = await _fetch_ohlcv_for_indicators(symbol, market_type, count=250)
        if df.empty:
            raise ValueError(f"No data available for symbol '{symbol}'")

        current_price = float(df["close"].iloc[-1]) if "close" in df.columns else None
        indicator_results = _compute_indicators(df, indicators)

        return {
            "symbol": symbol,
            "price": current_price,
            "instrument_type": market_type,
            "source": source,
            "indicators": indicator_results,
        }
    except Exception as exc:
        return {
            "error": str(exc),
            "source": source,
            "symbol": symbol,
            "instrument_type": market_type,
        }


def register_portfolio_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_holdings",
        description=(
            "Get holdings grouped by account. Supports account filter "
            "(kis/upbit/toss/samsung_pension/isa) and market filter (kr/us/crypto). "
            "Cash balances are excluded. minimum_value filters out low-value positions "
            "when include_current_price=True. When minimum_value is None (default), "
            "per-currency thresholds are applied: KRW=5000, USD=10. "
            "Explicit number uses uniform threshold. Response includes filtered_count, "
            "filter_reason, and per-symbol price lookup errors."
        ),
    )
    async def get_holdings(
        account: str | None = None,
        market: str | None = None,
        include_current_price: bool = True,
        minimum_value: float | None = None,  # None = per-currency defaults
        account_name: str | None = None,
    ) -> dict[str, Any]:
        if minimum_value is not None and minimum_value < 0:
            raise ValueError("minimum_value must be >= 0")

        (
            positions,
            errors,
            resolved_market_filter,
            resolved_account_filter,
        ) = await _collect_portfolio_positions(
            account=account,
            market=market,
            include_current_price=include_current_price,
            account_name=account_name,
        )

        filtered_count = 0
        filter_reason: str | None = None

        if include_current_price and minimum_value is not None:
            threshold = float(minimum_value)
            filter_reason = f"minimum_value < {_format_filter_threshold(threshold)}"
            filtered_positions: list[dict[str, Any]] = []
            for position in positions:
                value = _value_for_minimum_filter(position)
                if value < threshold:
                    filtered_count += 1
                    continue
                filtered_positions.append(position)
            positions = filtered_positions
        elif include_current_price and minimum_value is None:
            threshold_map = _DEFAULT_MINIMUM_VALUES.copy()
            filter_reason_parts = []
            for instrument_type, threshold in threshold_map.items():
                filter_reason_parts.append(
                    f"{instrument_type} < {_format_filter_threshold(threshold)}"
                )
            filter_reason = ", ".join(filter_reason_parts)
            filtered_positions = []
            for position in positions:
                value = _value_for_minimum_filter(position)
                instrument_type = position.get("instrument_type")
                threshold = threshold_map.get(instrument_type, 0.0)
                if value < threshold:
                    filtered_count += 1
                    continue
                filtered_positions.append(position)
            positions = filtered_positions
        elif not include_current_price:
            filter_reason = "minimum_value filter skipped (include_current_price=False)"
        else:
            filter_reason = "minimum_value filter disabled"

        grouped_accounts: dict[str, dict[str, Any]] = {}
        for position in positions:
            account_id = position["account"]
            grouped = grouped_accounts.setdefault(
                account_id,
                {
                    "account": account_id,
                    "broker": position["broker"],
                    "account_name": position["account_name"],
                    "positions": [],
                },
            )
            grouped["positions"].append(_position_to_output(position))

        accounts = [grouped_accounts[key] for key in sorted(grouped_accounts.keys())]
        summary = _build_holdings_summary(positions, include_current_price)
        reported_minimum_value = (
            _DEFAULT_MINIMUM_VALUES.copy() if minimum_value is None else minimum_value
        )

        return {
            "filters": {
                "account": resolved_account_filter,
                "account_name": account_name,
                "market": _INSTRUMENT_TO_MARKET.get(resolved_market_filter),
                "include_current_price": include_current_price,
                "minimum_value": reported_minimum_value,
            },
            "filtered_count": filtered_count,
            "filter_reason": filter_reason,
            "total_accounts": len(accounts),
            "total_positions": len(positions),
            "summary": summary,
            "accounts": accounts,
            "errors": errors,
        }

    @mcp.tool(
        name="get_position",
        description=(
            "Check whether a symbol is currently held and return detailed positions "
            "across all accounts. If no position exists, returns status='미보유'."
        ),
    )
    async def get_position(
        symbol: str,
        market: str | None = None,
    ) -> dict[str, Any]:
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        parsed_market = _parse_holdings_market_filter(market)
        if parsed_market == "equity_us":
            query_symbol = _normalize_position_symbol(symbol, "equity_us")
        elif parsed_market == "equity_kr":
            query_symbol = _normalize_position_symbol(symbol, "equity_kr")
        elif parsed_market == "crypto":
            query_symbol = _normalize_position_symbol(symbol, "crypto")
        else:
            query_symbol = symbol.strip().upper()

        positions, errors, _, _ = await _collect_portfolio_positions(
            account=None,
            market=market,
            include_current_price=True,
        )

        matched_positions = [
            position
            for position in positions
            if _is_position_symbol_match(
                position_symbol=position["symbol"],
                query_symbol=query_symbol,
                instrument_type=position["instrument_type"],
            )
        ]

        if not matched_positions:
            return {
                "symbol": query_symbol,
                "market": _INSTRUMENT_TO_MARKET.get(parsed_market),
                "has_position": False,
                "status": "미보유",
                "position_count": 0,
                "positions": [],
                "errors": errors,
            }

        matched_positions.sort(
            key=lambda position: (
                position["account"],
                position["market"],
                position["symbol"],
            )
        )

        return {
            "symbol": query_symbol,
            "market": _INSTRUMENT_TO_MARKET.get(parsed_market),
            "has_position": True,
            "status": "보유",
            "position_count": len(matched_positions),
            "accounts": sorted({position["account"] for position in matched_positions}),
            "positions": [
                {
                    "account": position["account"],
                    "broker": position["broker"],
                    "account_name": position["account_name"],
                    **_position_to_output(position),
                }
                for position in matched_positions
            ],
            "errors": errors,
        }

    @mcp.tool(
        name="simulate_avg_cost",
        description=(
            "Simulate dollar-cost averaging / adding to a position. "
            "Given current holdings and additional buy plans, "
            "calculates the new average cost, breakeven %, and unrealised P&L."
        ),
    )
    async def simulate_avg_cost(
        holdings: dict[str, float],
        plans: list[dict[str, float]],
        current_market_price: float | None = None,
        target_price: float | None = None,
    ) -> dict[str, Any]:
        """Simulate averaging-down / dollar-cost averaging."""
        h_price = holdings.get("price")
        if h_price is None:
            h_price = holdings.get("avg_price")
        h_qty = holdings.get("quantity")
        if h_price is None or h_qty is None:
            raise ValueError(
                "holdings must contain 'price' (or 'avg_price') and 'quantity'"
            )
        h_price = float(h_price)
        h_qty = float(h_qty)
        if h_price < 0 or h_qty < 0:
            raise ValueError("holdings price and quantity must be >= 0")

        if not plans:
            raise ValueError("plans must contain at least one entry")
        validated_plans: list[tuple[float, float]] = []
        for i, p in enumerate(plans):
            pp = p.get("price")
            if pp is None:
                pp = p.get("avg_price")
            pq = p.get("quantity")
            if pp is None or pq is None:
                raise ValueError(
                    f"plans[{i}] must contain 'price' (or 'avg_price') and 'quantity'"
                )
            pp, pq = float(pp), float(pq)
            if pp <= 0 or pq <= 0:
                raise ValueError(f"plans[{i}] price and quantity must be > 0")
            validated_plans.append((pp, pq))

        mkt = float(current_market_price) if current_market_price is not None else None
        tp = float(target_price) if target_price is not None else None
        if tp is not None and tp <= 0:
            raise ValueError("target_price must be > 0")

        total_qty = h_qty
        total_invested_raw = h_price * h_qty
        avg_price_raw = (total_invested_raw / total_qty) if total_qty > 0 else None
        avg_price = round(avg_price_raw, 2) if avg_price_raw is not None else None

        current_position: dict[str, Any] = {
            "avg_price": avg_price,
            "total_quantity": total_qty,
            "total_invested": round(total_invested_raw, 2),
        }
        if mkt is not None and avg_price is not None:
            pnl = round((mkt - avg_price) * total_qty, 2)
            pnl_pct = round((mkt / avg_price - 1) * 100, 2)
            current_position["unrealized_pnl"] = pnl
            current_position["unrealized_pnl_pct"] = pnl_pct
            current_position["pnl_vs_current"] = pnl
            current_position["pnl_vs_current_pct"] = pnl_pct

        if tp is not None and avg_price is not None:
            projected_profit = round((tp - avg_price) * total_qty, 2)
            target_return_pct = round((tp / avg_price - 1) * 100, 2)
            current_position["target_profit"] = projected_profit
            current_position["target_return_pct"] = target_return_pct

        steps: list[dict[str, Any]] = []
        for idx, (bp, bq) in enumerate(validated_plans, start=1):
            total_invested_raw += bp * bq
            total_qty = round(total_qty + bq, 10)
            avg_price = round(total_invested_raw / total_qty, 2)

            step: dict[str, Any] = {
                "step": idx,
                "buy_price": bp,
                "buy_quantity": bq,
                "new_avg_price": avg_price,
                "total_quantity": total_qty,
                "total_invested": round(total_invested_raw, 2),
            }
            if mkt is not None:
                breakeven_pct = round((avg_price / mkt - 1) * 100, 2)
                pnl = round((mkt - avg_price) * total_qty, 2)
                pnl_pct = round((mkt / avg_price - 1) * 100, 2)
                step["breakeven_change_pct"] = breakeven_pct
                step["unrealized_pnl"] = pnl
                step["unrealized_pnl_pct"] = pnl_pct
                step["pnl_vs_current"] = pnl
                step["pnl_vs_current_pct"] = pnl_pct

            if tp is not None:
                target_profit = round((tp - avg_price) * total_qty, 2)
                target_return_pct = round((tp / avg_price - 1) * 100, 2)
                step["target_profit"] = target_profit
                step["target_return_pct"] = target_return_pct

            steps.append(step)

        result: dict[str, Any] = {
            "current_position": current_position,
            "steps": steps,
        }
        if mkt is not None:
            result["current_market_price"] = mkt

        if tp is not None and steps:
            final_avg_price = float(steps[-1]["new_avg_price"])
            profit_per_unit = round(tp - final_avg_price, 2)
            total_profit = round(profit_per_unit * total_qty, 2)
            total_return_pct = round((tp / final_avg_price - 1) * 100, 2)
            result["target_analysis"] = {
                "target_price": tp,
                "final_avg_price": final_avg_price,
                "profit_per_unit": profit_per_unit,
                "total_profit": total_profit,
                "total_return_pct": total_return_pct,
            }

        return result

    @mcp.tool(
        name="update_manual_holdings",
        description=(
            "Update manual holdings from parsed securities app screenshot data. "
            "Uses upsert by default and supports action='remove' for sold holdings."
        ),
    )
    async def update_manual_holdings(
        holdings: list[dict[str, Any]],
        broker: str = "toss",
        account_name: str = "기본 계좌",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if not holdings:
            return {
                "success": False,
                "error": "holdings list is required",
                "dry_run": dry_run,
            }

        try:
            async with AsyncSessionLocal() as db:
                service = ScreenshotHoldingsService(db)
                user_id = _env_int("MCP_USER_ID", 1)
                result = await service.resolve_and_update(
                    user_id=user_id,
                    holdings_data=holdings,
                    broker=broker,
                    account_name=account_name,
                    dry_run=dry_run,
                )
                return result
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "dry_run": dry_run,
                "broker": broker,
                "account_name": account_name,
            }

    @mcp.tool(
        name="create_dca_plan",
        description=(
            "Create a Dollar Cost Averaging (DCA) buying plan based on "
            "technical analysis. Uses support/resistance levels and RSI to "
            "determine optimal buying points. dry_run=True by default for safety. "
            "When dry_run=False, executes orders sequentially."
        ),
    )
    async def create_dca_plan(
        symbol: str,
        total_amount: float,
        splits: int = 3,
        strategy: str = "support",
        dry_run: bool = True,
        market: str | None = None,
        execute_steps: list[int] | None = None,
    ) -> dict[str, Any]:
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")
        if total_amount <= 0:
            raise ValueError("total_amount must be greater than 0")
        if not (2 <= splits <= 5):
            raise ValueError("splits must be between 2 and 5")

        valid_strategies = {"support", "equal", "aggressive"}
        if strategy not in valid_strategies:
            raise ValueError(
                f"Invalid strategy '{strategy}'. Must be one of: {', '.join(sorted(valid_strategies))}"
            )
        if execute_steps is not None:
            invalid_steps = [s for s in execute_steps if not (1 <= s <= splits)]
            if invalid_steps:
                raise ValueError(
                    f"execute_steps must be between 1 and {splits}, got: {invalid_steps}"
                )

        market_type, normalized_symbol = _resolve_market_type(symbol, market)
        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            _sr_fn = globals().get(
                "_get_support_resistance_impl",
                _get_support_resistance_impl,
            )
            sr_result = await _sr_fn(normalized_symbol, None)
            if "error" in sr_result:
                return {
                    "success": False,
                    "error": sr_result["error"],
                    "source": sr_result.get("source", "get_support_resistance"),
                    "dry_run": dry_run,
                }

            current_price = sr_result.get("current_price")
            supports = sr_result.get("supports", [])

            _ind_fn = globals().get("_get_indicators_impl", _get_indicators_impl)
            indicator_result = await _ind_fn(normalized_symbol, ["rsi"], None)
            if "error" in indicator_result:
                return {
                    "success": False,
                    "error": indicator_result["error"],
                    "source": indicator_result.get("source", "get_indicators"),
                    "dry_run": dry_run,
                }

            rsi_data = indicator_result.get("indicators", {}).get("rsi", {})
            rsi_value = rsi_data.get("14") if rsi_data else None

            price_levels = _compute_dca_price_levels(
                strategy,
                splits,
                current_price,
                supports,
            )
            weights = _compute_rsi_weights(rsi_value, splits)

            plans: list[dict[str, Any]] = []
            total_quantity = 0.0
            for step, (level, weight) in enumerate(
                zip(price_levels, weights, strict=True),
                start=1,
            ):
                step_amount = total_amount * weight
                step_price = level["price"]
                level_source = level["source"]

                if market_type == "equity_kr":
                    original_price = step_price
                    step_price = adjust_tick_size_kr(step_price, "buy")
                    tick_adjusted = step_price != original_price
                else:
                    original_price = None
                    tick_adjusted = False

                if market_type == "crypto":
                    quantity = step_amount / step_price
                else:
                    quantity = int(step_amount / step_price)
                    if quantity == 0:
                        return {
                            "success": False,
                            "error": (
                                f"Amount {step_amount:.0f} is insufficient for 1 unit at price {step_price}"
                            ),
                            "dry_run": dry_run,
                        }

                total_quantity += quantity
                distance_pct = round((step_price - current_price) / current_price * 100, 2)

                plans.append(
                    {
                        "step": step,
                        "price": round(step_price, 2),
                        "distance_pct": distance_pct,
                        "amount": round(step_amount, 0),
                        "quantity": round(quantity, 8)
                        if market_type == "crypto"
                        else quantity,
                        "source": level_source,
                    }
                )
                if tick_adjusted and original_price is not None:
                    plans[-1]["original_price"] = round(original_price, 2)
                    plans[-1]["tick_adjusted"] = True

            avg_target_price = sum(p["price"] for p in plans) / len(plans)
            min_dist = min(p["distance_pct"] for p in plans)
            max_dist = max(p["distance_pct"] for p in plans)
            summary = {
                "symbol": normalized_symbol,
                "current_price": current_price,
                "rsi_14": rsi_value,
                "strategy": strategy,
                "total_amount": total_amount,
                "avg_target_price": round(avg_target_price, 2),
                "total_quantity": round(total_quantity, 8)
                if market_type == "crypto"
                else int(total_quantity),
                "price_range_pct": f"{min_dist:.2f}% ~ {max_dist:.2f}%",
                "weight_mode": (
                    "front_heavy"
                    if rsi_value is not None and rsi_value < 30
                    else "back_heavy"
                    if rsi_value is not None and rsi_value > 50
                    else "equal"
                ),
            }

            execution_results: list[dict[str, Any]] = []
            executed_steps: list[int] = []
            should_execute = not dry_run or (execute_steps is not None)

            plan_id: int | None = None
            created_plan_steps: dict[int, DcaPlanStep] = {}
            try:
                async with AsyncSessionLocal() as db:
                    dca_service = DcaService(db)
                    plans_for_db = [
                        {
                            "step": p["step"],
                            "price": p["price"],
                            "amount": p["amount"],
                            "quantity": p["quantity"],
                            "source": p.get("source"),
                        }
                        for p in plans
                    ]
                    created_plan = await dca_service.create_plan(
                        user_id=_MCP_DCA_USER_ID,
                        symbol=normalized_symbol,
                        market=market_type,
                        total_amount=total_amount,
                        splits=splits,
                        strategy=strategy,
                        plans_data=plans_for_db,
                        rsi_14=rsi_value,
                    )
                    plan_id = created_plan.id

                    async with AsyncSessionLocal() as reload_db:
                        reload_service = DcaService(reload_db)
                        reloaded_plan = await reload_service.get_plan(
                            plan_id,
                            _MCP_DCA_USER_ID,
                        )
                        if not reloaded_plan:
                            raise ValueError(f"Plan {plan_id} not found after creation")
                        for step in reloaded_plan.steps or []:
                            created_plan_steps[step.step_number] = step
            except Exception as exc:
                logger.error("Failed to persist DCA plan: %s", exc)
                return {
                    "success": False,
                    "error": f"Failed to persist DCA plan: {exc}",
                    "dry_run": not should_execute,
                    "executed": False,
                    "plan_id": None,
                }

            if should_execute:
                for plan_step in plans:
                    if execute_steps is not None and plan_step["step"] not in execute_steps:
                        continue

                    order_amount = plan_step["amount"]
                    order_price = plan_step["price"]
                    if order_amount > 1_000_000:
                        return {
                            "success": False,
                            "error": (
                                f"Step {plan_step['step']} amount {order_amount:.0f} KRW exceeds limit 1,000,000 KRW"
                            ),
                            "dry_run": not should_execute,
                            "executed": bool(executed_steps),
                            "plan_id": plan_id,
                            "summary": summary,
                        }

                    _po_fn = globals().get("_place_order_impl", _place_order_impl)
                    order_result = await _po_fn(
                        symbol=normalized_symbol,
                        side="buy",
                        order_type="limit",
                        amount=order_amount,
                        price=order_price,
                        dry_run=False,
                        reason=f"DCA plan step {plan_step['step']}/{splits}",
                    )

                    execution_results.append(
                        {
                            "step": plan_step["step"],
                            "success": order_result.get("success", False),
                            "result": order_result,
                        }
                    )
                    executed_steps.append(plan_step["step"])

                    if order_result.get("success") and plan_id is not None:
                        order_id = None
                        if "order_id" in order_result:
                            order_id = order_result["order_id"]
                        elif "execution" in order_result and isinstance(
                            order_result["execution"],
                            dict,
                        ):
                            order_id = (
                                order_result["execution"].get("uuid")
                                or order_result["execution"].get("ord_no")
                                or order_result["execution"].get("odno")
                            )

                        if order_id and plan_step["step"] in created_plan_steps:
                            try:
                                async with AsyncSessionLocal() as db:
                                    dca_service = DcaService(db)
                                    step = created_plan_steps[plan_step["step"]]
                                    await dca_service.mark_step_ordered(
                                        step.id,
                                        str(order_id),
                                    )
                            except Exception as exc:
                                logger.error("Failed to mark step ordered: %s", exc)
                                return {
                                    "success": False,
                                    "error": f"Failed to mark step ordered: {exc}",
                                    "dry_run": not should_execute,
                                    "executed": bool(executed_steps),
                                    "plan_id": plan_id,
                                    "execution_results": execution_results,
                                    "summary": summary,
                                }
                        elif order_id:
                            logger.error(
                                "Step %s not found in plan %s - available steps: %s",
                                plan_step["step"],
                                plan_id,
                                list(created_plan_steps.keys()),
                            )
                            return {
                                "success": False,
                                "error": f"Step {plan_step['step']} not found in plan {plan_id}",
                                "dry_run": not should_execute,
                                "executed": bool(executed_steps),
                                "plan_id": plan_id,
                                "execution_results": execution_results,
                                "summary": summary,
                            }

                    if not order_result.get("success"):
                        return {
                            "success": False,
                            "error": f"Order failed at step {plan_step['step']}",
                            "failed_step": plan_step["step"],
                            "dry_run": not should_execute,
                            "executed": bool(executed_steps),
                            "plan_id": plan_id,
                            "execution_results": execution_results,
                            "summary": summary,
                        }

            response: dict[str, Any] = {
                "success": True,
                "dry_run": not should_execute,
                "executed": bool(executed_steps),
                "plan_id": plan_id,
                "plans": plans,
                "summary": summary,
            }
            if should_execute:
                response["execution_results"] = execution_results
            if should_execute and executed_steps:
                response["executed_steps"] = executed_steps
            return response

        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "source": source,
                "dry_run": dry_run,
            }

    async def _get_dca_status_impl(
        plan_id: int | None = None,
        symbol: str | None = None,
        status: str = "active",
        limit: int = 10,
    ) -> dict[str, Any]:
        valid_statuses = {"active", "completed", "cancelled", "expired", "all"}
        if status not in valid_statuses:
            return {
                "success": False,
                "error": (
                    f"Invalid status '{status}'. Must be one of: {', '.join(sorted(valid_statuses))}"
                ),
                "plans": [],
                "total_plans": 0,
            }
        if limit < 1 or limit > 1000:
            return {
                "success": False,
                "error": f"limit must be between 1 and 1000, got: {limit}",
                "plans": [],
                "total_plans": 0,
            }

        try:
            async with AsyncSessionLocal() as db:
                dca_service = DcaService(db)
                plans: list[DcaPlan] = []

                if plan_id is not None:
                    plan = await dca_service.get_plan(plan_id, _MCP_DCA_USER_ID)
                    if plan:
                        plans = [plan]
                elif symbol is not None:
                    symbol = symbol.strip()
                    if status == "all":
                        plans = await dca_service.get_plans_by_status(
                            user_id=_MCP_DCA_USER_ID,
                            symbol=symbol,
                            status=None,
                            limit=limit,
                        )
                    else:
                        plans = await dca_service.get_plans_by_status(
                            user_id=_MCP_DCA_USER_ID,
                            symbol=symbol,
                            status=status,
                            limit=limit,
                        )
                else:
                    if status == "all":
                        plans = await dca_service.get_plans_by_status(
                            user_id=_MCP_DCA_USER_ID,
                            status=None,
                            limit=limit,
                        )
                    else:
                        plans = await dca_service.get_plans_by_status(
                            user_id=_MCP_DCA_USER_ID,
                            status=status,
                            limit=limit,
                        )

                def _format_dca_plan(plan: DcaPlan) -> dict[str, Any]:
                    p: dict[str, Any] = {
                        "plan_id": plan.id,
                        "id": plan.id,
                        "user_id": plan.user_id,
                        "symbol": plan.symbol,
                        "market": plan.market,
                        "status": plan.status.value
                        if hasattr(plan.status, "value")
                        else str(plan.status),
                        "total_amount": float(plan.total_amount)
                        if getattr(plan, "total_amount", None) is not None
                        else None,
                        "splits": plan.splits,
                        "strategy": plan.strategy,
                        "rsi_14": float(plan.rsi_14) if plan.rsi_14 is not None else None,
                        "created_at": plan.created_at.isoformat() if plan.created_at else None,
                        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
                        "completed_at": plan.completed_at.isoformat()
                        if plan.completed_at
                        else None,
                    }

                    steps_list: list[dict[str, Any]] = []
                    total_steps = 0
                    counts = {
                        "filled": 0,
                        "ordered": 0,
                        "pending": 0,
                        "cancelled": 0,
                        "partial": 0,
                        "skipped": 0,
                    }
                    invested = 0.0
                    filled_qty_total = 0.0
                    filled_price_weighted = 0.0

                    if hasattr(plan, "steps") and plan.steps:
                        for step in plan.steps:
                            total_steps += 1
                            status_name = (
                                step.status.value
                                if hasattr(step.status, "value")
                                else str(step.status)
                            )
                            if status_name == DcaStepStatus.FILLED.value:
                                counts["filled"] += 1
                            elif status_name == DcaStepStatus.ORDERED.value:
                                counts["ordered"] += 1
                            elif status_name == DcaStepStatus.PENDING.value:
                                counts["pending"] += 1
                            elif status_name == DcaStepStatus.CANCELLED.value:
                                counts["cancelled"] += 1
                            elif status_name == DcaStepStatus.PARTIAL.value:
                                counts["partial"] += 1
                            elif status_name == DcaStepStatus.SKIPPED.value:
                                counts["skipped"] += 1

                            filled_amount = (
                                float(step.filled_amount)
                                if getattr(step, "filled_amount", None) is not None
                                else 0.0
                            )
                            filled_qty = (
                                float(step.filled_quantity)
                                if getattr(step, "filled_quantity", None) is not None
                                else 0.0
                            )
                            filled_price = (
                                float(step.filled_price)
                                if getattr(step, "filled_price", None) is not None
                                else None
                            )
                            invested += filled_amount
                            if filled_qty and filled_price is not None:
                                filled_qty_total += filled_qty
                                filled_price_weighted += filled_price * filled_qty

                            ordered_at_val = getattr(step, "ordered_at", None)
                            filled_at_val = getattr(step, "filled_at", None)
                            steps_list.append(
                                {
                                    "id": step.id,
                                    "plan_id": step.plan_id,
                                    "step": step.step_number,
                                    "step_number": step.step_number,
                                    "target_price": float(step.target_price)
                                    if getattr(step, "target_price", None) is not None
                                    else None,
                                    "target_amount": float(step.target_amount)
                                    if getattr(step, "target_amount", None) is not None
                                    else None,
                                    "target_quantity": float(step.target_quantity)
                                    if getattr(step, "target_quantity", None) is not None
                                    else None,
                                    "status": status_name,
                                    "order_id": step.order_id,
                                    "ordered_at": ordered_at_val.isoformat()
                                    if ordered_at_val is not None
                                    else None,
                                    "filled_price": float(step.filled_price)
                                    if getattr(step, "filled_price", None) is not None
                                    else None,
                                    "filled_quantity": float(step.filled_quantity)
                                    if getattr(step, "filled_quantity", None) is not None
                                    else None,
                                    "filled_amount": float(step.filled_amount)
                                    if getattr(step, "filled_amount", None) is not None
                                    else None,
                                    "filled_at": filled_at_val.isoformat()
                                    if filled_at_val is not None
                                    else None,
                                    "level_source": getattr(step, "level_source", None),
                                }
                            )

                    avg_filled_price = None
                    if filled_qty_total > 0:
                        avg_filled_price = filled_price_weighted / filled_qty_total

                    remaining = None
                    if p.get("total_amount") is not None:
                        remaining = float(p["total_amount"]) - invested

                    p["steps"] = steps_list
                    p["progress"] = {
                        "total_steps": total_steps,
                        "filled": counts["filled"],
                        "ordered": counts["ordered"],
                        "pending": counts["pending"],
                        "cancelled": counts["cancelled"],
                        "partial": counts["partial"],
                        "skipped": counts["skipped"],
                        "invested": round(invested, 2),
                        "remaining": round(remaining, 2) if remaining is not None else None,
                        "avg_filled_price": round(avg_filled_price, 8)
                        if avg_filled_price is not None
                        else None,
                    }
                    return p

                formatted_plans = [_format_dca_plan(plan) for plan in plans]
                return {
                    "success": True,
                    "plans": formatted_plans,
                    "total_plans": len(formatted_plans),
                }
        except ValueError as ve:
            return {
                "success": False,
                "error": str(ve),
                "plans": [],
                "total_plans": 0,
            }
        except Exception as exc:
            logger.error("Error fetching DCA status: %s", exc)
            return {
                "success": False,
                "error": str(exc),
                "plans": [],
                "total_plans": 0,
            }

    @mcp.tool(
        name="get_dca_status",
        description=(
            "Get status of DCA (Dollar Cost Averaging) plans. "
            "Supports filtering by: plan_id (exact match), "
            "symbol + status (for symbol's plans), or just status. "
            "Response always includes total_plans count."
        ),
    )
    async def get_dca_status(
        plan_id: int | None = None,
        symbol: str | None = None,
        status: str = "active",
        limit: int = 10,
    ) -> dict[str, Any]:
        _impl = globals().get("_get_dca_status_impl", _get_dca_status_impl)
        return await _impl(plan_id, symbol, status, limit)

    @mcp.tool(
        name="get_cash_balance",
        description=(
            "Query available cash balances from all accounts. "
            "Supports Upbit (KRW), KIS domestic (KRW), and KIS overseas (USD). "
            "Returns detailed balance information including orderable amounts."
        ),
    )
    async def get_cash_balance(account: str | None = None) -> dict[str, Any]:
        """Query available cash balances from all accounts."""
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
                errors.append(
                    {"source": "upbit", "market": "crypto", "error": str(exc)}
                )

        if account_filter is None or account_filter in (
            "kis",
            "kis_domestic",
            "kis_overseas",
        ):
            kis = KISClient()

            if account_filter is None or account_filter in ("kis", "kis_domestic"):
                try:
                    domestic_data = await kis.inquire_domestic_cash_balance()
                    dncl_amt = float(domestic_data.get("dnca_tot_amt", 0) or 0)
                    orderable = float(
                        domestic_data.get("stck_cash_ord_psbl_amt", 0) or 0
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
                    usd_margin = _select_usd_row_for_us_order(overseas_margin_data)
                    if usd_margin is None:
                        raise RuntimeError(
                            "USD margin data not found in KIS overseas margin"
                        )

                    balance = _to_float(
                        usd_margin.get("frcr_dncl_amt1")
                        or usd_margin.get("frcr_dncl_amt_2"),
                        default=0.0,
                    )
                    orderable = _extract_usd_orderable_from_row(usd_margin)

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

    # Export for backward-compatible monkeypatching from app.mcp_server.tools
    globals()["_get_dca_status_impl"] = _get_dca_status_impl

__all__ = ["PORTFOLIO_TOOL_NAMES", "register_portfolio_tools"]
