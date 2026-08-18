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
  * positionSide (ROB-1288) — ``submit_order``/``order_test`` accept an
    explicit ``position_side``; when stated it is sent verbatim and the
    response must echo it exactly, or
    ``BinanceFuturesDemoPositionSideMismatch`` is raised (an absent echo
    counts as a mismatch). Hedge-mode values are refused pre-HTTP by
    ``BinanceFuturesDemoHedgeModeBlocked`` — One-way only stands. The
    position readbacks preserve each row's ``positionSide``. 🔴 Nowhere is
    the value defaulted or inferred from a quantity sign; contract v2 §4.3
    forbids it, so absence fails closed instead.
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
    BinanceFuturesDemoHedgeModeBlocked,
    BinanceFuturesDemoLeverageMismatch,
    BinanceFuturesDemoMissingCredentials,
    BinanceFuturesDemoPositionSideMismatch,
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

# ROB-1288 — Binance's positionSide vocabulary. "BOTH" is the One-way value;
# "LONG"/"SHORT" only exist under Hedge mode, which this adapter does not
# support (see BinanceFuturesDemoHedgeModeBlocked). Both sets are spelled out
# so a caller passing a Hedge value gets the *hedge-blocked* error rather than
# a vague "unknown value" — the distinction matters when reading a failure.
ONE_WAY_POSITION_SIDE: Final[str] = "BOTH"
HEDGE_POSITION_SIDES: Final[frozenset[str]] = frozenset({"LONG", "SHORT"})
KNOWN_POSITION_SIDES: Final[frozenset[str]] = (
    frozenset({ONE_WAY_POSITION_SIDE}) | HEDGE_POSITION_SIDES
)


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _extract_position_side(payload: Any) -> str | None:
    """Read ``positionSide`` out of a broker payload, or ``None`` if absent.

    🔴 ROB-1288 / contract v2 §4.3 — this function has exactly one job:
    surface what Binance sent. It never falls back to a default, and it never
    looks at ``positionAmt``/``origQty``/``side`` to guess. A row with no
    ``positionSide`` is reported as missing, and the caller decides whether
    missing is fatal (it is, on any path that needs the value).
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("positionSide")
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


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
    # ROB-1288: the positionSide the operator stated for this prospective
    # order (``None`` = not stated, and nothing is substituted for it).
    position_side: str | None = None
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
        # Use only the one-way identity; raw key substrings and the secret must
        # never cross the logging boundary.
        return (
            f"<BinanceFuturesDemoExecutionClient base_url={self._base_url!r} "
            f"credential_fingerprint={self.credential_fingerprint!r}>"
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
        position_side: str | None = None,
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
          * a ``position_side`` this adapter cannot honour (see
            ``_validate_position_side``)
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
        self._validate_position_side(position_side)

    @staticmethod
    def _validate_position_side(position_side: str | None) -> None:
        """Reject a ``position_side`` this adapter cannot honour, pre-HTTP.

        ROB-1288. Three outcomes, in increasing severity:

          * ``None`` — the caller stated nothing. Accepted: the param is
            omitted from the signed payload and no echo check runs. 🔴 Nothing
            is substituted; the adapter does not pick a side on the caller's
            behalf, which is the inference contract v2 §4.3 forbids.
          * ``"BOTH"`` — the One-way value. Accepted, sent, and echo-verified.
          * ``"LONG"`` / ``"SHORT"`` — Hedge-mode values. Rejected with
            ``BinanceFuturesDemoHedgeModeBlocked`` *before* any signing or
            HTTP: this adapter is One-way only (ROB-298 PR 2), and a
            Hedge-side order is out of scope rather than merely malformed.
          * anything else (wrong case, unknown token, non-string) — a caller
            bug, rejected as ``ValueError``. Note the deliberate absence of
            normalisation: ``"both"`` is not quietly upper-cased, because
            silently rewriting the one field whose exact value is being
            echo-verified would defeat the verification.
        """
        if position_side is None:
            return
        if not isinstance(position_side, str) or position_side not in (
            KNOWN_POSITION_SIDES
        ):
            raise ValueError(
                f"position_side {position_side!r} not in "
                f"{sorted(KNOWN_POSITION_SIDES)} (exact case required)"
            )
        if position_side in HEDGE_POSITION_SIDES:
            raise BinanceFuturesDemoHedgeModeBlocked(
                f"position_side={position_side!r} is a Hedge-mode value. "
                "ROB-298 PR 2 supports One-way mode only "
                f"(positionSide={ONE_WAY_POSITION_SIDE!r}); refusing to sign "
                "or dispatch."
            )

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
        position_side: str | None = None,
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
        if position_side is not None:
            # ROB-1288: sent verbatim, exactly as the caller stated it. The
            # param is absent when the caller stated nothing — absent, not
            # defaulted (contract v2 §4.3).
            params["positionSide"] = position_side
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
        position_side: str | None = None,
    ) -> FuturesDemoDryRunResult:
        """Pure dry-run preview. No HTTP, no signing.

        Still runs boundary validation so the same rejection contract
        applies whether the operator is previewing or confirming — a
        preview with ``qty=0`` is a caller bug, not a "harmless dry run".
        The same holds for ``position_side``: a Hedge-mode value is refused
        here too, so the preview cannot advertise an order the confirm path
        would reject.
        """
        self._validate_order_args(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            time_in_force=time_in_force,
            position_side=position_side,
        )
        cid = client_order_id or self._new_client_order_id()
        return FuturesDemoDryRunResult(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            client_order_id=cid,
            reduce_only=reduce_only,
            position_side=position_side,
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
        position_side: str | None = None,
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

        ``position_side`` (ROB-1288) is the explicit ``positionSide`` for
        this order. It is optional and defaults to ``None`` — an unstated
        side is sent as an absent param, never as a guessed one. When it IS
        stated, the value is sent verbatim and the response must echo that
        exact value back; a differing echo, or a response with no
        ``positionSide`` at all, raises
        ``BinanceFuturesDemoPositionSideMismatch``. 🔴 A close order's side is
        never derived from the sign of a quantity (contract v2 §4.3).
        """
        self._validate_order_args(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            time_in_force=time_in_force,
            position_side=position_side,
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
                position_side=position_side,
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
            position_side=position_side,
        )
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.post(_ORDER_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        echoed_position_side = self._verify_position_side_echo(
            requested=position_side,
            body=body,
            context=f"submit_order symbol={symbol!r} client_order_id={cid!r}",
        )
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
            position_side=echoed_position_side,
        )

    @staticmethod
    def _verify_position_side_echo(
        *,
        requested: str | None,
        body: Any,
        context: str,
    ) -> str | None:
        """Compare the broker's ``positionSide`` echo against the request.

        ROB-1288 / contract v2 §4.3, mirroring the ``set_leverage`` echo
        check. Returns the value to record on the result DTO:

          * ``requested is None`` — nothing was asked for, so nothing is
            verified; whatever Binance sent (possibly nothing) is preserved
            verbatim. This is what keeps the change additive for the callers
            that predate ROB-1288.
          * ``requested`` set, echo equal — returns the echoed value.
          * ``requested`` set, echo different — raises.
          * ``requested`` set, echo **absent** — raises. 🔴 This is the case
            worth being explicit about: "the broker didn't say" must not
            collapse into "the broker agreed". With nothing echoed there is
            no evidence, and the only way to produce a value would be to
            infer one, which is precisely what is forbidden.
        """
        echoed = _extract_position_side(body)
        if requested is None:
            return echoed
        if echoed is None:
            raise BinanceFuturesDemoPositionSideMismatch(
                f"{context}: requested positionSide={requested!r} but the "
                "Binance response carried no positionSide field. Refusing to "
                "treat an absent echo as agreement, and refusing to infer the "
                "side from quantity/side (contract v2 §4.3)."
            )
        if echoed != requested:
            raise BinanceFuturesDemoPositionSideMismatch(
                f"{context}: positionSide echo mismatch — requested "
                f"{requested!r}, Binance echoed {echoed!r}. Refusing to "
                "proceed."
            )
        return echoed

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
        position_side: str | None = None,
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
            position_side=position_side,
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
            position_side=position_side,
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

    async def get_all_open_orders(self) -> FuturesDemoOpenOrdersResult:
        """Query open orders across ALL symbols (read-side; no mutation).

        Signed ``GET /fapi/v1/openOrders`` with no ``symbol`` param —
        Binance returns every open order on the account regardless of
        symbol. ROB-993's account-wide broker-flat gate needs this: a
        single-symbol query cannot see a pre-existing order on a
        *different* symbol left by another consumer of a shared Demo
        account (verify-993-r2-2329.md Finding 2).
        """
        params = {"recvWindow": str(BINANCE_FUTURES_DEMO_RECV_WINDOW_MS)}
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get(_OPEN_ORDERS_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        orders = [
            FuturesDemoOpenOrder(
                client_order_id=str(entry.get("clientOrderId", "")),
                broker_order_id=str(entry.get("orderId", "")),
                symbol=str(entry.get("symbol", "")),
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
            # ROB-1288: preserved verbatim; absent stays absent.
            position_side=_extract_position_side(body),
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
            # ROB-1288: preserved verbatim; 🔴 never derived from the sign of
            # ``position_amt``.
            position_side=_extract_position_side(entry),
        )

    async def get_all_positions(self) -> list[FuturesDemoPositionResult]:
        """Query positions across ALL symbols from ``/fapi/v2/positionRisk``
        (no ``symbol`` param).

        Binance returns one row per symbol the account has ever touched
        (most with ``positionAmt=0``); every row is surfaced verbatim,
        including flat ones — filtering is the caller's job. ROB-993's
        account-wide broker-flat gate needs this: a single-symbol
        ``get_position`` cannot see a pre-existing position on a
        *different* symbol left by another consumer of a shared Demo
        account (verify-993-r2-2329.md Finding 2).
        """
        params = {"recvWindow": str(BINANCE_FUTURES_DEMO_RECV_WINDOW_MS)}
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get(_POSITION_RISK_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        rows = (
            body if isinstance(body, list) else [body] if isinstance(body, dict) else []
        )
        results: list[FuturesDemoPositionResult] = []
        for entry in rows:
            position_amt = Decimal(str(entry.get("positionAmt", "0")))
            entry_price = Decimal(str(entry.get("entryPrice", "0")))
            leverage_raw = entry.get("leverage", "0")
            try:
                leverage = int(Decimal(str(leverage_raw)))
            except (ValueError, ArithmeticError):
                leverage = 0
            results.append(
                FuturesDemoPositionResult(
                    symbol=str(entry.get("symbol", "")),
                    position_amt=position_amt,
                    entry_price=entry_price,
                    leverage=leverage,
                    is_flat=(position_amt == 0),
                    # ROB-1288: the account-wide readback preserves each row's
                    # positionSide. 🔴 A row without one stays ``None`` — the
                    # signed ``position_amt`` sitting right there is not a
                    # licence to infer (contract v2 §4.3).
                    position_side=_extract_position_side(entry),
                )
            )
        return results

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
