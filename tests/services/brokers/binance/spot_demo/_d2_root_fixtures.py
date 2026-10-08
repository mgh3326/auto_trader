"""#1268 — shared builders for the D2 root reconcile tests (no DB, no network)."""

from __future__ import annotations

import copy
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.spot_demo.d2_remediation_single import (
    D2_BOUND_ORDERS,
    D2_CREDENTIAL_FINGERPRINT,
    D2_EXCEPTION_ID,
    D2_PRE_SNAPSHOT_HASH,
    D2_REMEDIATION_ID,
    WRITER_NAME,
    D2BoundOrder,
)
from app.services.brokers.binance.spot_demo.d2_root_reconcile import (
    AUDIT_KEY,
    TOOL_NAME,
)

BTC, ETH, USDC = D2_BOUND_ORDERS
SPOT_DEMO_URL = "https://demo-api.binance.com"


def d2_metadata(order: D2BoundOrder, operation_id: str = "op") -> dict[str, Any]:
    """The evidence metadata the D2 writer stamps on its claim."""
    return {
        "writer": WRITER_NAME,
        "d2_exception_id": D2_EXCEPTION_ID,
        "remediation_id": D2_REMEDIATION_ID,
        "pre_snapshot_hash": D2_PRE_SNAPSHOT_HASH,
        "credential_fingerprint": D2_CREDENTIAL_FINGERPRINT,
        "operation_id": operation_id,
        "canary_or_strategy_use": "forbidden",
    }


def fake_instrument(symbol: str = "BTCUSDT", **overrides: Any) -> SimpleNamespace:
    values = {"venue": "binance", "product": "spot", "venue_symbol": symbol}
    values.update(overrides)
    return SimpleNamespace(**values)


def fake_row(order: D2BoundOrder = BTC, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": 442,
        "client_order_id": order.client_order_id,
        "broker_order_id": "9000001",
        "product": "spot",
        "venue_host": "demo-api.binance.com",
        "parent_client_order_id": None,
        "lifecycle_state": "filled",
        "side": order.side,
        "order_type": order.order_type,
        "qty": order.quantity,
        "price": order.price,
        "filled_at": None,
        "extra_metadata": d2_metadata(order),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def filled_body(order: D2BoundOrder = BTC, broker_order_id: Any = 9000001) -> dict:
    """A redacted Spot ``GET /api/v3/order`` answer for a fully filled order."""
    return {
        "symbol": order.symbol,
        "orderId": broker_order_id,
        "clientOrderId": order.client_order_id,
        "price": format(order.price, "f"),
        "origQty": format(order.quantity, "f"),
        "executedQty": format(order.quantity, "f"),
        "cummulativeQuoteQty": format(order.quantity * order.price, "f"),
        "status": "FILLED",
        "timeInForce": order.time_in_force,
        "type": order.order_type,
        "side": order.side,
        "time": 1787300000000,
        "updateTime": 1787303700000,
    }


def _with_metadata(**changes: Any) -> SimpleNamespace:
    metadata = d2_metadata(BTC)
    metadata.update(changes)
    return fake_row(extra_metadata=metadata)


def _reconciled_by_tool() -> SimpleNamespace:
    metadata = d2_metadata(BTC)
    metadata[AUDIT_KEY] = {"tool": TOOL_NAME, "batch_id": "b", "at": "t"}
    return fake_row(lifecycle_state="reconciled", extra_metadata=metadata)


#: refusal verdict -> (row, instrument) that only that guard refuses.
ROW_REFUSALS: dict[str, Any] = {
    "not_found": lambda: (None, None),
    "already_reconciled": lambda: (_reconciled_by_tool(), fake_instrument()),
    "not_spot": lambda: (fake_row(product="usdm_futures"), fake_instrument()),
    "not_spot_demo_host": lambda: (
        fake_row(venue_host="demo-fapi.binance.com"),
        fake_instrument(),
    ),
    "not_root": lambda: (fake_row(parent_client_order_id="p-1"), fake_instrument()),
    "not_filled": lambda: (fake_row(lifecycle_state="submitted"), fake_instrument()),
    "not_d2_writer": lambda: (
        _with_metadata(writer="demo_scalping"),
        fake_instrument(),
    ),
    "exception_mismatch": lambda: (
        _with_metadata(d2_exception_id="other-exception"),
        fake_instrument(),
    ),
    "remediation_mismatch": lambda: (
        _with_metadata(remediation_id="other-remediation"),
        fake_instrument(),
    ),
    "canary_use_not_forbidden": lambda: (
        _with_metadata(canary_or_strategy_use="allowed"),
        fake_instrument(),
    ),
    "credential_fingerprint_mismatch": lambda: (
        _with_metadata(credential_fingerprint="sha256:" + "0" * 64),
        fake_instrument(),
    ),
    "not_bound_order": lambda: (
        fake_row(client_order_id="d2rem-000000000000000000000000"),
        fake_instrument(),
    ),
    "instrument_mismatch": lambda: (fake_row(), fake_instrument("ETHUSDT")),
    "side_mismatch": lambda: (fake_row(side="BUY"), fake_instrument()),
    "order_type_mismatch": lambda: (fake_row(order_type="MARKET"), fake_instrument()),
    "qty_mismatch": lambda: (fake_row(qty=Decimal("0.00016")), fake_instrument()),
    "price_mismatch": lambda: (fake_row(price=Decimal("75421.28")), fake_instrument()),
    "broker_order_id_missing": lambda: (
        fake_row(broker_order_id=None),
        fake_instrument(),
    ),
}


def refusal_row(verdict: str) -> tuple[Any, Any]:
    return ROW_REFUSALS[verdict]()


def _body(**changes: Any) -> dict:
    body = filled_body()
    body.update(changes)
    return body


#: evidence refusal verdict -> (row, body) that only that guard refuses.
EVIDENCE_REFUSALS: dict[str, Any] = {
    "evidence_order_not_found": lambda: (
        fake_row(),
        BinanceDemoOrderNotFound("not found"),
    ),
    "evidence_read_failed": lambda: (fake_row(), RuntimeError("socket closed")),
    "evidence_not_mapping": lambda: (fake_row(), ["not", "a", "mapping"]),
    "evidence_client_order_id": lambda: (
        fake_row(),
        _body(clientOrderId="someone-else"),
    ),
    "evidence_order_id": lambda: (fake_row(), _body(orderId=9000002)),
    "evidence_symbol": lambda: (fake_row(), _body(symbol="ETHUSDT")),
    "evidence_side": lambda: (fake_row(), _body(side="BUY")),
    "evidence_type": lambda: (fake_row(), _body(type="MARKET")),
    "evidence_status_not_filled": lambda: (
        fake_row(),
        _body(status="PARTIALLY_FILLED"),
    ),
    "evidence_orig_qty": lambda: (fake_row(), _body(origQty="0.00016000")),
    "evidence_executed_qty": lambda: (fake_row(), _body(executedQty="0.00014000")),
    "evidence_price": lambda: (fake_row(), _body(price="75421.28000000")),
    "evidence_time_in_force": lambda: (fake_row(), _body(timeInForce="IOC")),
    "evidence_fill_actual_conflict": lambda: (
        _with_metadata(filled_qty="0.00014"),
        filled_body(),
    ),
}


def refusal_evidence(verdict: str) -> tuple[Any, Any]:
    return EVIDENCE_REFUSALS[verdict]()


class FakeSpotDemoReader:
    """A read-only Spot Demo reader. Any other attribute access fails loudly."""

    def __init__(
        self,
        answers: dict[str, Any] | None = None,
        *,
        base_url: str = SPOT_DEMO_URL,
        credential_fingerprint: str = D2_CREDENTIAL_FINGERPRINT,
    ) -> None:
        self._base_url = base_url
        self.credential_fingerprint = credential_fingerprint
        self.answers = answers or {}
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def get_order_status(self, *, symbol: str, client_order_id: str) -> dict:
        self.calls.append((symbol, client_order_id))
        answer = self.answers.get(client_order_id)
        if answer is None:
            raise BinanceDemoOrderNotFound(client_order_id)
        if isinstance(answer, BaseException):
            raise answer
        return copy.deepcopy(answer)

    async def aclose(self) -> None:
        self.closed = True

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"reconcile reached a non-read client method: {name}")
