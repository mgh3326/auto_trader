from __future__ import annotations

from app.core.async_rate_limiter import RateLimitExceededError
from app.services.brokers.kis.client import KISClient
from app.services.brokers.upbit.client import (
    cancel_and_reorder,
    cancel_orders,
    place_buy_order,
    place_market_buy_order,
    place_market_sell_order,
    place_sell_order,
)
from app.services.domain_errors import (
    RateLimitError,
    UpstreamUnavailableError,
    ValidationError,
)
from app.services.orders.contracts import OrderResult
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol


def _normalize_market(market: str) -> str:
    normalized = str(market or "").strip().lower()
    aliases = {
        "kr": "equity_kr",
        "kospi": "equity_kr",
        "kosdaq": "equity_kr",
        "us": "equity_us",
        "nasdaq": "equity_us",
        "nyse": "equity_us",
        "crypto": "crypto",
        "upbit": "crypto",
    }
    resolved = aliases.get(normalized, normalized)
    if resolved not in {"equity_kr", "equity_us", "crypto"}:
        raise ValidationError(f"Unsupported market: {market}")
    return resolved


def _normalize_side(side: str | None) -> str:
    normalized = str(side or "").strip().lower()
    if normalized not in {"buy", "sell"}:
        raise ValidationError("side must be 'buy' or 'sell'")
    return normalized


def _normalize_order_type(order_type: str | None) -> str:
    normalized = str(order_type or "limit").strip().lower()
    if normalized not in {"limit", "market"}:
        raise ValidationError("order_type must be 'limit' or 'market'")
    return normalized


def _normalize_symbol(symbol: str, market: str) -> str:
    value = str(symbol or "").strip()
    if not value:
        raise ValidationError("symbol is required")
    if market == "crypto":
        upper = value.upper()
        if upper.startswith(("KRW-", "USDT-")):
            return upper
        return f"KRW-{upper}"
    return value.upper()


def _to_int_quantity(value: float | int | None, *, field_name: str) -> int:
    if value is None:
        raise ValidationError(f"{field_name} is required")
    quantity = int(float(value))
    if quantity <= 0:
        raise ValidationError(f"{field_name} must be > 0")
    return quantity


def _to_float_price(value: float | int | None, *, field_name: str) -> float:
    if value is None:
        raise ValidationError(f"{field_name} is required")
    price = float(value)
    if price <= 0:
        raise ValidationError(f"{field_name} must be > 0")
    return price


def _map_error(exc: Exception) -> Exception:
    if isinstance(exc, (ValidationError, RateLimitError, UpstreamUnavailableError)):
        return exc
    if isinstance(exc, RateLimitExceededError):
        return RateLimitError(str(exc))
    return UpstreamUnavailableError(str(exc))


async def place_order(
    *,
    symbol: str,
    market: str,
    side: str,
    order_type: str = "limit",
    quantity: float | int | None = None,
    price: float | int | None = None,
    amount: float | int | None = None,
    exchange_code: str | None = None,
    is_mock: bool = False,
) -> OrderResult:
    resolved_market = _normalize_market(market)
    resolved_side = _normalize_side(side)
    resolved_type = _normalize_order_type(order_type)
    resolved_symbol = _normalize_symbol(symbol, resolved_market)

    try:
        if resolved_market == "crypto":
            if resolved_side == "buy":
                if resolved_type == "market":
                    spend = _to_float_price(
                        amount if amount is not None else price,
                        field_name="amount",
                    )
                    raw = await place_market_buy_order(resolved_symbol, f"{spend}")
                else:
                    order_price = _to_float_price(price, field_name="price")
                    order_quantity = _to_float_price(quantity, field_name="quantity")
                    raw = await place_buy_order(
                        market=resolved_symbol,
                        price=f"{order_price}",
                        volume=f"{order_quantity:.8f}",
                        ord_type="limit",
                    )
            else:
                order_quantity = _to_float_price(quantity, field_name="quantity")
                if resolved_type == "market":
                    raw = await place_market_sell_order(
                        resolved_symbol,
                        f"{order_quantity:.8f}",
                    )
                else:
                    order_price = _to_float_price(price, field_name="price")
                    raw = await place_sell_order(
                        resolved_symbol,
                        f"{order_quantity:.8f}",
                        f"{order_price}",
                    )
            return OrderResult(
                order_id=str(raw.get("uuid") or "") or None,
                status="submitted",
                market=resolved_market,
                symbol=resolved_symbol,
                side=resolved_side,
                order_type=resolved_type,
                source="upbit",
                raw=raw,
            )

        kis = KISClient()
        kis_quantity = _to_int_quantity(quantity, field_name="quantity")

        if resolved_market == "equity_kr":
            price_value = (
                0
                if resolved_type == "market"
                else int(_to_float_price(price, field_name="price"))
            )
            raw = await kis.order_korea_stock(
                stock_code=resolved_symbol,
                order_type=resolved_side,
                quantity=kis_quantity,
                price=price_value,
                is_mock=is_mock,
            )
            return OrderResult(
                order_id=str(raw.get("odno") or "") or None,
                status="submitted",
                market=resolved_market,
                symbol=resolved_symbol,
                side=resolved_side,
                order_type=resolved_type,
                source="kis",
                raw=raw,
            )

        exchange = (
            str(exchange_code).strip().upper()
            if exchange_code is not None
            else await get_us_exchange_by_symbol(resolved_symbol)
        )
        price_value = (
            0.0
            if resolved_type == "market"
            else _to_float_price(price, field_name="price")
        )
        raw = await kis.order_overseas_stock(
            symbol=resolved_symbol,
            exchange_code=exchange,
            order_type=resolved_side,
            quantity=kis_quantity,
            price=price_value,
            is_mock=is_mock,
        )
        return OrderResult(
            order_id=str(raw.get("odno") or "") or None,
            status="submitted",
            market=resolved_market,
            symbol=resolved_symbol,
            side=resolved_side,
            order_type=resolved_type,
            source="kis",
            raw=raw,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


async def cancel_order(
    *,
    order_id: str,
    symbol: str,
    market: str,
    side: str | None = None,
    quantity: float | int | None = None,
    price: float | int | None = None,
    exchange_code: str | None = None,
    is_mock: bool = False,
    krx_fwdg_ord_orgno: str | None = None,
) -> OrderResult:
    resolved_market = _normalize_market(market)
    resolved_symbol = _normalize_symbol(symbol, resolved_market)
    if not str(order_id or "").strip():
        raise ValidationError("order_id is required")

    try:
        if resolved_market == "crypto":
            items = await cancel_orders([order_id])
            raw = items[0] if items else {}
            return OrderResult(
                order_id=str(raw.get("uuid") or order_id),
                status="cancelled" if "error" not in raw else "failed",
                market=resolved_market,
                symbol=resolved_symbol,
                side=None,
                order_type=None,
                source="upbit",
                raw=raw,
            )

        kis = KISClient()
        kis_quantity = _to_int_quantity(quantity, field_name="quantity")

        if resolved_market == "equity_kr":
            resolved_side = _normalize_side(side)
            price_value = int(_to_float_price(price, field_name="price"))
            raw = await kis.cancel_korea_order(
                order_number=order_id,
                stock_code=resolved_symbol,
                quantity=kis_quantity,
                price=price_value,
                order_type=resolved_side,
                is_mock=is_mock,
                krx_fwdg_ord_orgno=krx_fwdg_ord_orgno,
            )
            return OrderResult(
                order_id=str(raw.get("odno") or order_id),
                status="cancelled",
                market=resolved_market,
                symbol=resolved_symbol,
                side=resolved_side,
                order_type=None,
                source="kis",
                raw=raw,
            )

        exchange = (
            str(exchange_code).strip().upper()
            if exchange_code is not None
            else await get_us_exchange_by_symbol(resolved_symbol)
        )
        raw = await kis.cancel_overseas_order(
            order_number=order_id,
            symbol=resolved_symbol,
            exchange_code=exchange,
            quantity=kis_quantity,
            is_mock=is_mock,
        )
        return OrderResult(
            order_id=str(raw.get("odno") or order_id),
            status="cancelled",
            market=resolved_market,
            symbol=resolved_symbol,
            side=None,
            order_type=None,
            source="kis",
            raw=raw,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


async def modify_order(
    *,
    order_id: str,
    symbol: str,
    market: str,
    new_price: float | int | None,
    new_quantity: float | int | None,
    exchange_code: str | None = None,
    is_mock: bool = False,
    krx_fwdg_ord_orgno: str | None = None,
) -> OrderResult:
    resolved_market = _normalize_market(market)
    resolved_symbol = _normalize_symbol(symbol, resolved_market)
    if not str(order_id or "").strip():
        raise ValidationError("order_id is required")

    try:
        if resolved_market == "crypto":
            if new_price is None:
                raise ValidationError("new_price is required for crypto modify")
            quantity_value = float(new_quantity) if new_quantity is not None else None
            raw = await cancel_and_reorder(
                order_uuid=order_id,
                new_price=float(new_price),
                new_quantity=quantity_value,
            )
            new_order = raw.get("new_order") if isinstance(raw, dict) else None
            new_order_id = None
            if isinstance(new_order, dict):
                new_order_id = str(new_order.get("uuid") or "") or None
            return OrderResult(
                order_id=new_order_id or order_id,
                status="modified",
                market=resolved_market,
                symbol=resolved_symbol,
                side=None,
                order_type="limit",
                source="upbit",
                raw=raw,
            )

        kis = KISClient()
        quantity_value = _to_int_quantity(new_quantity, field_name="new_quantity")
        price_value = _to_float_price(new_price, field_name="new_price")

        if resolved_market == "equity_kr":
            raw = await kis.modify_korea_order(
                order_number=order_id,
                stock_code=resolved_symbol,
                quantity=quantity_value,
                new_price=int(price_value),
                is_mock=is_mock,
                krx_fwdg_ord_orgno=krx_fwdg_ord_orgno,
            )
            return OrderResult(
                order_id=str(raw.get("odno") or order_id),
                status="modified",
                market=resolved_market,
                symbol=resolved_symbol,
                side=None,
                order_type="limit",
                source="kis",
                raw=raw,
            )

        exchange = (
            str(exchange_code).strip().upper()
            if exchange_code is not None
            else await get_us_exchange_by_symbol(resolved_symbol)
        )
        raw = await kis.modify_overseas_order(
            order_number=order_id,
            symbol=resolved_symbol,
            exchange_code=exchange,
            quantity=quantity_value,
            new_price=price_value,
            is_mock=is_mock,
        )
        return OrderResult(
            order_id=str(raw.get("odno") or order_id),
            status="modified",
            market=resolved_market,
            symbol=resolved_symbol,
            side=None,
            order_type="limit",
            source="kis",
            raw=raw,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


__all__ = ["place_order", "cancel_order", "modify_order", "OrderResult"]
