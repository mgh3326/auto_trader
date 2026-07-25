"""ROB-298 — Binance Spot Demo execution backend.

Mutation-capable signed adapter for ``demo-api.binance.com``. Self-
contained — the prior ``binance.testnet.execution_client`` sibling was
removed in ROB-298. Uses the Spot Demo env namespace, host allowlist,
exception hierarchy, and HMAC chokepoint, all environment-specific by
design (see ``spot_demo/__init__.py`` for the rationale).

Hard invariants enforced here:

  * Default-disabled — ``BINANCE_SPOT_DEMO_ENABLED=true`` required at
    construction. Defaults raise ``BinanceSpotDemoDisabled``.
  * Fail-closed credentials — empty key or secret raises
    ``BinanceSpotDemoMissingCredentials``.
  * Host allowlist — ``base_url`` host must be in ``SPOT_DEMO_HOSTS``;
    enforced both at construction (via the transport factory) and on
    every request (via the per-request event hook). Live or testnet
    hosts raise ``BinanceSpotDemoCrossAllowlistViolation``.
  * Per-call operator gate — ``submit_order(..., confirm=False)`` (the
    default) returns a ``SpotDemoDryRunResult`` and dispatches zero
    HTTP. Only ``confirm=True`` routes the signed POST through the
    HMAC chokepoint to ``/api/v3/order``.
  * Secret hygiene — the API secret lives on a single private
    attribute (``_api_secret``) and is never read by ``repr``, log
    messages, or error strings. Mirrors the testnet contract verified
    by ``test_secret_is_not_in_repr`` / leak-probe tests.

Distinct from preflight: ``preflight.py`` covers the read-only
``GET /api/v3/account`` smoke path. This module is the mutation surface
and is the only place under ``spot_demo/`` that signs POST/DELETE
requests.
"""

from __future__ import annotations

import logging
import os
import uuid
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
from app.services.brokers.binance.spot_demo.dto import (
    SpotDemoAssetBalance,
    SpotDemoCancelResult,
    SpotDemoOpenOrder,
    SpotDemoOpenOrdersResult,
    SpotDemoOrderSubmitResult,
    SpotDemoOrderTestResult,
)
from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoDisabled,
    BinanceSpotDemoMissingCredentials,
)
from app.services.brokers.binance.spot_demo.signing import (
    BINANCE_SPOT_DEMO_RECV_WINDOW_MS,
    _sign_request_params,
)
from app.services.brokers.binance.spot_demo.transport import build_spot_demo_client

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL: Final[str] = "https://demo-api.binance.com"
_ORDER_PATH: Final[str] = "/api/v3/order"
_ORDER_TEST_PATH: Final[str] = "/api/v3/order/test"
_OPEN_ORDERS_PATH: Final[str] = "/api/v3/openOrders"
_ACCOUNT_PATH: Final[str] = "/api/v3/account"
ALLOWED_SIDES: Final[frozenset[str]] = frozenset({"BUY", "SELL"})
ALLOWED_ORDER_TYPES: Final[frozenset[str]] = frozenset({"LIMIT", "MARKET"})


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


class SpotDemoDryRunResult:
    """Operator-gate dry-run sentinel.

    Returned by mutation entry points (``submit_order``, ``cancel_order``)
    when ``confirm=False`` (the default). Contains enough metadata for
    the caller to audit the intended action without any HTTP having been
    dispatched. Carries no signed payload because no signing occurred.
    """

    __slots__ = (
        "symbol",
        "side",
        "order_type",
        "qty",
        "client_order_id",
        "reason",
    )

    def __init__(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        client_order_id: str,
        reason: str = "confirm=False — operator gate not passed; no HTTP attempted",
    ) -> None:
        self.symbol = symbol
        self.side = side
        self.order_type = order_type
        self.qty = qty
        self.client_order_id = client_order_id
        self.reason = reason

    def __repr__(self) -> str:
        return (
            f"<SpotDemoDryRunResult symbol={self.symbol!r} side={self.side!r} "
            f"order_type={self.order_type!r} qty={self.qty} "
            f"client_order_id={self.client_order_id!r}>"
        )


class BinanceSpotDemoExecutionClient:
    """Signed adapter for the Binance Spot Demo endpoint.

    Construct via ``BinanceSpotDemoExecutionClient.from_env()`` in
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
            raise BinanceSpotDemoMissingCredentials(
                "BINANCE_SPOT_DEMO_API_KEY is empty. Refusing to construct "
                "Spot Demo execution client."
            )
        if not api_secret:
            raise BinanceSpotDemoMissingCredentials(
                "BINANCE_SPOT_DEMO_API_SECRET is empty. Refusing to construct "
                "Spot Demo execution client."
            )
        # Transport factory enforces the host-allowlist check on base_url;
        # raises BinanceSpotDemoCrossAllowlistViolation for testnet/live,
        # BinanceLiveHostBlocked for any other non-Spot-Demo host.
        self._client = build_spot_demo_client(
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
    def from_env(cls) -> BinanceSpotDemoExecutionClient:
        """Construct from environment variables with full fail-closed checks.

        Env contract:
          * ``BINANCE_SPOT_DEMO_ENABLED`` MUST be truthy.
          * Credentials (ROB-302): the ``BINANCE_SPOT_DEMO_API_*`` pair
            OR the canonical ``BINANCE_DEMO_API_*`` pair MUST be present
            (the per-product pair wins when set). A half-set pair fails
            closed. Resolved via ``demo.credentials.resolve_demo_credentials``.
          * ``BINANCE_SPOT_DEMO_BASE_URL`` (optional) MUST be a Spot Demo
            host if set; the transport factory enforces.

        Note: ``BINANCE_TESTNET_*`` env vars are ignored. The testnet
        adapter was retired in ROB-298 and is not re-enabled by setting
        those vars.
        """
        if not _truthy(os.environ.get("BINANCE_SPOT_DEMO_ENABLED")):
            raise BinanceSpotDemoDisabled(
                "BINANCE_SPOT_DEMO_ENABLED is not truthy. Set "
                "BINANCE_SPOT_DEMO_ENABLED=true to opt in to the Spot Demo "
                "execution path. Default is fail-closed."
            )
        # ROB-302: credentials resolve through the shared canonical pair.
        # Additive — byte-identical when BINANCE_SPOT_DEMO_API_* is set.
        try:
            creds = resolve_demo_credentials("spot", os.environ)
        except BinanceDemoCredentialError as exc:
            raise BinanceSpotDemoMissingCredentials(str(exc)) from exc
        base_url = os.environ.get("BINANCE_SPOT_DEMO_BASE_URL", _DEFAULT_BASE_URL)
        return cls(
            api_key=creds.api_key, api_secret=creds.api_secret, base_url=base_url
        )

    def __repr__(self) -> str:
        # Use only the one-way identity; raw key substrings and the secret must
        # never cross the logging boundary.
        return (
            f"<BinanceSpotDemoExecutionClient base_url={self._base_url!r} "
            f"credential_fingerprint={self.credential_fingerprint!r}>"
        )

    async def aclose(self) -> None:
        """Release the underlying httpx client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _new_client_order_id(self) -> str:
        """Generate a uniqueish client_order_id (UUID4-derived).

        Binance accepts up to ~36 chars; UUID4 hex (32 chars) is within
        bounds.
        """
        return uuid.uuid4().hex

    def _validate_order_args(
        self,
        *,
        side: str,
        order_type: str,
        price: Decimal | None,
        time_in_force: str | None,
    ) -> None:
        if side not in ALLOWED_SIDES:
            raise ValueError(f"side {side!r} not in {sorted(ALLOWED_SIDES)}")
        if order_type not in ALLOWED_ORDER_TYPES:
            raise ValueError(
                f"order_type {order_type!r} not in {sorted(ALLOWED_ORDER_TYPES)}"
            )
        if order_type == "LIMIT":
            if price is None:
                raise ValueError("LIMIT order requires explicit price")
            if time_in_force is None:
                raise ValueError("LIMIT order requires time_in_force (e.g. GTC)")
        if order_type == "MARKET" and price is not None:
            raise ValueError("MARKET order must not carry a price")

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
    ) -> dict[str, str]:
        """Construct the params dict that will be HMAC-signed."""
        params: dict[str, str] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": format(qty, "f"),
            "recvWindow": str(BINANCE_SPOT_DEMO_RECV_WINDOW_MS),
        }
        if client_order_id is not None:
            params["newClientOrderId"] = client_order_id
        if order_type == "LIMIT":
            assert price is not None and time_in_force is not None
            params["price"] = format(price, "f")
            params["timeInForce"] = time_in_force
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
    ) -> SpotDemoDryRunResult:
        """Pure dry-run preview. No HTTP, no signing.

        Returns the prospective ``SpotDemoDryRunResult`` so the operator
        can audit the intended action without dispatching.
        """
        cid = client_order_id or self._new_client_order_id()
        return SpotDemoDryRunResult(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            client_order_id=cid,
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
        confirm: bool = False,
    ) -> SpotDemoOrderSubmitResult | SpotDemoDryRunResult:
        """Operator-gated submit.

        Without ``confirm=True`` (the default), returns a
        ``SpotDemoDryRunResult`` and dispatches zero HTTP. With
        ``confirm=True``, signs the params via the HMAC chokepoint and
        POSTs them to ``demo-api.binance.com/api/v3/order``.
        """
        self._validate_order_args(
            side=side,
            order_type=order_type,
            price=price,
            time_in_force=time_in_force,
        )
        cid = client_order_id or self._new_client_order_id()
        if not confirm:
            return SpotDemoDryRunResult(
                symbol=symbol,
                side=side,
                order_type=order_type,
                qty=qty,
                client_order_id=cid,
            )
        params = self._build_order_params(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            time_in_force=time_in_force,
            client_order_id=cid,
        )
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.post(_ORDER_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        return SpotDemoOrderSubmitResult(
            client_order_id=body.get("clientOrderId", cid),
            broker_order_id=str(body.get("orderId", "")),
            symbol=body.get("symbol", symbol),
            side=body.get("side", side),
            order_type=body.get("type", order_type),
            qty=Decimal(str(body.get("origQty", qty))),
            executed_qty=Decimal(str(body.get("executedQty", "0"))),
            cummulative_quote_qty=Decimal(str(body.get("cummulativeQuoteQty", "0"))),
            fee_usdt=_spot_fee_usdt(body, symbol=str(body.get("symbol", symbol))),
            status=body.get("status", "UNKNOWN"),
            raw_response_redacted=_redact(body),
        )

    async def cancel_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
        confirm: bool = False,
    ) -> SpotDemoCancelResult | SpotDemoDryRunResult:
        """Operator-gated cancel.

        Without ``confirm=True`` (the default), returns a
        ``SpotDemoDryRunResult`` and dispatches zero HTTP. With
        ``confirm=True``, signs and DELETEs
        ``demo-api.binance.com/api/v3/order``.
        """
        if not confirm:
            return SpotDemoDryRunResult(
                symbol=symbol,
                side="-",
                order_type="-",
                qty=Decimal("0"),
                client_order_id=client_order_id,
            )
        params = {
            "symbol": symbol,
            "origClientOrderId": client_order_id,
            "recvWindow": str(BINANCE_SPOT_DEMO_RECV_WINDOW_MS),
        }
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.delete(_ORDER_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        return SpotDemoCancelResult(
            client_order_id=body.get("clientOrderId", client_order_id),
            broker_order_id=str(body.get("orderId", "")),
            symbol=body.get("symbol", symbol),
            status=body.get("status", "CANCELED"),
            raw_response_redacted=_redact(body),
        )

    # ------------------------------------------------------------------
    # /api/v3/order/test — order-shape validation without placement.
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
    ) -> SpotDemoOrderTestResult:
        """POST to ``/api/v3/order/test`` — validates without placing.

        Binance returns 200 with an empty JSON body on success. No
        operator gate is required because this endpoint is non-mutating
        by Binance's contract; however the same HMAC + host-allowlist
        chokepoint is exercised so the call path is identical to the
        real submit (modulo path).
        """
        self._validate_order_args(
            side=side,
            order_type=order_type,
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
        )
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.post(_ORDER_TEST_PATH, params=signed)
        resp.raise_for_status()
        return SpotDemoOrderTestResult(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
        )

    # ------------------------------------------------------------------
    # Read-side queries (reconciliation / status checks).
    # ------------------------------------------------------------------
    async def get_open_orders(self, *, symbol: str) -> SpotDemoOpenOrdersResult:
        """Query open orders for ``symbol`` (read-side; no mutation)."""
        params = {
            "symbol": symbol,
            "recvWindow": str(BINANCE_SPOT_DEMO_RECV_WINDOW_MS),
        }
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get(_OPEN_ORDERS_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        orders = [
            SpotDemoOpenOrder(
                client_order_id=str(entry.get("clientOrderId", "")),
                broker_order_id=str(entry.get("orderId", "")),
                symbol=str(entry.get("symbol", symbol)),
                side=str(entry.get("side", "")),
                qty=Decimal(str(entry.get("origQty", "0"))),
                status=str(entry.get("status", "")),
            )
            for entry in body
        ]
        return SpotDemoOpenOrdersResult(orders=orders)

    async def get_order_status(
        self, *, symbol: str, client_order_id: str
    ) -> dict[str, Any]:
        """Query a specific order's status by client_order_id (read-side).

        Returns the redacted broker response dict; callers map fields as
        needed. No DTO wrap here because the status payload shape varies
        with order type (LIMIT vs STOP_*) and the ledger layer needs the
        full raw shape for state-machine decisions.
        """
        params = {
            "symbol": symbol,
            "origClientOrderId": client_order_id,
            "recvWindow": str(BINANCE_SPOT_DEMO_RECV_WINDOW_MS),
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
                    f"Spot Demo order not found for client_order_id={client_order_id!r}"
                )
        resp.raise_for_status()
        return _redact(resp.json())

    async def get_asset_balance(self, *, asset: str) -> SpotDemoAssetBalance:
        """Signed ``GET /api/v3/account``; return only ``asset``'s free/locked.

        Narrow by design: every other balance row and all account-level
        flags are dropped here so the full account payload never reaches a
        caller, log line, or evidence file. If the account holds none of
        ``asset``, returns zero free/locked (absence == zero, not an error).
        Read-side only — no mutation, no operator gate.
        """
        params = {"recvWindow": str(BINANCE_SPOT_DEMO_RECV_WINDOW_MS)}
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get(_ACCOUNT_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        for entry in body.get("balances") or []:
            if entry.get("asset") == asset:
                return SpotDemoAssetBalance(
                    asset=asset,
                    free=Decimal(str(entry.get("free", "0"))),
                    locked=Decimal(str(entry.get("locked", "0"))),
                )
        return SpotDemoAssetBalance(asset=asset, free=Decimal("0"), locked=Decimal("0"))


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


def _spot_fee_usdt(payload: Any, *, symbol: str) -> Decimal | None:
    """Normalize native Spot fill commissions into the order's USDT quote.

    Base commission is exactly convertible with that fill's native price;
    quote commission is already USDT. Third-asset commission needs separate
    market evidence, so this boundary returns ``None`` instead of inventing a
    conversion.
    """
    if not isinstance(payload, dict) or not symbol.endswith("USDT"):
        return None
    fills = payload.get("fills")
    if not isinstance(fills, list) or not fills:
        return None
    base_asset = symbol.removesuffix("USDT")
    total = Decimal("0")
    for fill in fills:
        if not isinstance(fill, dict):
            return None
        try:
            commission = Decimal(str(fill["commission"]))
            price = Decimal(str(fill["price"]))
        except (KeyError, ArithmeticError, ValueError):
            return None
        if (
            not commission.is_finite()
            or commission < 0
            or not price.is_finite()
            or price <= 0
        ):
            return None
        commission_asset = fill.get("commissionAsset")
        if commission_asset == "USDT":
            total += commission
        elif commission_asset == base_asset:
            total += commission * price
        else:
            return None
    return total
