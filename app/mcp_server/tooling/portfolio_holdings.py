"""Portfolio tool registration for MCP."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.core.db import AsyncSessionLocal
from app.mcp_server.env_utils import _env_int
from app.mcp_server.tooling.fundamentals_handlers import _get_support_resistance_impl
from app.mcp_server.tooling.market_data_indicators import (
    _compute_indicators,
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.market_data_quotes import (
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.orders_history import (
    _place_order_impl,
)
from app.mcp_server.tooling.portfolio_cash import (
    get_cash_balance_impl as _get_cash_balance_impl,
)
from app.mcp_server.tooling.portfolio_dca_core import (
    create_dca_plan_impl,
    simulate_avg_cost_impl,
)
from app.mcp_server.tooling.portfolio_dca_status import (
    get_dca_status_impl as _get_dca_status_impl,
)
from app.mcp_server.tooling.shared import (
    DEFAULT_MINIMUM_VALUES as _DEFAULT_MINIMUM_VALUES,
)
from app.mcp_server.tooling.shared import (
    INSTRUMENT_TO_MARKET as _INSTRUMENT_TO_MARKET,
)
from app.mcp_server.tooling.shared import MCP_DCA_USER_ID as _MCP_DCA_USER_ID
from app.mcp_server.tooling.shared import MCP_USER_ID as _MCP_USER_ID
from app.mcp_server.tooling.shared import (
    UPBIT_TICKER_BATCH_SIZE as _UPBIT_TICKER_BATCH_SIZE,
)
from app.mcp_server.tooling.shared import (
    build_holdings_summary as _build_holdings_summary,
)
from app.mcp_server.tooling.shared import (
    canonical_account_id as _canonical_account_id,
)
from app.mcp_server.tooling.shared import (
    format_filter_threshold as _format_filter_threshold,
)
from app.mcp_server.tooling.shared import (
    instrument_to_manual_market_type as _instrument_to_manual_market_type,
)
from app.mcp_server.tooling.shared import (
    is_position_symbol_match as _is_position_symbol_match,
)
from app.mcp_server.tooling.shared import (
    logger,
)
from app.mcp_server.tooling.shared import (
    manual_market_to_instrument_type as _manual_market_to_instrument_type,
)
from app.mcp_server.tooling.shared import (
    match_account_filter as _match_account_filter,
)
from app.mcp_server.tooling.shared import (
    normalize_account_filter as _normalize_account_filter,
)
from app.mcp_server.tooling.shared import (
    normalize_position_symbol as _normalize_position_symbol,
)
from app.mcp_server.tooling.shared import (
    parse_holdings_market_filter as _parse_holdings_market_filter,
)
from app.mcp_server.tooling.shared import (
    position_to_output as _position_to_output,
)
from app.mcp_server.tooling.shared import (
    recalculate_profit_fields as _recalculate_profit_fields,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.mcp_server.tooling.shared import (
    to_float as _to_float,
)
from app.mcp_server.tooling.shared import (
    value_for_minimum_filter as _value_for_minimum_filter,
)
from app.monitoring.tracing_spans import traced_await
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
            holdings = await traced_await(
                service.get_holdings_by_user(
                    user_id=user_id,
                    market_type=manual_market,
                ),
                op="db.service",
                name="manual_holdings.get_holdings_by_user",
                data={"user_id": user_id, "market_filter": market_filter},
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
                prices = await upbit_service.fetch_multiple_current_prices(
                    batch_symbols
                )
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
    tasks.append(
        _collect_manual_positions(user_id=user_id, market_filter=market_filter)
    )

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


def _register_portfolio_tools_impl(mcp: FastMCP) -> None:
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
        return await simulate_avg_cost_impl(
            holdings=holdings,
            plans=plans,
            current_market_price=current_market_price,
            target_price=target_price,
        )

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
                result = await traced_await(
                    service.resolve_and_update(
                        user_id=user_id,
                        holdings_data=holdings,
                        broker=broker,
                        account_name=account_name,
                        dry_run=dry_run,
                    ),
                    op="db.service",
                    name="screenshot_holdings.resolve_and_update",
                    data={
                        "user_id": user_id,
                        "broker": broker,
                        "account_name": account_name,
                        "dry_run": dry_run,
                        "holdings_count": len(holdings),
                    },
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
        return await create_dca_plan_impl(
            symbol=symbol,
            total_amount=total_amount,
            splits=splits,
            strategy=strategy,
            dry_run=dry_run,
            market=market,
            execute_steps=execute_steps,
            resolve_market_type=_resolve_market_type,
            sr_impl=_get_support_resistance_impl,
            indicators_impl=_get_indicators_impl,
            place_order_impl=_place_order_impl,
            dca_service_factory=lambda db: DcaService(db),
            session_factory=AsyncSessionLocal,
            logger_obj=logger,
            mcp_dca_user_id=_MCP_DCA_USER_ID,
        )

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
        return await _get_dca_status_impl(
            plan_id=plan_id,
            symbol=symbol,
            status=status,
            limit=limit,
            session_factory=AsyncSessionLocal,
            dca_service_factory=lambda db: DcaService(db),
            logger_obj=logger,
            mcp_dca_user_id=_MCP_DCA_USER_ID,
        )

    @mcp.tool(
        name="get_cash_balance",
        description=(
            "Query available cash balances from all accounts. "
            "Supports Upbit (KRW), KIS domestic (KRW), and KIS overseas (USD). "
            "Returns detailed balance information including orderable amounts."
        ),
    )
    async def get_cash_balance(account: str | None = None) -> dict[str, Any]:
        return await _get_cash_balance_impl(account=account)


__all__ = [
    "PORTFOLIO_TOOL_NAMES",
    "_register_portfolio_tools_impl",
    "_collect_portfolio_positions",
    "_get_indicators_impl",
]
