"""Order tool registration for MCP."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from app.mcp_server.tick_size import adjust_tick_size_kr
from app.mcp_server.tooling import order_execution as _order_execution
from app.mcp_server.tooling.orders_modify_cancel import (
    _extract_kis_order_number,
    _get_kis_field,
    _normalize_kis_domestic_order,
    _normalize_kis_overseas_order,
    _normalize_upbit_order,
)
from app.mcp_server.tooling.shared import (
    normalize_market as _normalize_market,
    parse_holdings_market_filter as _parse_holdings_market_filter,
    resolve_market_type as _resolve_market_type,
    logger,
)
from app.services import upbit as upbit_service
from app.services.kis import KISClient
from data.stocks_info.overseas_us_stocks import get_exchange_by_symbol

if TYPE_CHECKING:
    from fastmcp import FastMCP

ORDER_TOOL_NAMES: set[str] = {
    "place_order",
    "modify_order",
    "cancel_order",
    "get_order_history",
}

_calculate_date_range = _order_execution._calculate_date_range
_normalize_market_type_to_external = _order_execution._normalize_market_type_to_external
_get_current_price_for_order = _order_execution._get_current_price_for_order
_get_holdings_for_order = _order_execution._get_holdings_for_order
_get_balance_for_order = _order_execution._get_balance_for_order
_check_daily_order_limit = _order_execution._check_daily_order_limit
_record_order_history = _order_execution._record_order_history
_preview_order = _order_execution._preview_order
_execute_order = _order_execution._execute_order
_place_order_impl = _order_execution._place_order_impl


def _calculate_order_summary(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate summary statistics for orders."""
    total_orders = len(orders)
    filled = sum(1 for o in orders if o.get("status") == "filled")
    pending = sum(1 for o in orders if o.get("status") == "pending")
    partial = sum(1 for o in orders if o.get("status") == "partial")
    cancelled = sum(1 for o in orders if o.get("status") == "cancelled")

    return {
        "total_orders": total_orders,
        "filled": filled,
        "pending": pending,
        "partial": partial,
        "cancelled": cancelled,
    }


def _register_order_tools_impl(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_order_history",
        description=(
            "Get order history for a symbol. Supports Upbit (crypto) and KIS "
            "(KR/US equities). Returns normalized order information."
        ),
    )
    async def get_order_history(
        symbol: str | None = None,
        status: Literal["all", "pending", "filled", "cancelled"] = "all",
        order_id: str | None = None,
        market: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int | None = 50,
    ) -> dict[str, Any]:
        if status != "pending" and not symbol:
            raise ValueError(
                f"symbol is required when status='{status}'. "
                "Use status='pending' for symbol-free queries, or provide a symbol (e.g. symbol='KRW-BTC')."
            )

        symbol = (symbol or "").strip() or None
        order_id = (order_id or "").strip() or None
        market_hint = (market or "").strip().lower() or None
        side = (side or "").strip().lower() or None

        if side and side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")

        if limit is None:
            limit = 50
        elif limit < -1:
            raise ValueError("limit must be >= -1")

        limit_val = limit if limit not in (0, -1) else float("inf")
        effective_days = days

        market_types: list[str] = []
        normalized_symbol: str | None = None

        if symbol:
            market_type, normalized_symbol = _resolve_market_type(symbol, market_hint)
            market_types = [market_type]
        elif market_hint:
            norm = _normalize_market(market_hint)
            if norm:
                market_types = [norm]

        if not market_types and status == "pending":
            market_types = ["crypto", "equity_kr", "equity_us"]

        if not market_types and order_id:
            if "-" in order_id and len(order_id) == 36:
                market_types = ["crypto"]
            else:
                market_types = ["crypto", "equity_kr", "equity_us"]

        orders: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for m_type in market_types:
            try:
                fetched = []

                if m_type == "crypto":
                    if status in ("all", "pending"):
                        open_ops = await upbit_service.fetch_open_orders(
                            market=normalized_symbol
                        )
                        fetched.extend([_normalize_upbit_order(o) for o in open_ops])

                    if status in ("all", "filled", "cancelled") and normalized_symbol:
                        fetch_limit = 100 if limit_val == float("inf") else max(limit, 20)
                        closed_ops = await upbit_service.fetch_closed_orders(
                            market=normalized_symbol,
                            limit=fetch_limit,
                        )
                        fetched.extend([_normalize_upbit_order(o) for o in closed_ops])

                elif m_type == "equity_kr":
                    kis = KISClient()
                    if status in ("all", "pending"):
                        logger.debug(
                            "Fetching KR pending orders, symbol=%s", normalized_symbol
                        )
                        open_ops = await kis.inquire_korea_orders()
                        if open_ops:
                            logger.debug("Raw API response keys: %s", list(open_ops[0].keys()))
                        for o in open_ops:
                            o_sym = str(_get_kis_field(o, "pdno", "PDNO"))
                            if normalized_symbol and o_sym != normalized_symbol:
                                continue
                            fetched.append(_normalize_kis_domestic_order(o))

                    if status in ("all", "filled", "cancelled") and normalized_symbol:
                        lookup_days = effective_days if effective_days is not None else 30
                        start_dt, end_dt = _calculate_date_range(lookup_days)
                        hist_ops = await kis.inquire_daily_order_domestic(
                            start_date=start_dt,
                            end_date=end_dt,
                            stock_code=normalized_symbol,
                            side="00",
                        )
                        fetched.extend([_normalize_kis_domestic_order(o) for o in hist_ops])

                elif m_type == "equity_us":
                    kis = KISClient()
                    if status in ("all", "pending"):
                        target_exchanges = ["NASD", "NYSE", "AMEX"]
                        if normalized_symbol:
                            ex = get_exchange_by_symbol(normalized_symbol)
                            if ex:
                                target_exchanges = [ex]

                        seen_oids = set()
                        for ex in target_exchanges:
                            try:
                                ops = await kis.inquire_overseas_orders(ex)
                                for o in ops:
                                    oid = _extract_kis_order_number(o)
                                    if oid in seen_oids:
                                        continue
                                    seen_oids.add(oid)

                                    o_sym = str(_get_kis_field(o, "pdno", "PDNO"))
                                    if normalized_symbol and o_sym != normalized_symbol:
                                        continue
                                    fetched.append(_normalize_kis_overseas_order(o))
                            except Exception:
                                pass

                    if status in ("all", "filled", "cancelled") and normalized_symbol:
                        lookup_days = effective_days if effective_days is not None else 30
                        start_dt, end_dt = _calculate_date_range(lookup_days)
                        ex = get_exchange_by_symbol(normalized_symbol) or "NASD"
                        hist_ops = await kis.inquire_daily_order_overseas(
                            start_date=start_dt,
                            end_date=end_dt,
                            symbol=normalized_symbol,
                            exchange_code=ex,
                            side="00",
                        )
                        fetched.extend([_normalize_kis_overseas_order(o) for o in hist_ops])

                source_market = _normalize_market_type_to_external(m_type)
                for f in fetched:
                    f["_source_market"] = source_market
                orders.extend(fetched)

            except Exception as e:
                errors.append({"market": m_type, "error": str(e)})

        original_order_count = len(orders)
        unique_orders = {}
        for o in orders:
            oid = str(o.get("order_id") or "").strip()
            source_market = o.get("_source_market") or o.get("market") or "unknown"
            if oid:
                key = (source_market, oid)
                unique_orders[key] = o
            else:
                key = (
                    source_market,
                    o.get("symbol"),
                    o.get("side"),
                    o.get("ordered_price"),
                    o.get("ordered_qty"),
                    o.get("ordered_at"),
                    o.get("status"),
                    o.get("currency"),
                )
                unique_orders[key] = o

        orders = list(unique_orders.values())
        removed_duplicates = original_order_count - len(orders)
        if removed_duplicates > 0:
            logger.info("Removed %s duplicate orders", removed_duplicates)

        filtered_orders = []
        for o in orders:
            o_status = o.get("status")
            if status == "pending":
                if o_status not in ("pending", "partial"):
                    continue
            elif status == "filled":
                if o_status != "filled":
                    continue
            elif status == "cancelled":
                if o_status != "cancelled":
                    continue

            if order_id and o.get("order_id") != order_id:
                continue

            if side and o.get("side") != side:
                continue

            filtered_orders.append(o)

        def _get_sort_key(o: dict[str, Any]) -> str:
            val = o.get("ordered_at") or o.get("created_at") or ""
            return str(val)

        filtered_orders.sort(key=_get_sort_key, reverse=True)

        total_available = len(filtered_orders)
        truncated = False
        if limit_val != float("inf") and total_available > limit_val:
            filtered_orders = filtered_orders[: int(limit_val)]
            truncated = True

        response_orders = []
        for o in filtered_orders:
            cleaned = dict(o)
            cleaned.pop("_source_market", None)
            response_orders.append(cleaned)

        summary = _calculate_order_summary(response_orders)

        ret_market = "mixed"
        if len(market_types) == 1:
            ret_market = _normalize_market_type_to_external(market_types[0])
        elif normalized_symbol:
            m, _ = _resolve_market_type(normalized_symbol, None)
            ret_market = _normalize_market_type_to_external(m)

        return {
            "success": bool(response_orders) or not errors,
            "symbol": normalized_symbol,
            "market": ret_market,
            "status": status,
            "filters": {
                "symbol": symbol,
                "status": status,
                "order_id": order_id,
                "market": market_hint,
                "side": side,
                "days": days,
                "limit": limit,
            },
            "orders": response_orders,
            "summary": summary,
            "truncated": truncated,
            "total_available": total_available,
            "errors": errors,
        }

    @mcp.tool(
        name="place_order",
        description=(
            "Place buy/sell orders for stocks or crypto. "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "Always returns dry_run preview unless explicitly set to False. "
            "Safety limit: max 20 orders/day. "
            "dry_run=True by default for safety."
        ),
    )
    async def place_order(
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: Literal["limit", "market"] = "limit",
        quantity: float | None = None,
        price: float | None = None,
        amount: float | None = None,
        dry_run: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        return await _place_order_impl(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            amount=amount,
            dry_run=dry_run,
            reason=reason,
        )

    @mcp.tool(
        name="cancel_order",
        description=(
            "Cancel a pending order. "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "For KIS orders, automatically retrieves order details if not provided."
        ),
    )
    async def cancel_order(
        order_id: str,
        symbol: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a pending order."""
        order_id = (order_id or "").strip()
        if not order_id:
            raise ValueError("order_id is required")

        symbol = (symbol or "").strip() if symbol else None
        market_type = _parse_holdings_market_filter(market)

        if market_type is None:
            if symbol:
                market_type, _ = _resolve_market_type(symbol, None)
            elif "-" in order_id and len(order_id) == 36:
                market_type = "crypto"
            else:
                raise ValueError(
                    "market must be specified when symbol is not provided and order_id is not a UUID"
                )

        try:
            if market_type == "crypto":
                results = await upbit_service.cancel_orders([order_id])
                if results and len(results) > 0:
                    result = results[0]
                    if "error" in result:
                        return {
                            "success": False,
                            "order_id": order_id,
                            "error": result.get("error"),
                        }
                    return {
                        "success": True,
                        "order_id": order_id,
                        "cancelled_at": result.get("created_at", ""),
                    }
                return {
                    "success": False,
                    "order_id": order_id,
                    "error": "No result from Upbit",
                }

            if market_type == "equity_kr":
                if not symbol:
                    try:
                        kis = KISClient()
                        open_orders = await kis.inquire_korea_orders()
                        for order in open_orders:
                            if (
                                str(
                                    _get_kis_field(
                                        order, "odno", "ODNO", "ord_no", "ORD_NO"
                                    )
                                )
                                == order_id
                            ):
                                symbol = str(_get_kis_field(order, "pdno", "PDNO"))
                                break
                    except Exception as exc:
                        return {
                            "success": False,
                            "order_id": order_id,
                            "error": f"Failed to auto-retrieve order details: {exc}",
                        }

                if not symbol:
                    return {
                        "success": False,
                        "order_id": order_id,
                        "error": "symbol not found in order",
                    }

                try:
                    kis = KISClient()
                    side_code = "02"  # Default to buy
                    price = 0
                    quantity = 1

                    open_orders = await kis.inquire_korea_orders()
                    for order in open_orders:
                        if (
                            str(
                                _get_kis_field(
                                    order, "odno", "ODNO", "ord_no", "ORD_NO"
                                )
                            )
                            == order_id
                        ):
                            side_code = _get_kis_field(
                                order,
                                "sll_buy_dvsn_cd",
                                "SLL_BUY_DVSN_CD",
                                default="02",
                            )
                            price = int(
                                float(
                                    _get_kis_field(
                                        order, "ord_unpr", "ORD_UNPR", default=0
                                    )
                                    or 0
                                )
                            )
                            quantity = int(
                                float(
                                    _get_kis_field(
                                        order, "ord_qty", "ORD_QTY", default=0
                                    )
                                    or 0
                                )
                            )
                            break

                    order_type_str = "buy" if side_code == "02" else "sell"
                    result = await kis.cancel_korea_order(
                        order_number=order_id,
                        stock_code=symbol,
                        quantity=quantity,
                        price=price,
                        order_type=order_type_str,
                    )
                    return {
                        "success": True,
                        "order_id": order_id,
                        "symbol": symbol,
                        "cancelled_at": result.get("ord_tmd", ""),
                    }
                except Exception as exc:
                    return {
                        "success": False,
                        "order_id": order_id,
                        "symbol": symbol,
                        "error": str(exc),
                    }

            if market_type == "equity_us":
                if not symbol:
                    try:
                        kis = KISClient()
                        open_orders = await kis.inquire_overseas_orders("NASD")
                        for order in open_orders:
                            if str(_get_kis_field(order, "odno", "ODNO")) == order_id:
                                symbol = str(_get_kis_field(order, "pdno", "PDNO"))
                                break
                    except Exception as exc:
                        return {
                            "success": False,
                            "order_id": order_id,
                            "error": f"Failed to auto-retrieve order details: {exc}",
                        }

                if not symbol:
                    return {
                        "success": False,
                        "order_id": order_id,
                        "error": "symbol not found in order",
                    }

                try:
                    kis = KISClient()
                    quantity = 1

                    open_orders = await kis.inquire_overseas_orders("NASD")
                    for order in open_orders:
                        if str(_get_kis_field(order, "odno", "ODNO")) == order_id:
                            quantity = int(
                                float(
                                    _get_kis_field(
                                        order, "nccs_qty", "NCCS_QTY", default=0
                                    )
                                    or 0
                                )
                            )
                            break

                    result = await kis.cancel_overseas_order(
                        order_number=order_id,
                        symbol=symbol,
                        exchange_code="NASD",
                        quantity=quantity,
                    )
                    return {
                        "success": True,
                        "order_id": order_id,
                        "symbol": symbol,
                        "cancelled_at": result.get("ord_tmd", ""),
                    }
                except Exception as exc:
                    return {
                        "success": False,
                        "order_id": order_id,
                        "symbol": symbol,
                        "error": str(exc),
                    }

            return {
                "success": False,
                "order_id": order_id,
                "error": "Unsupported market type",
            }

        except Exception as exc:
            return {
                "success": False,
                "order_id": order_id,
                "error": str(exc),
            }

    @mcp.tool(
        name="modify_order",
        description=(
            "Modify a pending order (price/quantity). "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "dry_run=True by default for safety. "
            "Upbit: only limit orders in wait state. "
            "KIS: uses API modify endpoint."
        ),
    )
    async def modify_order(
        order_id: str,
        symbol: str,
        market: str | None = None,
        new_price: float | None = None,
        new_quantity: float | None = None,
        dry_run: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        """Modify a pending order (price/quantity)."""
        del reason  # currently metadata-only; kept for API contract stability
        if new_price is None and new_quantity is None:
            raise ValueError(
                "At least one of new_price or new_quantity must be specified"
            )
        if new_price is not None and new_price <= 0:
            raise ValueError("new_price must be a positive number")
        if new_quantity is not None and new_quantity <= 0:
            raise ValueError("new_quantity must be a positive number")

        order_id = order_id.strip()
        symbol = symbol.strip()
        market_type, normalized_symbol = _resolve_market_type(symbol, market)

        if dry_run:
            changes: dict[str, Any] = {
                "price": {"from": None, "to": new_price} if new_price else None,
                "quantity": {"from": None, "to": new_quantity}
                if new_quantity
                else None,
            }
            return {
                "success": True,
                "status": "simulated",
                "order_id": order_id,
                "symbol": normalized_symbol,
                "market": _normalize_market_type_to_external(market_type),
                "changes": changes,
                "method": "dry_run",
                "dry_run": dry_run,
                "message": f"Dry run - Preview changes for order {order_id}",
            }

        if market_type == "crypto":
            try:
                original_order = await upbit_service.fetch_order_detail(order_id)

                if original_order.get("state") != "wait":
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": "Order not in wait state (cannot modify non-pending orders)",
                        "dry_run": dry_run,
                    }
                if original_order.get("ord_type") != "limit":
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": "Only limit orders can be modified (not market orders)",
                        "dry_run": dry_run,
                    }

                original_price = float(original_order.get("price", 0) or 0)
                original_quantity = float(
                    original_order.get("remaining_volume", 0) or 0
                )
                final_price = new_price if new_price is not None else original_price
                final_quantity = (
                    new_quantity if new_quantity is not None else original_quantity
                )

                result = await upbit_service.cancel_and_reorder(
                    order_id, final_price, final_quantity
                )
                changes = {
                    "price": {"from": original_price, "to": final_price}
                    if final_price != original_price
                    else None,
                    "quantity": {"from": original_quantity, "to": final_quantity}
                    if final_quantity != original_quantity
                    else None,
                }

                if result.get("new_order") and "uuid" in result["new_order"]:
                    return {
                        "success": True,
                        "status": "modified",
                        "order_id": order_id,
                        "new_order_id": result["new_order"]["uuid"],
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "changes": changes,
                        "method": "cancel_reorder",
                        "dry_run": dry_run,
                        "message": "Order modified via cancel and reorder",
                    }
                return {
                    "success": False,
                    "status": "failed",
                    "order_id": order_id,
                    "symbol": normalized_symbol,
                    "market": _normalize_market_type_to_external(market_type),
                    "error": result.get("cancel_result", {}).get("error", "Unknown error"),
                    "changes": changes,
                    "method": "cancel_reorder",
                    "dry_run": dry_run,
                }
            except Exception as exc:
                return {
                    "success": False,
                    "status": "failed",
                    "order_id": order_id,
                    "symbol": normalized_symbol,
                    "market": _normalize_market_type_to_external(market_type),
                    "error": str(exc),
                    "changes": None,
                    "method": "cancel_reorder",
                    "dry_run": dry_run,
                }

        if market_type == "equity_kr":
            try:
                kis = KISClient()
                open_orders = await kis.inquire_korea_orders()
                target_order = None
                for order in open_orders:
                    if (
                        str(_get_kis_field(order, "odno", "ODNO", "ord_no", "ORD_NO"))
                        == order_id
                    ):
                        target_order = order
                        break

                if not target_order:
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": "Order not found in open orders",
                        "dry_run": dry_run,
                    }

                original_price = int(
                    float(
                        _get_kis_field(target_order, "ord_unpr", "ORD_UNPR", default=0)
                        or 0
                    )
                )
                original_quantity = int(
                    float(
                        _get_kis_field(target_order, "ord_qty", "ORD_QTY", default=0)
                        or 0
                    )
                )
                side_code = _get_kis_field(
                    target_order, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD"
                )
                side = "buy" if side_code == "02" else "sell"

                final_price_raw = (
                    int(new_price) if new_price is not None else original_price
                )
                final_price = int(adjust_tick_size_kr(float(final_price_raw), side))
                final_quantity = (
                    int(new_quantity) if new_quantity is not None else original_quantity
                )

                result = await kis.modify_korea_order(
                    order_id, normalized_symbol, final_quantity, final_price
                )
                changes = {
                    "price": {"from": original_price, "to": final_price}
                    if final_price != original_price
                    else None,
                    "quantity": {"from": original_quantity, "to": final_quantity}
                    if final_quantity != original_quantity
                    else None,
                }

                if result.get("odno"):
                    return {
                        "success": True,
                        "status": "modified",
                        "order_id": order_id,
                        "new_order_id": result["odno"],
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "changes": changes,
                        "method": "api_modify",
                        "dry_run": dry_run,
                        "message": "Order modified via KIS API",
                    }
                return {
                    "success": False,
                    "status": "failed",
                    "order_id": order_id,
                    "symbol": normalized_symbol,
                    "market": _normalize_market_type_to_external(market_type),
                    "error": result.get("msg", "Unknown error"),
                    "changes": changes,
                    "method": "api_modify",
                    "dry_run": dry_run,
                }
            except Exception as exc:
                return {
                    "success": False,
                    "status": "failed",
                    "order_id": order_id,
                    "symbol": normalized_symbol,
                    "market": _normalize_market_type_to_external(market_type),
                    "error": str(exc),
                    "changes": None,
                    "method": "api_modify",
                    "dry_run": dry_run,
                }

        if market_type == "equity_us":
            try:
                kis = KISClient()
                target_order = None
                target_exchange = None
                preferred_exchange = get_exchange_by_symbol(normalized_symbol) or "NASD"
                exchange_candidates: list[str] = []
                for exchange in [preferred_exchange, "NASD", "NYSE", "AMEX"]:
                    if exchange and exchange not in exchange_candidates:
                        exchange_candidates.append(exchange)

                for exchange in exchange_candidates:
                    try:
                        open_orders = await kis.inquire_overseas_orders(exchange)
                    except Exception:
                        continue
                    for order in open_orders:
                        if str(_get_kis_field(order, "odno", "ODNO")) == order_id:
                            target_order = order
                            target_exchange = exchange
                            break
                    if target_order:
                        break

                if not target_order:
                    return {
                        "success": False,
                        "status": "failed",
                        "order_id": order_id,
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "error": f"Order not found in open orders (checked: {', '.join(exchange_candidates)})",
                        "dry_run": dry_run,
                    }

                original_price = float(
                    _get_kis_field(
                        target_order, "ft_ord_unpr3", "FT_ORD_UNPR3", default=0
                    )
                    or 0
                )
                original_quantity = int(
                    float(
                        _get_kis_field(
                            target_order, "ft_ord_qty", "FT_ORD_QTY", default=0
                        )
                        or 0
                    )
                )

                exchange_code = target_exchange or preferred_exchange
                final_price = (
                    float(new_price) if new_price is not None else original_price
                )
                final_quantity = (
                    int(new_quantity) if new_quantity is not None else original_quantity
                )

                result = await kis.modify_overseas_order(
                    order_id,
                    normalized_symbol,
                    exchange_code,
                    final_quantity,
                    final_price,
                )
                changes = {
                    "price": {"from": original_price, "to": final_price}
                    if final_price != original_price
                    else None,
                    "quantity": {"from": original_quantity, "to": final_quantity}
                    if final_quantity != original_quantity
                    else None,
                }

                if result.get("odno"):
                    return {
                        "success": True,
                        "status": "modified",
                        "order_id": order_id,
                        "new_order_id": result["odno"],
                        "symbol": normalized_symbol,
                        "market": _normalize_market_type_to_external(market_type),
                        "changes": changes,
                        "method": "api_modify",
                        "dry_run": dry_run,
                        "message": "Order modified via KIS API",
                    }
                return {
                    "success": False,
                    "status": "failed",
                    "order_id": order_id,
                    "symbol": normalized_symbol,
                    "market": _normalize_market_type_to_external(market_type),
                    "error": result.get("msg", "Unknown error"),
                    "changes": changes,
                    "method": "api_modify",
                    "dry_run": dry_run,
                }
            except Exception as exc:
                return {
                    "success": False,
                    "status": "failed",
                    "order_id": order_id,
                    "symbol": normalized_symbol,
                    "market": _normalize_market_type_to_external(market_type),
                    "error": str(exc),
                    "changes": None,
                    "method": "api_modify",
                    "dry_run": dry_run,
                }

        return {
            "success": False,
            "status": "failed",
            "order_id": order_id,
            "symbol": normalized_symbol,
            "market": _normalize_market_type_to_external(market_type),
            "error": "Unknown market type",
            "changes": None,
            "method": "none",
            "dry_run": dry_run,
        }

__all__ = ["ORDER_TOOL_NAMES", "_register_order_tools_impl"]
