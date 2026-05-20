"""ROB-286 — Binance Spot testnet execution client (signed adapter).

The only signed-endpoint surface in this codebase. Structurally
testnet-only: the class name is ``BinanceTestnetExecutionClient`` (no
``Live`` variant exists or can be substituted), the underlying transport
factory refuses any base URL whose host is not in ``TESTNET_HOSTS``, and
every submission requires a per-call ``confirm=True`` flag.

Hard invariants enforced here:
  * #4 (fail-closed credentials) — ``__init__`` raises
    ``BinanceMissingCredentials`` if either credential is empty.
  * #5/#6 (testnet-only + default disabled) — ``from_env()`` requires
    ``BINANCE_TESTNET_ENABLED=true``; defaults raise
    ``BinanceTestnetDisabled``.
  * #8 (operator gate) — submission methods default to
    ``dry_run=True, confirm=False`` and return ``DryRunResult``; no HTTP
    is performed without an explicit ``confirm=True``.

The secret string is stored on a private attribute (``_api_secret``) and
never appears in ``repr``, log messages, or error strings (defensively
verified by ``test_secret_is_not_in_repr`` /
``test_secret_not_in_logs_on_init_failure``).
"""

from __future__ import annotations

import logging
import os
import uuid
from decimal import Decimal
from typing import Final

from app.services.brokers.binance.testnet.dto import (
    CancelResult,
    DryRunResult,
    OrderPreview,
)
from app.services.brokers.binance.testnet.errors import (
    BinanceMissingCredentials,
    BinanceTestnetDisabled,
)
from app.services.brokers.binance.testnet.signing import (
    BINANCE_RECV_WINDOW_MS,
    _sign_request_params,
)
from app.services.brokers.binance.testnet.transport import build_testnet_client

logger = logging.getLogger(__name__)

DEFAULT_MAX_NOTIONAL_USDT: Final[Decimal] = Decimal("10")
ALLOWED_SIDES: Final[frozenset[str]] = frozenset({"BUY", "SELL"})
ALLOWED_ORDER_TYPES: Final[frozenset[str]] = frozenset({"LIMIT", "MARKET"})


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


class BinanceTestnetExecutionClient:
    """Signed adapter for Binance Spot testnet.

    Construct via ``BinanceTestnetExecutionClient.from_env()`` in
    production code paths (does the fail-closed env-validation in one
    place); direct ``__init__`` is exposed for tests that want to inject
    fake credentials.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = "https://testnet.binance.vision",
        max_notional_usdt: Decimal = DEFAULT_MAX_NOTIONAL_USDT,
    ) -> None:
        if not api_key:
            raise BinanceMissingCredentials(
                "BINANCE_TESTNET_API_KEY is empty. Refusing to construct adapter."
            )
        if not api_secret:
            raise BinanceMissingCredentials(
                "BINANCE_TESTNET_API_SECRET is empty. Refusing to construct adapter."
            )
        # Transport factory does the base_url host-allowlist check.
        self._client = build_testnet_client(
            api_key=api_key, api_secret=api_secret, base_url=base_url
        )
        # _api_secret is the ONLY persistent reference to the secret.
        # repr/str/log paths must never read this attribute directly.
        self._api_secret = api_secret
        # _api_key kept private too, although it would only leak the
        # public half of the credential pair.
        self._api_key = api_key
        self._base_url = base_url
        self._max_notional_usdt = max_notional_usdt

    @classmethod
    def from_env(cls) -> BinanceTestnetExecutionClient:
        """Construct from environment variables with full fail-closed checks.

        Env contract:
          * ``BINANCE_TESTNET_ENABLED`` MUST be truthy.
          * ``BINANCE_TESTNET_API_KEY`` MUST be present and non-empty.
          * ``BINANCE_TESTNET_API_SECRET`` MUST be present and non-empty.
          * ``BINANCE_TESTNET_BASE_URL`` (optional) MUST be a testnet host
            if set; the transport factory enforces this.
          * ``BINANCE_TESTNET_MAX_NOTIONAL_USDT`` (optional, default 10).
        """
        if not _truthy(os.environ.get("BINANCE_TESTNET_ENABLED")):
            raise BinanceTestnetDisabled(
                "BINANCE_TESTNET_ENABLED is not truthy. Set "
                "BINANCE_TESTNET_ENABLED=true to opt in to the testnet "
                "execution path. Default is fail-closed."
            )
        api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
        api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
        if not api_key:
            raise BinanceMissingCredentials(
                "BINANCE_TESTNET_API_KEY is empty or missing. Refusing to construct."
            )
        if not api_secret:
            raise BinanceMissingCredentials(
                "BINANCE_TESTNET_API_SECRET is empty or missing. Refusing to construct."
            )
        base_url = os.environ.get(
            "BINANCE_TESTNET_BASE_URL", "https://testnet.binance.vision"
        )
        max_notional_raw = os.environ.get("BINANCE_TESTNET_MAX_NOTIONAL_USDT", "10")
        try:
            max_notional = Decimal(max_notional_raw)
        except Exception as exc:
            raise ValueError(
                f"BINANCE_TESTNET_MAX_NOTIONAL_USDT={max_notional_raw!r} is not a "
                "valid Decimal."
            ) from exc
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            base_url=base_url,
            max_notional_usdt=max_notional,
        )

    def __repr__(self) -> str:
        # Hard reviewer focus #8 — never reference _api_secret in repr/str.
        # The api_key half is also redacted to a fingerprint to avoid log
        # spam exposing credentials in error pages.
        api_key_fp = (
            f"{self._api_key[:4]}…{self._api_key[-2:]}"
            if len(self._api_key) >= 6
            else "***"
        )
        return (
            f"<BinanceTestnetExecutionClient base_url={self._base_url!r} "
            f"api_key={api_key_fp!r} max_notional_usdt={self._max_notional_usdt}>"
        )

    async def aclose(self) -> None:
        """Release the underlying httpx client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Validation helpers (separated so fail-closed tests can hit them
    # without constructing fake HTTP exchanges).
    # ------------------------------------------------------------------
    def _validate_notional(
        self,
        *,
        notional_usdt: Decimal,
        override_reason: str | None,
    ) -> None:
        """Enforce the max-notional + override-reason forcing function.

        Raise ``ValueError`` if the requested notional exceeds the
        per-adapter cap and no ``override_reason`` was provided.
        """
        if notional_usdt <= self._max_notional_usdt:
            return
        if not override_reason:
            raise ValueError(
                f"Requested notional {notional_usdt} USDT exceeds the configured "
                f"max ({self._max_notional_usdt} USDT). Supply "
                "notional_override_reason=<string> on this call to override; "
                "the value is recorded on the ledger row for audit."
            )

    def _new_client_order_id(self) -> str:
        """Generate a uniqueish client_order_id (UUID4-derived).

        Binance accepts up to ~36 chars; UUID4 hex (32 chars) is well
        within bounds.
        """
        return uuid.uuid4().hex

    def _build_preview(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        notional_usdt: Decimal,
        client_order_id: str | None,
    ) -> OrderPreview:
        if side not in ALLOWED_SIDES:
            raise ValueError(f"side {side!r} not in {sorted(ALLOWED_SIDES)}")
        if order_type not in ALLOWED_ORDER_TYPES:
            raise ValueError(
                f"order_type {order_type!r} not in {sorted(ALLOWED_ORDER_TYPES)}"
            )
        if order_type == "LIMIT" and price is None:
            raise ValueError("LIMIT order requires explicit price")
        if order_type == "MARKET" and price is not None:
            raise ValueError("MARKET order must not carry a price")
        cid = client_order_id or self._new_client_order_id()
        # Template of params the signed transport would send. The actual
        # signature is computed at submit time so timestamps stay current.
        template: dict[str, str] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": str(quantity),
            "newClientOrderId": cid,
            "recvWindow": str(BINANCE_RECV_WINDOW_MS),
        }
        if price is not None:
            template["price"] = str(price)
            template["timeInForce"] = "GTC"
        return OrderPreview(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            notional_usdt=notional_usdt,
            client_order_id=cid,
            signed_payload_template=template,
        )

    # ------------------------------------------------------------------
    # Dry-run paths (Task 5 — no HTTP)
    # ------------------------------------------------------------------
    async def preview_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        notional_usdt: Decimal,
        client_order_id: str | None = None,
        notional_override_reason: str | None = None,
    ) -> OrderPreview:
        """Pure-validation preview. No HTTP. Returns the prospective preview."""
        self._validate_notional(
            notional_usdt=notional_usdt, override_reason=notional_override_reason
        )
        return self._build_preview(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            notional_usdt=notional_usdt,
            client_order_id=client_order_id,
        )

    async def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        notional_usdt: Decimal,
        client_order_id: str | None = None,
        notional_override_reason: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> DryRunResult:
        """Operator-gated submit. Without ``confirm=True``, no HTTP is sent.

        Returns ``DryRunResult`` when ``confirm=False`` (the default).
        The confirmed-submit codepath is implemented in Task 6.
        """
        self._validate_notional(
            notional_usdt=notional_usdt, override_reason=notional_override_reason
        )
        preview = self._build_preview(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            notional_usdt=notional_usdt,
            client_order_id=client_order_id,
        )
        if not confirm:
            return DryRunResult(
                preview=preview,
                reason="confirm=False — operator gate not passed; no HTTP attempted",
            )
        if not dry_run:
            # Confirmed live-submit path lands in Task 6 (test_execution_client_submit_cancel_fake).
            return await self._submit_confirmed(preview=preview)
        # confirm=True + dry_run=True is treated as "preview only".
        return DryRunResult(
            preview=preview,
            reason="dry_run=True with confirm=True; treated as preview",
        )

    async def cancel_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> DryRunResult | CancelResult:
        """Operator-gated cancel. Without ``confirm=True``, no HTTP is sent."""
        # Build a synthetic preview for symmetry with submit_order's
        # DryRunResult payload (callers can audit the intended cancel).
        preview = OrderPreview(
            symbol=symbol,
            side="-",
            order_type="-",
            quantity=Decimal("0"),
            price=None,
            notional_usdt=Decimal("0"),
            client_order_id=client_order_id,
            signed_payload_template={
                "symbol": symbol,
                "origClientOrderId": client_order_id,
                "recvWindow": str(BINANCE_RECV_WINDOW_MS),
            },
        )
        if not confirm:
            return DryRunResult(
                preview=preview,
                reason="confirm=False — operator gate not passed; no HTTP attempted",
            )
        if dry_run:
            return DryRunResult(
                preview=preview,
                reason="dry_run=True with confirm=True; treated as preview",
            )
        return await self._cancel_confirmed(
            symbol=symbol, client_order_id=client_order_id
        )

    # ------------------------------------------------------------------
    # Confirmed-submit codepaths (Task 6 — fake-client HTTP).
    # ------------------------------------------------------------------
    async def _submit_confirmed(self, *, preview: OrderPreview):
        """Confirmed live-submit. Implemented in Task 6."""
        from app.services.brokers.binance.testnet.dto import OrderSubmitResult

        params = dict(preview.signed_payload_template)
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.post(
            "/api/v3/order",
            params=signed,
        )
        resp.raise_for_status()
        body = resp.json()
        return OrderSubmitResult(
            client_order_id=body.get("clientOrderId", preview.client_order_id),
            broker_order_id=str(body.get("orderId", "")),
            symbol=body.get("symbol", preview.symbol),
            side=body.get("side", preview.side),
            order_type=body.get("type", preview.order_type),
            quantity=Decimal(str(body.get("origQty", preview.quantity))),
            price=(
                Decimal(str(body["price"]))
                if body.get("price") and body["price"] != "0.00000000"
                else None
            ),
            status=body.get("status", "UNKNOWN"),
            transact_time_ms=int(body.get("transactTime", 0)),
            raw_response=body,
        )

    async def _cancel_confirmed(
        self, *, symbol: str, client_order_id: str
    ) -> CancelResult:
        params = {
            "symbol": symbol,
            "origClientOrderId": client_order_id,
            "recvWindow": str(BINANCE_RECV_WINDOW_MS),
        }
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.delete("/api/v3/order", params=signed)
        resp.raise_for_status()
        body = resp.json()
        return CancelResult(
            client_order_id=body.get("clientOrderId", client_order_id),
            broker_order_id=str(body.get("orderId", "")),
            symbol=body.get("symbol", symbol),
            status=body.get("status", "UNKNOWN"),
            raw_response=body,
        )

    # ------------------------------------------------------------------
    # Read-side queries (used by reconciliation in Task 11).
    # ------------------------------------------------------------------
    async def open_orders(self, *, symbol: str) -> list[dict[str, object]]:
        """Query open orders for ``symbol`` (read-side; no mutation)."""
        params = {"symbol": symbol, "recvWindow": str(BINANCE_RECV_WINDOW_MS)}
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get("/api/v3/openOrders", params=signed)
        resp.raise_for_status()
        body = resp.json()
        # Binance returns a list.
        return list(body)

    async def recent_fills(
        self, *, symbol: str, limit: int = 100
    ) -> list[dict[str, object]]:
        """Query the last ``limit`` fills for ``symbol``."""
        params = {
            "symbol": symbol,
            "limit": str(limit),
            "recvWindow": str(BINANCE_RECV_WINDOW_MS),
        }
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get("/api/v3/myTrades", params=signed)
        resp.raise_for_status()
        return list(resp.json())
