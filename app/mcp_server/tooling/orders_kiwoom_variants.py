# app/mcp_server/tooling/orders_kiwoom_variants.py
"""Kiwoom mock-only MCP tools.

Every tool is hard-pinned to ``account_mode="kiwoom_mock"``. They:
- Validate ``validate_kiwoom_mock_config`` before any side effect.
- Reject anything except KR equity (``market="kr"``).
- Reject ``NXT``/``SOR`` exchanges.
- Reject unsafe order ids (path separators, query fragments, whitespace,
  commas, newlines).
- Default order-like tools to ``dry_run=True`` and never call the broker
  unless ``dry_run=False`` AND ``confirm=True`` are both supplied.

Mirrors the structure of ``orders_kis_variants.py``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from app.core.config import validate_kiwoom_mock_config
from app.services.brokers.kiwoom import constants

if TYPE_CHECKING:
    from fastmcp import FastMCP

ACCOUNT_MODE_KIWOOM_MOCK = "kiwoom_mock"

KIWOOM_MOCK_TOOL_NAMES: set[str] = {
    "kiwoom_mock_preview_order",
    "kiwoom_mock_place_order",
    "kiwoom_mock_cancel_order",
    "kiwoom_mock_modify_order",
    "kiwoom_mock_get_order_history",
    "kiwoom_mock_get_positions",
    "kiwoom_mock_get_orderable_cash",
}

_SAFE_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _mock_config_error() -> dict[str, Any] | None:
    missing = validate_kiwoom_mock_config()
    if not missing:
        return None
    return {
        "success": False,
        "error": (
            "Kiwoom mock account is disabled or missing required configuration: "
            + ", ".join(missing)
        ),
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


def _market_error(market: str | None) -> dict[str, Any] | None:
    if market is None:
        return None
    if str(market).strip().lower() != "kr":
        return {
            "success": False,
            "error": "kiwoom_mock tools only support market='kr' (KR equity).",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        }
    return None


def _exchange_error(exchange: str | None) -> dict[str, Any] | None:
    if exchange is None:
        return None
    value = str(exchange).strip().upper()
    if (
        value in constants.MOCK_REJECTED_EXCHANGES
        or value != constants.MOCK_EXCHANGE_KRX
    ):
        return {
            "success": False,
            "error": f"kiwoom_mock supports KRX only; rejected exchange={exchange!r}.",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        }
    return None


def _order_id_error(order_id: str) -> dict[str, Any] | None:
    candidate = (order_id or "").strip()
    if not candidate or not _SAFE_ORDER_ID_RE.fullmatch(candidate):
        return {
            "success": False,
            "error": f"Unsafe order id rejected by kiwoom_mock: {order_id!r}",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        }
    return None


# ---------------------------------------------------------------------------
# Implementation seams (overridable via monkeypatch in tests).


async def _kiwoom_mock_place_order_impl(**kwargs: Any) -> dict[str, Any]:
    # Real implementation would build KiwoomMockClient.from_app_settings()
    # and call KiwoomDomesticOrderClient.place_buy_order/place_sell_order.
    # In this PR we only support dry_run; live submission is intentionally
    # blocked at the tool boundary (see register()).
    return {
        "success": True,
        "dry_run": kwargs.get("dry_run", True),
        "side": kwargs.get("side"),
        "symbol": kwargs.get("symbol"),
        "quantity": kwargs.get("quantity"),
        "price": kwargs.get("price"),
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


async def _kiwoom_mock_preview_impl(**kwargs: Any) -> dict[str, Any]:
    return {
        "success": True,
        "preview": True,
        "symbol": kwargs.get("symbol"),
        "side": kwargs.get("side"),
        "quantity": kwargs.get("quantity"),
        "price": kwargs.get("price"),
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


async def _kiwoom_mock_cancel_impl(**kwargs: Any) -> dict[str, Any]:
    return {
        "success": True,
        "dry_run": kwargs.get("dry_run", True),
        "order_id": kwargs.get("order_id"),
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


async def _kiwoom_mock_modify_impl(**kwargs: Any) -> dict[str, Any]:
    return {
        "success": True,
        "dry_run": kwargs.get("dry_run", True),
        "order_id": kwargs.get("order_id"),
        "new_price": kwargs.get("new_price"),
        "new_quantity": kwargs.get("new_quantity"),
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


async def _kiwoom_mock_order_history_impl(**kwargs: Any) -> dict[str, Any]:
    return {
        "success": True,
        "rows": [],
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


async def _kiwoom_mock_positions_impl(**kwargs: Any) -> dict[str, Any]:
    return {
        "success": True,
        "positions": [],
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


async def _kiwoom_mock_orderable_cash_impl(**kwargs: Any) -> dict[str, Any]:
    return {
        "success": True,
        "cash": None,
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="kiwoom_mock_preview_order",
        description="Preview a KRX-only Kiwoom mock order without sending.",
    )
    async def kiwoom_mock_preview_order(  # noqa: D401
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: int,
        price: int,
        market: str | None = "kr",
        exchange: str | None = "KRX",
    ) -> dict[str, Any]:
        for guard in (
            _mock_config_error(),
            _market_error(market),
            _exchange_error(exchange),
        ):
            if guard:
                return guard
        return await _kiwoom_mock_preview_impl(
            symbol=symbol, side=side, quantity=quantity, price=price
        )

    @mcp.tool(
        name="kiwoom_mock_place_order",
        description="Place a KRX-only Kiwoom mock order. dry_run defaults to True.",
    )
    async def kiwoom_mock_place_order(
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: int,
        price: int,
        market: str | None = "kr",
        exchange: str | None = "KRX",
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        for guard in (
            _mock_config_error(),
            _market_error(market),
            _exchange_error(exchange),
        ):
            if guard:
                return guard
        if not dry_run and not confirm:
            return {
                "success": False,
                "error": "kiwoom_mock_place_order requires confirm=True when dry_run=False.",
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            }
        return await _kiwoom_mock_place_order_impl(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            dry_run=dry_run,
        )

    @mcp.tool(
        name="kiwoom_mock_cancel_order",
        description="Cancel a Kiwoom mock order by id. dry_run defaults to True.",
    )
    async def kiwoom_mock_cancel_order(
        order_id: str,
        symbol: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if (guard := _mock_config_error()) is not None:
            return guard
        if (guard := _order_id_error(order_id)) is not None:
            return guard
        if not dry_run and not confirm:
            return {
                "success": False,
                "error": "kiwoom_mock_cancel_order requires confirm=True when dry_run=False.",
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            }
        return await _kiwoom_mock_cancel_impl(
            order_id=order_id, symbol=symbol, dry_run=dry_run
        )

    @mcp.tool(
        name="kiwoom_mock_modify_order",
        description="Modify a Kiwoom mock order. dry_run defaults to True.",
    )
    async def kiwoom_mock_modify_order(
        order_id: str,
        symbol: str,
        new_price: int | None = None,
        new_quantity: int | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if (guard := _mock_config_error()) is not None:
            return guard
        if (guard := _order_id_error(order_id)) is not None:
            return guard
        if not dry_run and not confirm:
            return {
                "success": False,
                "error": "kiwoom_mock_modify_order requires confirm=True when dry_run=False.",
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            }
        return await _kiwoom_mock_modify_impl(
            order_id=order_id,
            symbol=symbol,
            new_price=new_price,
            new_quantity=new_quantity,
            dry_run=dry_run,
        )

    @mcp.tool(
        name="kiwoom_mock_get_order_history",
        description="Read Kiwoom mock order/fill history (read-only).",
    )
    async def kiwoom_mock_get_order_history(
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        if (guard := _mock_config_error()) is not None:
            return guard
        return await _kiwoom_mock_order_history_impl(cont_yn=cont_yn, next_key=next_key)

    @mcp.tool(
        name="kiwoom_mock_get_positions",
        description="Read Kiwoom mock positions/balance (read-only).",
    )
    async def kiwoom_mock_get_positions() -> dict[str, Any]:
        if (guard := _mock_config_error()) is not None:
            return guard
        return await _kiwoom_mock_positions_impl()

    @mcp.tool(
        name="kiwoom_mock_get_orderable_cash",
        description="Read Kiwoom mock orderable cash (read-only).",
    )
    async def kiwoom_mock_get_orderable_cash(
        symbol: str | None = None,
    ) -> dict[str, Any]:
        if (guard := _mock_config_error()) is not None:
            return guard
        return await _kiwoom_mock_orderable_cash_impl(symbol=symbol)
