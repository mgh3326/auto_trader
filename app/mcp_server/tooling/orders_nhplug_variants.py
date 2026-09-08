"""NH mock mirror-lane MCP tools; no live or generic account-mode fallback."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from app.core.config import settings, validate_nhplug_mock_config
from app.mcp_server.tooling.kis_live_ledger import _order_session_factory
from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.auth import NHPlugAuthClient
from app.services.brokers.nhplug.client import NHPlugMockClient
from app.services.brokers.nhplug.inquiry import NHDomesticInquiryClient
from app.services.brokers.nhplug.orders import NHDomesticOrderClient
from app.services.kis_mock_attribution import MissingAttribution
from app.services.nh_mock_attribution import record_signal, resolve_attribution
from app.services.nh_mock_order_ledger_service import NHMockOrderLedgerService

if TYPE_CHECKING:
    from fastmcp import FastMCP

ACCOUNT_MODE_NH_MOCK = "nh_mock"
NH_MOCK_TOOL_NAMES = {
    "nh_mock_preview_order",
    "nh_mock_place_order",
    "nh_mock_cancel_order",
    "nh_mock_reconcile_orders",
    "nh_mock_get_order_history",
    "nh_mock_get_positions",
    "nh_mock_get_orderable_cash",
}


def _response_shape(response: dict[str, Any]) -> dict[str, Any]:
    """Do not expose account-scoped raw broker bodies through an MCP result."""

    code = response.get("rsp_cd")
    return {
        "response_code": code if isinstance(code, str) else "unknown",
        "output_sections": sorted(
            key for key in response if isinstance(key, str) and key.startswith("Output")
        ),
    }


def _config_error(*, orders: bool = False) -> dict[str, Any] | None:
    missing = validate_nhplug_mock_config(orders=orders)
    if not missing:
        return None
    return {
        "success": False,
        "account_mode": ACCOUNT_MODE_NH_MOCK,
        "error_code": "nh_mock_config_invalid",
        "missing": missing,
    }


def _order_input_error(
    symbol: str, side: str, quantity: int, price: int
) -> dict[str, Any] | None:
    if not isinstance(symbol, str) or not symbol.isdigit() or len(symbol) != 6:
        return {
            "success": False,
            "error_code": "symbol_invalid",
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    if (
        side not in {"buy", "sell"}
        or isinstance(quantity, bool)
        or quantity <= 0
        or isinstance(price, bool)
        or price <= 0
    ):
        return {
            "success": False,
            "error_code": "order_invalid",
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    return None


async def _clients() -> tuple[NHDomesticOrderClient, NHDomesticInquiryClient, str]:
    auth = NHPlugAuthClient(
        app_key=settings.nhplug_app_key or "",
        app_secret=settings.nhplug_app_secret or "",
    )
    data = NHPlugMockClient(
        app_key=settings.nhplug_app_key or "",
        app_secret=settings.nhplug_app_secret or "",
        token_provider=auth.get_access_token,
    )
    account_no = settings.nhplug_mock_account_no or ""
    allowlist = MockAccountAllowlist.from_acctinfo_response(
        payload=await data.list_accounts(), configured_account_no=account_no
    )
    data.bind_account_allowlist(allowlist)
    return (
        NHDomesticOrderClient(
            app_key=settings.nhplug_app_key or "",
            app_secret=settings.nhplug_app_secret or "",
            token_provider=auth.get_access_token,
            account_allowlist=allowlist,
        ),
        NHDomesticInquiryClient(data),
        account_no,
    )


async def nh_mock_preview_order(
    *,
    symbol: str,
    side: Literal["buy", "sell"],
    quantity: int,
    price: int,
    exchange: str = "KRX",
) -> dict[str, Any]:
    if (error := _config_error()) or (
        error := _order_input_error(symbol, side, quantity, price)
    ):
        return error
    if exchange != "KRX":
        return {
            "success": False,
            "error_code": "exchange_invalid",
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    return {
        "success": True,
        "dry_run": True,
        "account_mode": ACCOUNT_MODE_NH_MOCK,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "exchange": "KRX",
        "network_calls": 0,
    }


async def nh_mock_place_order(
    *,
    symbol: str,
    side: Literal["buy", "sell"],
    quantity: int,
    price: int,
    dry_run: bool = True,
    confirm: bool = False,
    strategy: str | None = None,
    correlation_id: str | None = None,
    counterfactual_of: str | None = None,
    mirror_cohort: str | None = None,
    avg_purchase_price: int | None = None,
) -> dict[str, Any]:
    if (error := _config_error(orders=not dry_run)) or (
        error := _order_input_error(symbol, side, quantity, price)
    ):
        return error
    if not dry_run and not confirm:
        return {
            "success": False,
            "error_code": "confirm_required",
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    if side == "sell" and (
        avg_purchase_price is None
        or price < Decimal(str(avg_purchase_price)) * Decimal("1.01")
    ):
        return {
            "success": False,
            "error_code": "loss_sell_guard",
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    try:
        attribution = resolve_attribution(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            strategy=strategy,
            correlation_id=correlation_id,
            counterfactual_of=counterfactual_of,
            mirror_cohort=mirror_cohort,
        )
    except MissingAttribution:
        return {
            "success": False,
            "error_code": "attribution_required",
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "account_mode": ACCOUNT_MODE_NH_MOCK,
            "correlation_id": attribution.correlation_id,
            "strategy": attribution.strategy,
            "network_calls": 0,
        }
    async with _order_session_factory() as db:
        await record_signal(
            db,
            attribution=attribution,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
        )
        try:
            orders, _, _ = await _clients()
            response = await (
                orders.place_buy_order(symbol=symbol, quantity=quantity, price=price)
                if side == "buy"
                else orders.place_sell_order(
                    symbol=symbol, quantity=quantity, price=price
                )
            )
        except (
            Exception
        ) as exc:  # broker rejection must remain failure, never ledger acceptance
            return {
                "success": False,
                "error_code": "broker_rejected",
                "error_type": type(exc).__name__,
                "account_mode": ACCOUNT_MODE_NH_MOCK,
            }
        broker_id = next(
            (
                str(response[key])
                for key in ("orr_no", "ord_no", "order_no")
                if response.get(key) not in (None, "")
            ),
            None,
        )
        client_order_id = f"nhmock-{uuid.uuid4()}"
        row = await NHMockOrderLedgerService(db).record_send(
            client_order_id=client_order_id,
            broker_order_id=broker_id,
            correlation_id=attribution.correlation_id,
            counterfactual_of=attribution.counterfactual_of,
            strategy=attribution.strategy,
            symbol=symbol,
            side=side,
            quantity=Decimal(quantity),
            price=Decimal(price),
            response_code=response.get("rsp_cd"),
            raw_response=response,
        )
    return {
        "success": True,
        "dry_run": False,
        "account_mode": ACCOUNT_MODE_NH_MOCK,
        "ledger_id": row.id,
        "broker_order_id": broker_id,
        "correlation_id": attribution.correlation_id,
        "status": "accepted",
    }


async def nh_mock_cancel_order(
    *, symbol: str, original_order_no: str, dry_run: bool = True, confirm: bool = False
) -> dict[str, Any]:
    if (error := _config_error(orders=not dry_run)) is not None:
        return error
    if not dry_run and not confirm:
        return {
            "success": False,
            "error_code": "confirm_required",
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "account_mode": ACCOUNT_MODE_NH_MOCK,
            "network_calls": 0,
        }
    try:
        orders, _, _ = await _clients()
        response = await orders.cancel_order(
            symbol=symbol, original_order_no=original_order_no
        )
    except Exception as exc:
        return {
            "success": False,
            "error_code": "broker_rejected",
            "error_type": type(exc).__name__,
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    return {
        "success": True,
        "dry_run": False,
        "account_mode": ACCOUNT_MODE_NH_MOCK,
        "broker_response_code": response.get("rsp_cd"),
    }


async def nh_mock_get_order_history(*, trade_date: str) -> dict[str, Any]:
    if (error := _config_error()) is not None:
        return error
    try:
        _, inquiry, account_no = await _clients()
        response = await inquiry.daily_order_execution(
            trade_date=trade_date, act_no=account_no
        )
    except Exception as exc:
        return {
            "success": False,
            "error_code": "broker_rejected",
            "error_type": type(exc).__name__,
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    return {
        "success": True,
        "account_mode": ACCOUNT_MODE_NH_MOCK,
        **_response_shape(response),
    }


async def nh_mock_get_positions() -> dict[str, Any]:
    if (error := _config_error()) is not None:
        return error
    try:
        _, inquiry, account_no = await _clients()
        response = await inquiry.get_positions(act_no=account_no)
    except Exception as exc:
        return {
            "success": False,
            "error_code": "broker_rejected",
            "error_type": type(exc).__name__,
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    return {
        "success": True,
        "account_mode": ACCOUNT_MODE_NH_MOCK,
        **_response_shape(response),
    }


async def nh_mock_get_orderable_cash() -> dict[str, Any]:
    if (error := _config_error()) is not None:
        return error
    try:
        _, inquiry, account_no = await _clients()
        response = await inquiry.get_orderable_cash(act_no=account_no)
    except Exception as exc:
        return {
            "success": False,
            "error_code": "broker_rejected",
            "error_type": type(exc).__name__,
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    return {
        "success": True,
        "account_mode": ACCOUNT_MODE_NH_MOCK,
        **_response_shape(response),
    }


def _fill_evidence_rows(payload: dict[str, Any]) -> list[tuple[str, Decimal]]:
    """Extract only exact-id, positive-quantity daily execution evidence."""

    found: list[tuple[str, Decimal]] = []
    for value in payload.values():
        if not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                continue
            order_id = next(
                (
                    row.get(key)
                    for key in ("orr_no", "ord_no", "org_mkt_orr_no")
                    if row.get(key) not in (None, "")
                ),
                None,
            )
            quantity = next(
                (
                    row.get(key)
                    for key in ("cns_qty", "cntr_qty", "filled_qty")
                    if row.get(key) not in (None, "")
                ),
                None,
            )
            try:
                parsed = Decimal(str(quantity))
            except Exception:  # malformed broker evidence is not fill evidence
                continue
            if order_id is not None and parsed > 0:
                found.append((str(order_id), parsed))
    return found


async def nh_mock_reconcile_orders(
    *, trade_date: str, dry_run: bool = True
) -> dict[str, Any]:
    """Reconcile accepted rows from daily execution evidence; default is read-only."""

    if (error := _config_error()) is not None:
        return error
    try:
        _, inquiry, account_no = await _clients()
        payload = await inquiry.daily_order_execution(
            trade_date=trade_date, act_no=account_no
        )
    except Exception as exc:
        return {
            "success": False,
            "error_code": "broker_rejected",
            "error_type": type(exc).__name__,
            "account_mode": ACCOUNT_MODE_NH_MOCK,
        }
    evidence = _fill_evidence_rows(payload)
    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "account_mode": ACCOUNT_MODE_NH_MOCK,
            "exact_fill_evidence_count": len(evidence),
            **_response_shape(payload),
        }
    applied = 0
    async with _order_session_factory() as db:
        ledger = NHMockOrderLedgerService(db)
        for broker_order_id, quantity in evidence:
            applied += int(
                await ledger.reconcile_fill_evidence(
                    broker_order_id=broker_order_id, filled_quantity=quantity
                )
            )
    return {
        "success": True,
        "dry_run": False,
        "account_mode": ACCOUNT_MODE_NH_MOCK,
        "exact_fill_evidence_count": len(evidence),
        "rows_updated": applied,
        **_response_shape(payload),
    }


def register(mcp: FastMCP) -> None:
    for func in (
        nh_mock_preview_order,
        nh_mock_place_order,
        nh_mock_cancel_order,
        nh_mock_get_order_history,
        nh_mock_get_positions,
        nh_mock_get_orderable_cash,
        nh_mock_reconcile_orders,
    ):
        mcp.tool(
            name=func.__name__,
            description="NHPLUG mock mirror-lane tool; default-disabled and KRX-only.",
        )(func)
