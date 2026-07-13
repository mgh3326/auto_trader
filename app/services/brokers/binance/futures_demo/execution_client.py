"""ROB-298 PR 2 — Binance USD-M Futures Demo execution backend.

Mutation-capable signed adapter for ``demo-fapi.binance.com``. Sibling of
``spot_demo.execution_client`` — independent env namespace
(``BINANCE_FUTURES_DEMO_*``), independent host allowlist
(``demo-fapi.binance.com`` only), independent transport, exception
hierarchy, and HMAC chokepoint. Per ROB-296 §1, environment-specific
fail-closed isolation is preserved deliberately.

Hard invariants enforced here:

  * Default-disabled — ``BINANCE_FUTURES_DEMO_ENABLED=true`` required at
    construction. Defaults raise ``BinanceFuturesDemoDisabled``.
  * Fail-closed credentials — empty key or secret raises
    ``BinanceFuturesDemoMissingCredentials``.
  * Host allowlist — ``base_url`` host must be in
    ``FUTURES_DEMO_HOSTS``; enforced both at construction (via the
    transport factory) and on every request (via the per-request event
    hook). Live spot / live futures / Spot Demo / deprecated testnet
    hosts raise ``BinanceFuturesDemoCrossAllowlistViolation``.
  * Per-call operator gate — ``submit_order(..., confirm=False)`` (the
    default) returns a ``FuturesDemoDryRunResult`` and dispatches zero
    HTTP. Only ``confirm=True`` routes the signed POST through the HMAC
    chokepoint to ``/fapi/v1/order``.
  * Leverage echo verification — ``set_leverage`` verifies the Binance
    response echoes back the requested leverage; any mismatch raises
    ``BinanceFuturesDemoLeverageMismatch`` (the smoke contract pins 1x
    leverage exactly).
  * reduceOnly threading — the ``reduce_only`` flag on ``submit_order``
    is sent to Binance as ``reduceOnly=true`` when set, providing the
    structural guard against accidentally flipping a position.
  * Secret hygiene — the API secret lives on a single private attribute
    (``_api_secret``) and is never read by ``repr``, log messages, or
    error strings.

Distinct from preflight: ``preflight.py`` covers the read-only
``GET /fapi/v1/account`` smoke path. This module is the mutation surface
and is the only place under ``futures_demo/`` that signs POST/DELETE
requests (set_leverage, submit, cancel) plus the futures-specific signed
GETs (positionRisk, positionSide/dual, openOrders).
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from app.services.brokers.binance.demo.credential_identity import (
    demo_credential_fingerprint,
)
from app.services.brokers.binance.demo.credentials import resolve_demo_credentials
from app.services.brokers.binance.demo.errors import (
    BinanceDemoCredentialError,
    BinanceDemoOrderNotFound,
)
from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoCancelResult,
    FuturesDemoLeverageResult,
    FuturesDemoOpenOrder,
    FuturesDemoOpenOrdersResult,
    FuturesDemoOrderStatusResult,
    FuturesDemoOrderSubmitResult,
    FuturesDemoOrderTestResult,
    FuturesDemoPositionModeResult,
    FuturesDemoPositionResult,
)
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoDisabled,
    BinanceFuturesDemoLeverageMismatch,
    BinanceFuturesDemoMissingCredentials,
)
from app.services.brokers.binance.futures_demo.signing import (
    BINANCE_FUTURES_DEMO_RECV_WINDOW_MS,
    _sign_request_params,
)
from app.services.brokers.binance.futures_demo.transport import (
    build_futures_demo_client,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL: Final[str] = "https://demo-fapi.binance.com"
_ORDER_PATH: Final[str] = "/fapi/v1/order"
_ORDER_TEST_PATH: Final[str] = "/fapi/v1/order/test"
_OPEN_ORDERS_PATH: Final[str] = "/fapi/v1/openOrders"
# ROB-303: demo-fapi rejects /fapi/v1/positionRisk with -5000 ("Path ...
# is invalid"). v2 is the demo-fapi-supported position-reconcile source.
_POSITION_RISK_PATH: Final[str] = "/fapi/v2/positionRisk"
_POSITION_SIDE_DUAL_PATH: Final[str] = "/fapi/v1/positionSide/dual"
_LEVERAGE_PATH: Final[str] = "/fapi/v1/leverage"

ALLOWED_SIDES: Final[frozenset[str]] = frozenset({"BUY", "SELL"})
ALLOWED_ORDER_TYPES: Final[frozenset[str]] = frozenset({"LIMIT", "MARKET"})


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FuturesDemoDryRunResult:
    """Operator-gate dry-run sentinel for Futures Demo mutations.

    Returned by mutation entry points (``submit_order``, ``preview_submit``)
    when ``confirm=False`` (the default). Carries the prospective order
    metadata for operator audit without any HTTP having been dispatched.
    No signed payload is included because no signing occurred.
    """

    symbol: str
    side: str
    order_type: str
    qty: Decimal
    client_order_id: str
    reduce_only: bool = False
    reason: str = "confirm=False — operator gate not passed; no HTTP attempted"


class BinanceFuturesDemoExecutionClient:
    """Signed adapter for the Binance USD-M Futures Demo endpoint.

    Construct via ``BinanceFuturesDemoExecutionClient.from_env()`` in
    production code paths (does the fail-closed env validation in one
    place); direct ``__init__`` is exposed for tests that want to inject
    fake credentials.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        if not api_key:
            raise BinanceFuturesDemoMissingCredentials(
                "BINANCE_FUTURES_DEMO_API_KEY is empty. Refusing to construct "
                "Futures Demo execution client."
            )
        if not api_secret:
            raise BinanceFuturesDemoMissingCredentials(
                "BINANCE_FUTURES_DEMO_API_SECRET is empty. Refusing to "
                "construct Futures Demo execution client."
            )
        # Transport factory enforces the host-allowlist check on base_url;
        # raises BinanceFuturesDemoCrossAllowlistViolation for sibling
        # demo / live / deprecated-testnet hosts, BinanceLiveHostBlocked
        # for anything else.
        self._client = build_futures_demo_client(
            api_key=api_key, api_secret=api_secret, base_url=base_url
        )
        # _api_secret is the ONLY persistent reference to the secret.
        # repr/str/log paths MUST NOT read this attribute directly.
        self._api_secret = api_secret
        self._api_key = api_key
        self._base_url = base_url

    @property
    def credential_fingerprint(self) -> str:
        """Opaque identity used to bind reconciliation to this credential."""
        return demo_credential_fingerprint(self._api_key)

    @classmethod
    def from_env(cls) -> BinanceFuturesDemoExecutionClient:
        """Construct from environment variables with full fail-closed checks.

        Env contract:
          * ``BINANCE_FUTURES_DEMO_ENABLED`` MUST be truthy.
          * Credentials (ROB-302): the ``BINANCE_FUTURES_DEMO_API_*`` pair
            OR the canonical ``BINANCE_DEMO_API_*`` pair MUST be present
            (the per-product pair wins when set). A half-set pair fails
            closed. Resolved via ``demo.credentials.resolve_demo_credentials``.
          * ``BINANCE_FUTURES_DEMO_BASE_URL`` (optional) MUST be a Futures
            Demo host if set; transport factory enforces.

        Note: neither ``BINANCE_TESTNET_*`` nor ``BINANCE_SPOT_DEMO_*`` env
        vars activate this path. They are read by their respective adapters.
        """
        if not _truthy(os.environ.get("BINANCE_FUTURES_DEMO_ENABLED")):
            raise BinanceFuturesDemoDisabled(
                "BINANCE_FUTURES_DEMO_ENABLED is not truthy. Set "
                "BINANCE_FUTURES_DEMO_ENABLED=true to opt in to the Futures "
                "Demo execution path. Default is fail-closed."
            )
        # ROB-302: credentials resolve through the shared canonical pair.
        # Re-raise as the lane-specific error so fail-closed contracts hold.
        try:
            creds = resolve_demo_credentials("futures", os.environ)
        except BinanceDemoCredentialError as exc:
            raise BinanceFuturesDemoMissingCredentials(str(exc)) from exc
        base_url = os.environ.get("BINANCE_FUTURES_DEMO_BASE_URL", _DEFAULT_BASE_URL)
        return cls(
            api_key=creds.api_key, api_secret=creds.api_secret, base_url=base_url
        )

    def __repr__(self) -> str:
        # Never reference _api_secret in repr/str. api_key half is
        # fingerprinted to avoid log exposure of credentials.
        api_key_fp = (
            f"{self._api_key[:4]}…{self._api_key[-2:]}"
            if len(self._api_key) >= 6
            else "***"
        )
        return (
            f"<BinanceFuturesDemoExecutionClient base_url={self._base_url!r} "
            f"api_key={api_key_fp!r}>"
        )

    async def aclose(self) -> None:
        """Release the underlying httpx client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _new_client_order_id(self) -> str:
        """Generate a uniqueish client_order_id (UUID4-derived)."""
        return uuid.uuid4().hex

    def _validate_order_args(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None,
        time_in_force: str | None,
    ) -> None:
        """Validate order arguments before any signing/HTTP.

        Boundary validation — caller-side programming errors are rejected
        here as plain ``ValueError`` so they fail closed at the adapter
        boundary rather than relying on Binance to reject malformed
        payloads. This guards against:

          * empty ``symbol`` (would emit a signed request for a meaningless
            symbol)
          * ``qty <= 0`` (zero or negative quantity)
          * LIMIT order with ``price <= 0``
          * unknown ``side`` / ``order_type``
          * LIMIT order missing ``price`` or ``time_in_force``
          * MARKET order carrying a stray ``price``
        """
        if not symbol or not symbol.strip():
            raise ValueError("symbol must be non-empty")
        if side not in ALLOWED_SIDES:
            raise ValueError(f"side {side!r} not in {sorted(ALLOWED_SIDES)}")
        if order_type not in ALLOWED_ORDER_TYPES:
            raise ValueError(
                f"order_type {order_type!r} not in {sorted(ALLOWED_ORDER_TYPES)}"
            )
        if qty <= 0:
            raise ValueError(f"qty must be > 0, got {qty}")
        if order_type == "LIMIT":
            if price is None:
                raise ValueError("LIMIT order requires explicit price")
            if price <= 0:
                raise ValueError(f"LIMIT price must be > 0, got {price}")
            if time_in_force is None:
                raise ValueError("LIMIT order requires time_in_force (e.g. GTC)")
        if order_type == "MARKET" and price is not None:
            raise ValueError("MARKET order must not carry a price")

    def _validate_cancel_args(
        self,
        *,
        symbol: str,
        client_order_id: str,
    ) -> None:
        """Validate cancel arguments before any signing/HTTP.

        Empty ``symbol`` or ``client_order_id`` would emit a signed DELETE
        with a meaningless payload; reject at the adapter boundary.
        """
        if not symbol or not symbol.strip():
            raise ValueError("symbol must be non-empty")
        if not client_order_id or not client_order_id.strip():
            raise ValueError("client_order_id must be non-empty")

    def _build_order_params(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None,
        time_in_force: str | None,
        client_order_id: str | None,
        reduce_only: bool,
    ) -> dict[str, str]:
        """Construct the params dict that will be HMAC-signed."""
        params: dict[str, str] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": format(qty, "f"),
            "recvWindow": str(BINANCE_FUTURES_DEMO_RECV_WINDOW_MS),
        }
        if client_order_id is not None:
            params["newClientOrderId"] = client_order_id
        if order_type == "LIMIT":
            assert price is not None and time_in_force is not None
            params["price"] = format(price, "f")
            params["timeInForce"] = time_in_force
        if reduce_only:
            # Only set when True so default open-side orders never carry
            # ``reduceOnly=true``. Binance accepts the param omitted.
            params["reduceOnly"] = "true"
        return params

    # ------------------------------------------------------------------
    # Submit / Cancel — mutation entry points (operator-gated).
    # ------------------------------------------------------------------
    def preview_submit(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        client_order_id: str | None = None,
        price: Decimal | None = None,
        time_in_force: str | None = None,
        reduce_only: bool = False,
    ) -> FuturesDemoDryRunResult:
        """Pure dry-run preview. No HTTP, no signing.

        Still runs boundary validation so the same rejection contract
        applies whether the operator is previewing or confirming — a
        preview with ``qty=0`` is a caller bug, not a "harmless dry run".
        """
        self._validate_order_args(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            time_in_force=time_in_force,
        )
        cid = client_order_id or self._new_client_order_id()
        return FuturesDemoDryRunResult(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            client_order_id=cid,
            reduce_only=reduce_only,
        )

    async def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        client_order_id: str | None = None,
        price: Decimal | None = None,
        time_in_force: str | None = None,
        reduce_only: bool = False,
        confirm: bool = False,
    ) -> FuturesDemoOrderSubmitResult | FuturesDemoDryRunResult:
        """Operator-gated submit.

        Without ``confirm=True`` (the default), returns a
        ``FuturesDemoDryRunResult`` and dispatches zero HTTP. With
        ``confirm=True``, signs the params via the HMAC chokepoint and
        POSTs them to ``demo-fapi.binance.com/fapi/v1/order``.

        If ``reduce_only=True``, the signed payload includes
        ``reduceOnly=true``; otherwise the param is omitted (Binance
        defaults to ``reduceOnly=false`` on absence).
        """
        self._validate_order_args(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            time_in_force=time_in_force,
        )
        cid = client_order_id or self._new_client_order_id()
        if not confirm:
            return FuturesDemoDryRunResult(
                symbol=symbol,
                side=side,
                order_type=order_type,
                qty=qty,
                client_order_id=cid,
                reduce_only=reduce_only,
            )
        params = self._build_order_params(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            time_in_force=time_in_force,
            client_order_id=cid,
            reduce_only=reduce_only,
        )
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.post(_ORDER_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        return FuturesDemoOrderSubmitResult(
            client_order_id=str(body.get("clientOrderId", cid)),
            broker_order_id=str(body.get("orderId", "")),
            symbol=str(body.get("symbol", symbol)),
            side=str(body.get("side", side)),
            order_type=str(body.get("type", order_type)),
            qty=Decimal(str(body.get("origQty", qty))),
            executed_qty=Decimal(str(body.get("executedQty", "0"))),
            avg_price=Decimal(str(body.get("avgPrice", "0"))),
            status=str(body.get("status", "UNKNOWN")),
            reduce_only=bool(body.get("reduceOnly", reduce_only)),
            raw_response_redacted=_redact(body),
        )

    async def cancel_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
    ) -> FuturesDemoCancelResult:
        """Cancel an order by client_order_id.

        Signs and DELETEs ``demo-fapi.binance.com/fapi/v1/order``. There
        is no dry-run gate on cancel — by the time a cancel is being
        called, the operator has already committed to running against the
        broker. (Matches the model the smoke CLI uses to clean up a NEW
        order that didn't fill.)

        Boundary validation: empty ``symbol`` or ``client_order_id`` is
        rejected as ``ValueError`` before any signing/HTTP — caller bug,
        not broker-environment fail-closed.
        """
        self._validate_cancel_args(symbol=symbol, client_order_id=client_order_id)
        params = {
            "symbol": symbol,
            "origClientOrderId": client_order_id,
            "recvWindow": str(BINANCE_FUTURES_DEMO_RECV_WINDOW_MS),
        }
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.delete(_ORDER_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        return FuturesDemoCancelResult(
            client_order_id=str(body.get("clientOrderId", client_order_id)),
            broker_order_id=str(body.get("orderId", "")),
            symbol=str(body.get("symbol", symbol)),
            status=str(body.get("status", "CANCELED")),
            raw_response_redacted=_redact(body),
        )

    # ------------------------------------------------------------------
    # /fapi/v1/order/test — order-shape validation without placement.
    # ------------------------------------------------------------------
    async def order_test(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None = None,
        time_in_force: str | None = None,
        reduce_only: bool = False,
    ) -> FuturesDemoOrderTestResult:
        """POST to ``/fapi/v1/order/test`` — validates without placing.

        Binance returns 200 with an empty JSON body on success. No
        operator gate is required because this endpoint is non-mutating
        by Binance's contract; however the same HMAC + host-allowlist
        chokepoint is exercised so the call path is identical to the
        real submit (modulo path).
        """
        self._validate_order_args(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            time_in_force=time_in_force,
        )
        params = self._build_order_params(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            time_in_force=time_in_force,
            client_order_id=None,  # order/test doesn't need a client id
            reduce_only=reduce_only,
        )
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.post(_ORDER_TEST_PATH, params=signed)
        resp.raise_for_status()
        return FuturesDemoOrderTestResult(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
        )

    # ------------------------------------------------------------------
    # Read-side queries (reconciliation / status checks).
    # ------------------------------------------------------------------
    async def get_open_orders(self, *, symbol: str) -> FuturesDemoOpenOrdersResult:
        """Query open orders for ``symbol`` (read-side; no mutation)."""
        params = {
            "symbol": symbol,
            "recvWindow": str(BINANCE_FUTURES_DEMO_RECV_WINDOW_MS),
        }
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get(_OPEN_ORDERS_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        orders = [
            FuturesDemoOpenOrder(
                client_order_id=str(entry.get("clientOrderId", "")),
                broker_order_id=str(entry.get("orderId", "")),
                symbol=str(entry.get("symbol", symbol)),
                side=str(entry.get("side", "")),
                qty=Decimal(str(entry.get("origQty", "0"))),
                status=str(entry.get("status", "")),
                reduce_only=bool(entry.get("reduceOnly", False)),
            )
            for entry in body
        ]
        return FuturesDemoOpenOrdersResult(orders=orders)

    async def get_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
    ) -> FuturesDemoOrderStatusResult:
        """Query a single order's status by ``client_order_id`` (read-side).

        Signed ``GET /fapi/v1/order?symbol=&origClientOrderId=``. ROB-305 §4
        uses this to reconcile a submit response of ``status=NEW``: the smoke
        polls this endpoint (bounded) to learn whether the order actually
        ``FILLED`` before advancing the ledger past ``submitted``. Surfaces
        the broker status verbatim — no interpretation happens here.

        Boundary validation: empty ``symbol`` or ``client_order_id`` is a
        caller bug, rejected as ``ValueError`` before any signing/HTTP.
        """
        if not symbol or not symbol.strip():
            raise ValueError("symbol must be non-empty")
        if not client_order_id or not client_order_id.strip():
            raise ValueError("client_order_id must be non-empty")
        params = {
            "symbol": symbol,
            "origClientOrderId": client_order_id,
            "recvWindow": str(BINANCE_FUTURES_DEMO_RECV_WINDOW_MS),
        }
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get(_ORDER_PATH, params=signed)
        if resp.status_code == 400:
            try:
                broker_code = resp.json().get("code")
            except (AttributeError, TypeError, ValueError):
                broker_code = None
            if broker_code == -2013:
                raise BinanceDemoOrderNotFound(
                    "Futures Demo order not found for "
                    f"client_order_id={client_order_id!r}"
                )
        resp.raise_for_status()
        body = resp.json()
        return FuturesDemoOrderStatusResult(
            client_order_id=str(body.get("clientOrderId", client_order_id)),
            broker_order_id=str(body.get("orderId", "")),
            symbol=str(body.get("symbol", symbol)),
            side=str(body.get("side", "")),
            order_type=str(body.get("type", "")),
            status=str(body.get("status", "UNKNOWN")),
            orig_qty=Decimal(str(body.get("origQty", "0"))),
            executed_qty=Decimal(str(body.get("executedQty", "0"))),
            avg_price=Decimal(str(body.get("avgPrice", "0"))),
            reduce_only=bool(body.get("reduceOnly", False)),
            raw_response_redacted=_redact(body),
        )

    async def get_position(self, *, symbol: str) -> FuturesDemoPositionResult:
        """Query the current position for ``symbol`` from /fapi/v2/positionRisk.

        Returns the signed ``positionAmt`` (positive=long, negative=short,
        zero=flat) along with ``entryPrice`` and ``leverage``. Used by the
        smoke CLI to verify a position has been opened/closed.
        """
        params = {
            "symbol": symbol,
            "recvWindow": str(BINANCE_FUTURES_DEMO_RECV_WINDOW_MS),
        }
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get(_POSITION_RISK_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        # /fapi/v2/positionRisk?symbol=... returns a list; pick the matching row.
        entry: dict[str, Any] = {}
        if isinstance(body, list):
            for item in body:
                if str(item.get("symbol", "")) == symbol:
                    entry = item
                    break
            if not entry and body:
                # Defensive: take first row when no symbol match (shouldn't happen).
                entry = body[0]
        elif isinstance(body, dict):
            entry = body
        position_amt = Decimal(str(entry.get("positionAmt", "0")))
        entry_price = Decimal(str(entry.get("entryPrice", "0")))
        leverage_raw = entry.get("leverage", "0")
        try:
            leverage = int(Decimal(str(leverage_raw)))
        except (ValueError, ArithmeticError):
            leverage = 0
        return FuturesDemoPositionResult(
            symbol=str(entry.get("symbol", symbol)),
            position_amt=position_amt,
            entry_price=entry_price,
            leverage=leverage,
            is_flat=(position_amt == 0),
        )

    async def get_position_mode(self) -> FuturesDemoPositionModeResult:
        """Query the account's position mode (One-way vs Hedge).

        Returns ``is_hedge_mode`` based on Binance's ``dualSidePosition``
        field. This client does NOT raise on Hedge mode — that's the
        CLI/operator's gate (PR 2 only supports One-way at the operator
        level). The client just surfaces the flag.
        """
        params = {"recvWindow": str(BINANCE_FUTURES_DEMO_RECV_WINDOW_MS)}
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get(_POSITION_SIDE_DUAL_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        return FuturesDemoPositionModeResult(
            is_hedge_mode=bool(body.get("dualSidePosition", False)),
        )

    async def set_leverage(
        self,
        *,
        symbol: str,
        leverage: int,
    ) -> FuturesDemoLeverageResult:
        """Set leverage for ``symbol`` via POST /fapi/v1/leverage.

        ROB-298 PR 2 pins leverage to ``1`` exactly (locked design
        decision, see ROB-298 comment d258c471 — "leverage: 1x 강제").
        Any other requested value is rejected at the adapter boundary
        BEFORE the signed POST is dispatched — this is the structural
        guard against accidentally requesting >1x leverage on the demo
        account. The existing post-HTTP echo check remains as defense
        in depth.

        Also rejects empty ``symbol`` (caller bug; would emit a
        meaningless signed request).
        """
        if not symbol or not symbol.strip():
            raise ValueError("symbol must be non-empty")
        if leverage != 1:
            raise BinanceFuturesDemoLeverageMismatch(
                f"Futures Demo set_leverage refused: leverage={leverage} "
                "but ROB-298 PR 2 pins leverage=1 exactly. Refusing to "
                "dispatch signed POST."
            )
        params = {
            "symbol": symbol,
            "leverage": str(leverage),
            "recvWindow": str(BINANCE_FUTURES_DEMO_RECV_WINDOW_MS),
        }
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.post(_LEVERAGE_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        # Echo verification — Binance returns the now-set leverage.
        echoed_raw = body.get("leverage")
        try:
            echoed = int(Decimal(str(echoed_raw)))
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise BinanceFuturesDemoLeverageMismatch(
                f"Futures Demo /fapi/v1/leverage echo for symbol={symbol!r} "
                f"could not be parsed as int (raw={echoed_raw!r}); refusing to "
                "proceed."
            ) from exc
        if echoed != leverage:
            raise BinanceFuturesDemoLeverageMismatch(
                f"Futures Demo /fapi/v1/leverage echo mismatch for symbol={symbol!r}: "
                f"requested {leverage}, Binance echoed {echoed}. Smoke contract "
                "pins exact match; refusing to proceed."
            )
        max_notional_raw = body.get("maxNotionalValue", "0")
        try:
            max_notional = Decimal(str(max_notional_raw))
        except (TypeError, ValueError, ArithmeticError):
            max_notional = Decimal("0")
        return FuturesDemoLeverageResult(
            symbol=str(body.get("symbol", symbol)),
            leverage=echoed,
            max_notional_value=max_notional,
        )


def _redact(payload: Any) -> dict[str, Any]:
    """Strip keys that could carry credential material.

    Binance order responses are mostly safe (they echo back order
    metadata, not credentials), but defensively redact anything that
    looks like a credential bearer so a future Binance API change can't
    silently leak through this surface.
    """
    if not isinstance(payload, dict):
        return {"_raw": "<non-dict response>"}
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if key.lower() in {"apikey", "api_key", "secret", "signature"}:
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = value
    return redacted
