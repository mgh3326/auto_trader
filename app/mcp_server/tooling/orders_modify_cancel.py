"""Order modify/cancel normalization helpers."""

from __future__ import annotations

import hashlib
from typing import Any

from app.mcp_server.tick_size import adjust_tick_size_kr
from app.mcp_server.tooling.order_execution import (
    _normalize_market_type_to_external,
)
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import (
    parse_holdings_market_filter as _parse_holdings_market_filter,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.services import upbit as upbit_service
from app.services.kis import KISClient
from data.stocks_info.overseas_us_stocks import get_exchange_by_symbol


def _map_upbit_state(state: str, filled: float, remaining: float) -> str:
    if state == "wait":
        return "pending"
    if state == "done":
        if filled > 0:
            return "filled"
        return "cancelled"
    if state == "cancelled":
        return "cancelled"
    return "partial"


def _normalize_upbit_order(order: dict[str, Any]) -> dict[str, Any]:
    side_code = order.get("side", "")
    side = "buy" if side_code == "bid" else "sell"

    state = order.get("state", "")
    remaining = float(order.get("remaining_volume", 0) or 0)
    filled = float(order.get("executed_volume", 0) or 0)
    ordered = remaining + filled

    ordered_price = float(order.get("price", 0) or 0)
    filled_price = float(order.get("avg_price", 0) or 0)
    status = _map_upbit_state(state, filled, remaining)

    return {
        "order_id": order.get("uuid", ""),
        "symbol": order.get("market", ""),
        "side": side,
        "status": status,
        "ordered_qty": ordered,
        "filled_qty": filled,
        "remaining_qty": remaining,
        "ordered_price": ordered_price,
        "filled_avg_price": filled_price,
        "ordered_at": order.get("created_at", ""),
        "filled_at": order.get("done_at", ""),
        "currency": "KRW",
    }


def _get_kis_field(order: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = order.get(key)
        if value:
            return value
    return default


def _extract_kis_order_number(order: dict[str, Any]) -> str:
    value = _get_kis_field(
        order,
        "odno",
        "ODNO",
        "ord_no",
        "ORD_NO",
        "orgn_odno",
        "ORGN_ODNO",
        default="",
    )
    if value is None:
        return ""
    return str(value).strip()


def _build_temp_kr_order_id(
    *,
    symbol: str,
    side: str,
    ordered_price: int,
    ordered_qty: int,
    ordered_at: str,
) -> str:
    raw = "|".join(
        [symbol, side, str(ordered_price), str(ordered_qty), ordered_at.strip()]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12].upper()
    return f"TEMP_KR_{digest}"


def _map_kis_status(filled: int, remaining: int, status_name: str) -> str:
    if status_name in ("접수", "주문접수"):
        return "pending"
    if status_name == "주문취소":
        return "cancelled"
    if status_name in ("체결", "미체결"):
        if remaining > 0:
            return "partial"
        return "filled"
    return "pending"


def _normalize_kis_domestic_order(order: dict[str, Any]) -> dict[str, Any]:
    side_code = _get_kis_field(order, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD")
    side = "buy" if side_code == "02" else "sell"

    ordered = int(float(_get_kis_field(order, "ord_qty", "ORD_QTY", default=0) or 0))
    filled = int(float(_get_kis_field(order, "ccld_qty", "CCLD_QTY", default=0) or 0))
    remaining = ordered - filled

    ordered_price = int(
        float(_get_kis_field(order, "ord_unpr", "ORD_UNPR", default=0) or 0)
    )
    filled_price = int(
        float(_get_kis_field(order, "ccld_unpr", "CCLD_UNPR", default=0) or 0)
    )

    status = _map_kis_status(
        filled,
        remaining,
        _get_kis_field(order, "prcs_stat_name", "PRCS_STAT_NAME"),
    )
    symbol = str(_get_kis_field(order, "pdno", "PDNO"))
    ordered_at = (
        f"{_get_kis_field(order, 'ord_dt', 'ORD_DT')} "
        f"{_get_kis_field(order, 'ord_tmd', 'ORD_TMD')}"
    )
    order_id = _extract_kis_order_number(order)
    if not order_id:
        order_id = _build_temp_kr_order_id(
            symbol=symbol,
            side=side,
            ordered_price=ordered_price,
            ordered_qty=ordered,
            ordered_at=ordered_at,
        )
        logger.warning(
            "Missing order_id for KR order (symbol=%s, side=%s, qty=%s, price=%s, ordered_at=%s), generated %s",
            symbol,
            side,
            ordered,
            ordered_price,
            ordered_at,
            order_id,
        )

    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "ordered_qty": ordered,
        "filled_qty": filled,
        "remaining_qty": remaining,
        "ordered_price": ordered_price,
        "filled_avg_price": filled_price,
        "ordered_at": ordered_at,
        "filled_at": "",
        "currency": "KRW",
    }


def _normalize_kis_overseas_order(order: dict[str, Any]) -> dict[str, Any]:
    side_code = _get_kis_field(order, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD")
    side = "buy" if side_code == "02" else "sell"

    ordered = int(
        float(_get_kis_field(order, "ft_ord_qty", "FT_ORD_QTY", default=0) or 0)
    )
    filled = int(
        float(_get_kis_field(order, "ft_ccld_qty", "FT_CCLD_QTY", default=0) or 0)
    )
    remaining = ordered - filled

    ordered_price = float(
        _get_kis_field(order, "ft_ord_unpr3", "FT_ORD_UNPR3", default=0) or 0
    )
    filled_price = float(
        _get_kis_field(order, "ft_ccld_unpr3", "FT_CCLD_UNPR3", default=0) or 0
    )

    status = _map_kis_status(
        filled,
        remaining,
        _get_kis_field(order, "prcs_stat_name", "PRCS_STAT_NAME"),
    )

    return {
        "order_id": _extract_kis_order_number(order),
        "symbol": _get_kis_field(order, "pdno", "PDNO"),
        "side": side,
        "status": status,
        "ordered_qty": ordered,
        "filled_qty": filled,
        "remaining_qty": remaining,
        "ordered_price": ordered_price,
        "filled_avg_price": filled_price,
        "ordered_at": (
            f"{_get_kis_field(order, 'ord_dt', 'ORD_DT')} "
            f"{_get_kis_field(order, 'ord_tmd', 'ORD_TMD')}"
        ),
        "filled_at": "",
        "currency": "USD",
    }


async def cancel_order_impl(
    order_id: str,
    symbol: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
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
                side_code = "02"
                price = 0
                quantity = 1

                open_orders = await kis.inquire_korea_orders()
                for order in open_orders:
                    if (
                        str(_get_kis_field(order, "odno", "ODNO", "ord_no", "ORD_NO"))
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
                                _get_kis_field(order, "ord_unpr", "ORD_UNPR", default=0)
                                or 0
                            )
                        )
                        quantity = int(
                            float(
                                _get_kis_field(order, "ord_qty", "ORD_QTY", default=0)
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
                                _get_kis_field(order, "nccs_qty", "NCCS_QTY", default=0)
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


async def modify_order_impl(
    order_id: str,
    symbol: str,
    market: str | None = None,
    new_price: float | None = None,
    new_quantity: float | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    if new_price is None and new_quantity is None:
        raise ValueError("At least one of new_price or new_quantity must be specified")
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
            "quantity": {"from": None, "to": new_quantity} if new_quantity else None,
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
            original_quantity = float(original_order.get("remaining_volume", 0) or 0)
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
                    _get_kis_field(target_order, "ord_unpr", "ORD_UNPR", default=0) or 0
                )
            )
            original_quantity = int(
                float(
                    _get_kis_field(target_order, "ord_qty", "ORD_QTY", default=0) or 0
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
                _get_kis_field(target_order, "ft_ord_unpr3", "FT_ORD_UNPR3", default=0)
                or 0
            )
            original_quantity = int(
                float(
                    _get_kis_field(target_order, "ft_ord_qty", "FT_ORD_QTY", default=0)
                    or 0
                )
            )

            exchange_code = target_exchange or preferred_exchange
            final_price = float(new_price) if new_price is not None else original_price
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
                    "exchange": exchange_code,
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
                "exchange": exchange_code,
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
        "error": f"modify_order is not supported for market '{market_type}'",
        "dry_run": dry_run,
    }


__all__ = [
    "cancel_order_impl",
    "modify_order_impl",
    "_extract_kis_order_number",
    "_get_kis_field",
    "_normalize_kis_domestic_order",
    "_normalize_kis_overseas_order",
    "_normalize_upbit_order",
]
