"""D2 one-shot remediation writer — ``d2_remediation_single``.

``operator_contract.yaml`` names this writer by hand::

    binance_demo_remediation_20260820:
      surfaces:      [binance_spot_demo_remediation_only]
      writer:        d2_remediation_single
      allowed_operations:
        - BTCUSDT_sell_limit_bound_to_sealed_snapshot
        - ETHUSDT_sell_limit_bound_to_sealed_snapshot
        - USDCUSDT_sell_limit_conversion

Naming a writer does not create one, and until this module existed there was
nothing to run: the D2 execution attempt of 2026-08-20 stopped at
``D2_DUAL_ATTESTATION=FAIL`` because a static search for the named writer
returned no match.  This module is that named writer and nothing else.

**It is a second approved entry point, not a bypass.**
``scripts/binance_spot_demo_smoke.py`` is the ROB-298 BUY-round-trip smoke
path and stays exactly what it is; it does not wire a SELL ``--confirm`` and
therefore cannot express the three authorized operations.  Reaching around it
by calling :meth:`BinanceSpotDemoExecutionClient.submit_order` from an ad-hoc
script would have created an *unreviewed* execution surface.  A reviewed,
narrower one is the correct answer, so this module exists and both CLIs name
each other.

Every ROB-298 safety boundary is inherited unchanged and none is widened:

* the transport still pins ``demo-api.binance.com`` (this module re-asserts
  the host through :func:`assert_spot_demo_host` before it composes anything);
* ``submit_order(..., confirm=True)`` remains the only route to a signed POST,
  and this module's own default is likewise ``confirm=False``;
* ``BINANCE_SPOT_DEMO_ENABLED`` still gates client construction, and this
  module adds a *second*, independent, default-off gate
  (:data:`D2_REMEDIATION_ENABLED_ENV`) on top of it.

What this module adds on top of those inherited gates:

1. **A closed order set.**  :data:`D2_BOUND_ORDERS` is exactly three
   ``SELL``/``LIMIT`` tuples read out of the sealed r7 attempt-2 payload.  The
   writer never accepts an order from a caller: it accepts the *sealed
   payload*, re-derives the set, and refuses unless the derived set is ``==``
   the frozen constant.  A different symbol, side, order type, quantity, price,
   a fourth order, or a different ``pre_snapshot_hash`` all fail closed here,
   before any lease, DB, or socket work.
2. **A J3A lease precondition.**  No broker call is reachable without a live,
   re-attested :class:`PostgresAdvisoryKeysetLease` over the Binance Demo
   physical-account advisory key.  Ownership is re-proved immediately before
   each submit, never assumed from acquisition time.
3. **No blind retry.**  One dispatch per ``client_order_id``, enforced by a
   claim set rather than by the absence of a retry loop.  An ambiguous submit
   is resolved by *readback* (``GET /api/v3/order``), and an unresolved outcome
   stops the whole run instead of re-sending.
4. **Ledger evidence through the service.**  Every lifecycle write goes through
   :class:`BinanceDemoLedgerService`; the repository is never touched.
5. **Two fresh proof epochs.**  Post-dispatch state is proved twice from
   independent bounded observations, collected while the lease is still held.

Deliberate absences: no scheduler registration, no retry queue, no MARKET
path, no cancel path, no quantity/price arithmetic (the sealed floor values are
used verbatim), and no way to widen the order set at runtime.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from enum import StrEnum
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.spot_demo.dto import (
    SpotDemoOrderSubmitResult,
    SpotDemoOrderTestResult,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
    SpotDemoDryRunResult,
)
from app.services.brokers.binance.spot_demo.host_allowlist import (
    assert_spot_demo_host,
)
from app.services.mock_integration.coordination import (
    AdvisoryLeaseGrant,
    LockAuthorityConnection,
    PhysicalAccountScope,
    PostgresAdvisoryKeysetLease,
    SqlAlchemyLockAuthority,
    acquire_physical_account_lease,
    physical_account_scope_for_entry,
)
from app.services.mock_lane_registry import get_lane_registry_entry

# --------------------------------------------------------------------------
# Identity — the sealed object this writer is bound to
# --------------------------------------------------------------------------

#: ``operator_contract.yaml`` names this exact string.  Changing it silently
#: detaches the code from the authority that permits it to run.
WRITER_NAME: Final[str] = "d2_remediation_single"

#: ``operator_contract.yaml: strategy_order_exceptions``.
D2_EXCEPTION_ID: Final[str] = "binance-demo-remediation-20260820"

#: ``binding-payload-proposed.json: remediation_id``.
D2_REMEDIATION_ID: Final[str] = "d2-binance-demo-both-remediation-v2.1-20260818"

#: r7 attempt-2 — the only snapshot this writer will act under.  A payload
#: carrying any other hash is refused; there is no override argument.
D2_PRE_SNAPSHOT_HASH: Final[str] = (
    "sha256:5ba70a814654513c745e45c49206f11cf5f5d061478c668f86602af493e6b898"
)

#: ``r7-snapshot/attempt-2-authoritative.json`` selects attempt-2 and binds
#: these two digests to it.  Carried for evidence; the hash above is the gate.
D2_SNAPSHOT_SEAL_SHA256: Final[str] = (
    "d4c9a13a148f6c6c0d4451264da275826084f0b27c7a69a7698fb8c2b7952ed9"
)
D2_BINDING_PAYLOAD_SHA256: Final[str] = (
    "e1c2d250d73ae3bdb631289a7293c35c217b9e5c6e2694d3f8ea572d1835a3aa"
)

#: The J2A lane whose ``physical_account_id`` names the shared Binance Demo
#: account.  Spot, sidecar, and futures all derive the same advisory key, which
#: is exactly why one lease is enough for the whole account.
D2_LANE_ID: Final[str] = "crypto.binance.spot_demo.canonical"

D2_PRODUCT: Final[str] = "spot"
D2_PRODUCT_DOMAIN: Final[str] = "both"
D2_VENUE: Final[str] = "binance"
D2_VENUE_HOST: Final[str] = "demo-api.binance.com"
D2_QUOTE_ASSET: Final[str] = "USDT"

#: Second, independent env gate.  ``BINANCE_SPOT_DEMO_ENABLED`` still has to be
#: true for the client to construct at all; this one is additional and also
#: defaults to off.  Neither is relaxed by the other.
D2_REMEDIATION_ENABLED_ENV: Final[str] = "D2_REMEDIATION_SINGLE_ENABLED"

#: D6 leaves no order-type or time-in-force choice: LIMIT/GTC, explicit
#: quantity, exposure-reducing only.
D2_ORDER_TYPE: Final[str] = "LIMIT"
D2_SIDE: Final[str] = "SELL"
D2_TIME_IN_FORCE: Final[str] = "GTC"

#: Binance client-order-id prefix for this one-shot.  Kept short so the
#: ``<prefix>-<symbol>-<12 hex>`` form stays inside the 36-char constraint the
#: execution client asserts.
D2_CLIENT_ORDER_ID_PREFIX: Final[str] = "d2rem"

#: The sealed payload marks the three actionable rows with this disposition.
#: Dust (SOL/XRP/DOGE) and quote cash (USDT) carry different ones and are
#: therefore never picked up as orders.
_SEALED_ACTIONABLE_DISPOSITION: Final[str] = "REVIEWED_SCOPE_LIMIT_CANDIDATE"


class D2ReasonCode(StrEnum):
    """The complete refusal vocabulary; no free-form alternatives."""

    DISABLED = "d2_remediation_disabled"
    SEAL_HASH_MISMATCH = "d2_seal_hash_mismatch"
    SEAL_IDENTITY_MISMATCH = "d2_seal_identity_mismatch"
    SEAL_ORDER_SET_MISMATCH = "d2_seal_order_set_mismatch"
    SEAL_MALFORMED = "d2_seal_malformed"
    UNAUTHORIZED_OPERATION = "d2_unauthorized_operation"
    LEASE_NOT_HELD = "d2_lease_not_held"
    LEASE_SCOPE_MISMATCH = "d2_lease_scope_mismatch"
    BLIND_RETRY_REFUSED = "d2_blind_retry_refused"
    BROKER_ECHO_MISMATCH = "d2_broker_echo_mismatch"
    OUTCOME_UNKNOWN = "d2_outcome_unknown"
    HOST_NOT_SPOT_DEMO = "d2_host_not_spot_demo"
    PROOF_EPOCHS_INCOMPLETE = "d2_proof_epochs_incomplete"


class D2RemediationError(RuntimeError):
    """Base class carrying a machine-readable reason code."""

    def __init__(self, reason_code: D2ReasonCode, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {detail}" if detail else str(reason_code))


class D2RemediationDisabled(D2RemediationError):
    """The dedicated env gate is not armed."""


class D2SealBindingMismatch(D2RemediationError):
    """The supplied sealed payload is not the one this writer is bound to."""


class D2UnauthorizedOperation(D2RemediationError):
    """An operation outside the three authorized ones was requested."""


class D2LeaseNotHeld(D2RemediationError):
    """No live, attested J3A lease over the Binance Demo physical account."""


class D2BlindRetryRefused(D2RemediationError):
    """A second dispatch of one client_order_id was attempted."""


class D2OutcomeUnknown(D2RemediationError):
    """A dispatch outcome could not be established by readback."""


# --------------------------------------------------------------------------
# The closed order set
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class D2BoundOrder:
    """One of exactly three authorized operations.

    Frozen and slotted so equality is structural: the seal check below is a
    plain ``==`` against :data:`D2_BOUND_ORDERS`, not a name or substring
    match.  ``Decimal`` comparison is by value, so ``0.00015`` and
    ``0.00015000`` are the same order while ``0.00016`` is not.
    """

    symbol: str
    asset: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal
    time_in_force: str

    def request_params(self) -> dict[str, str]:
        """The exact broker request fields, as strings, for evidence output."""

        return {
            "symbol": self.symbol,
            "side": self.side,
            "type": self.order_type,
            "quantity": format(self.quantity, "f"),
            "price": format(self.price, "f"),
            "timeInForce": self.time_in_force,
        }


#: The three operations, in dispatch order.  Read from r7 attempt-2's
#: ``binding-payload-proposed.json``:
#:
#:   BTCUSDT  SELL LIMIT 0.00015000 @ 69266.01000000
#:   ETHUSDT  SELL LIMIT 0.00520000 @  2248.56000000
#:   USDCUSDT SELL LIMIT 5000.00000000 @ 1.00072000
#:
#: This tuple is the authority the runtime checks against.  The payload is
#: re-read at runtime so a doctored file is caught, but the payload can only
#: ever *match* this constant — it can never extend it.
D2_BOUND_ORDERS: Final[tuple[D2BoundOrder, ...]] = (
    D2BoundOrder(
        symbol="BTCUSDT",
        asset="BTC",
        side=D2_SIDE,
        order_type=D2_ORDER_TYPE,
        quantity=Decimal("0.00015000"),
        price=Decimal("69266.01000000"),
        time_in_force=D2_TIME_IN_FORCE,
    ),
    D2BoundOrder(
        symbol="ETHUSDT",
        asset="ETH",
        side=D2_SIDE,
        order_type=D2_ORDER_TYPE,
        quantity=Decimal("0.00520000"),
        price=Decimal("2248.56000000"),
        time_in_force=D2_TIME_IN_FORCE,
    ),
    D2BoundOrder(
        symbol="USDCUSDT",
        asset="USDC",
        side=D2_SIDE,
        order_type=D2_ORDER_TYPE,
        quantity=Decimal("5000.00000000"),
        price=Decimal("1.00072000"),
        time_in_force=D2_TIME_IN_FORCE,
    ),
)

#: ``allowed_operations`` from ``operator_contract.yaml``, in the same order as
#: :data:`D2_BOUND_ORDERS`.  Kept as a separate constant so the contract's own
#: wording is checkable against the code rather than only readable next to it.
D2_ALLOWED_OPERATION_IDS: Final[tuple[str, ...]] = (
    "BTCUSDT_sell_limit_bound_to_sealed_snapshot",
    "ETHUSDT_sell_limit_bound_to_sealed_snapshot",
    "USDCUSDT_sell_limit_conversion",
)


def _assert_closed_order_set() -> None:
    """Import-time invariants over the constant itself.

    A future edit that adds a fourth row, flips a side, or introduces MARKET
    fails here rather than at a broker.
    """

    if len(D2_BOUND_ORDERS) != 3:
        raise D2UnauthorizedOperation(
            D2ReasonCode.UNAUTHORIZED_OPERATION,
            f"the D2 exception authorizes exactly 3 operations, "
            f"got {len(D2_BOUND_ORDERS)}",
        )
    if len(D2_ALLOWED_OPERATION_IDS) != len(D2_BOUND_ORDERS):
        raise D2UnauthorizedOperation(
            D2ReasonCode.UNAUTHORIZED_OPERATION,
            "allowed_operations and bound orders disagree in length",
        )
    symbols = tuple(order.symbol for order in D2_BOUND_ORDERS)
    if len(set(symbols)) != len(symbols):
        raise D2UnauthorizedOperation(
            D2ReasonCode.UNAUTHORIZED_OPERATION, "duplicate symbol in bound order set"
        )
    for order, operation_id in zip(
        D2_BOUND_ORDERS, D2_ALLOWED_OPERATION_IDS, strict=True
    ):
        if order.side != D2_SIDE or order.order_type != D2_ORDER_TYPE:
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                f"{order.symbol}: only {D2_SIDE}/{D2_ORDER_TYPE} is authorized",
            )
        if order.quantity <= 0 or order.price <= 0:
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                f"{order.symbol}: non-positive quantity or price",
            )
        if not operation_id.startswith(f"{order.symbol}_"):
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                f"allowed_operation {operation_id!r} does not name {order.symbol}",
            )


_assert_closed_order_set()


# --------------------------------------------------------------------------
# Seal binding
# --------------------------------------------------------------------------


def _decimal(value: Any, *, what: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (DecimalException, TypeError, ValueError) as exc:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, f"{what}: not a decimal ({value!r})"
        ) from exc


def _sealed_order(symbol: str, entry: Any) -> D2BoundOrder | None:
    """Map one sealed ``authorized_symbols.spot`` row onto a bound order.

    Returns ``None`` for rows that are not actionable (dust attestations and
    the USDT quote-cash row), so those can never become orders by accident.
    Anything actionable but malformed raises rather than being skipped — a
    silently dropped row would shrink the set without saying so.
    """

    if not isinstance(entry, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, f"{symbol}: row is not a mapping"
        )
    if entry.get("disposition") != _SEALED_ACTIONABLE_DISPOSITION:
        return None
    step = entry.get("proposed_one_step")
    if not isinstance(step, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, f"{symbol}: proposed_one_step missing"
        )
    time_in_force = step.get("time_in_force")
    if time_in_force is not None and time_in_force != D2_TIME_IN_FORCE:
        # The sealed payload leaves TIF null (it sealed a price/quantity, not a
        # wire format).  Null is accepted and D6's GTC is supplied here; any
        # *other* explicit value is a disagreement, not a default.
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_ORDER_SET_MISMATCH,
            f"{symbol}: sealed time_in_force {time_in_force!r} is not "
            f"{D2_TIME_IN_FORCE!r}",
        )
    return D2BoundOrder(
        symbol=str(symbol),
        asset=str(entry.get("asset", "")),
        side=str(step.get("side", "")),
        order_type=str(step.get("order_type", "")),
        quantity=_decimal(step.get("proposed_quantity_floor"), what=f"{symbol} qty"),
        price=_decimal(step.get("proposed_limit_price_floor"), what=f"{symbol} price"),
        time_in_force=D2_TIME_IN_FORCE,
    )


def bind_sealed_orders(payload: Mapping[str, Any]) -> tuple[D2BoundOrder, ...]:
    """Re-derive the order set from a sealed payload and prove it is *the* one.

    The return value is :data:`D2_BOUND_ORDERS` itself, never the parsed
    tuple.  That is deliberate: a parsing bug can then only ever cause a
    refusal, never a widened order set.
    """

    if not isinstance(payload, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, "sealed payload is not a mapping"
        )
    if payload.get("pre_snapshot_hash") != D2_PRE_SNAPSHOT_HASH:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_HASH_MISMATCH,
            f"expected pre_snapshot_hash={D2_PRE_SNAPSHOT_HASH!r}, "
            f"got {payload.get('pre_snapshot_hash')!r}",
        )
    if payload.get("remediation_id") != D2_REMEDIATION_ID:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_IDENTITY_MISMATCH,
            f"expected remediation_id={D2_REMEDIATION_ID!r}, "
            f"got {payload.get('remediation_id')!r}",
        )
    if payload.get("product_domain") != D2_PRODUCT_DOMAIN:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_IDENTITY_MISMATCH,
            f"expected product_domain={D2_PRODUCT_DOMAIN!r}, "
            f"got {payload.get('product_domain')!r}",
        )

    authorized = payload.get("authorized_symbols")
    if not isinstance(authorized, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, "authorized_symbols missing"
        )
    spot = authorized.get("spot")
    if not isinstance(spot, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, "authorized_symbols.spot missing"
        )

    derived: list[D2BoundOrder] = []
    for symbol, entry in spot.items():
        if not isinstance(symbol, str) or not isinstance(entry, Mapping):
            continue
        order = _sealed_order(symbol, entry)
        if order is not None:
            derived.append(order)

    expected_by_symbol = {order.symbol: order for order in D2_BOUND_ORDERS}
    derived_by_symbol = {order.symbol: order for order in derived}
    if derived_by_symbol != expected_by_symbol:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_ORDER_SET_MISMATCH,
            "sealed actionable set is not the bound set: "
            f"expected {_describe(D2_BOUND_ORDERS)}, got {_describe(tuple(derived))}",
        )
    return D2_BOUND_ORDERS


def _describe(orders: Sequence[D2BoundOrder]) -> str:
    return (
        "["
        + ", ".join(
            f"{o.symbol} {o.side} {o.order_type} {format(o.quantity, 'f')}"
            f"@{format(o.price, 'f')}"
            for o in sorted(orders, key=lambda o: o.symbol)
        )
        + "]"
    )


# --------------------------------------------------------------------------
# Lease
# --------------------------------------------------------------------------


def d2_physical_account_scope() -> PhysicalAccountScope:
    """The coordination scope of the shared Binance Demo physical account.

    Derived from the signed J2A registry entry — there is no caller-supplied
    scope string anywhere on this path.
    """

    return physical_account_scope_for_entry(get_lane_registry_entry(D2_LANE_ID))


def d2_advisory_keyset() -> tuple[int, ...]:
    """The single advisory key one D2 lease must hold."""

    return (d2_physical_account_scope().advisory_key,)


async def acquire_d2_lease(*, engine: AsyncEngine) -> PostgresAdvisoryKeysetLease:
    """Take the account-wide J3A lease this writer refuses to run without.

    One key, because spot, sidecar, and futures all derive the same
    physical-account scope: the Binance Demo credentials name one account, so
    one lease covers the whole of it.

    The usual J3A caveat applies and is not softened here — the lease
    coordinates processes *inside this repository*. Binance never sees it, so
    an operator at a console or an out-of-repo process reaches the same account
    without contending for it. It is why the writer also re-attests before each
    submit and reads the account-wide open-order book in both proof epochs.
    """

    async def _factory() -> LockAuthorityConnection:
        connection = await engine.connect()
        return SqlAlchemyLockAuthority(
            connection, observer_factory=lambda: engine.connect()
        )

    return await acquire_physical_account_lease(
        keys=d2_advisory_keyset(), connection_factory=_factory
    )


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class D2PlannedOperation:
    """One composed request, before anything is dispatched."""

    operation_id: str
    client_order_id: str
    order: D2BoundOrder
    request_params: dict[str, str]


@dataclass(frozen=True, slots=True)
class D2DryRunReport:
    """Full-path rehearsal that stops immediately before the signed POST."""

    remediation_id: str
    pre_snapshot_hash: str
    writer: str
    venue_host: str
    lease_attested: bool
    operations: tuple[D2PlannedOperation, ...]
    order_test_results: tuple[SpotDemoOrderTestResult, ...] = ()
    broker_mutation_count: int = 0

    def as_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": "d2-remediation-single-dry-run.v1",
            "writer": self.writer,
            "remediation_id": self.remediation_id,
            "pre_snapshot_hash": self.pre_snapshot_hash,
            "venue_host": self.venue_host,
            "lease_attested": self.lease_attested,
            "broker_mutation_count": self.broker_mutation_count,
            "order_test_count": len(self.order_test_results),
            "operations": [
                {
                    "operation_id": op.operation_id,
                    "client_order_id": op.client_order_id,
                    "request_params": op.request_params,
                }
                for op in self.operations
            ],
        }


@dataclass(frozen=True, slots=True)
class D2ProofEpoch:
    """One bounded post-dispatch observation.

    Two of these, collected independently while the lease is still held,
    are the contract's "dual independent proof".
    """

    epoch_index: int
    observed_at_utc: str
    open_orders: tuple[dict[str, str], ...]
    balances: tuple[dict[str, str], ...]
    ledger_states: dict[str, str]

    def as_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": "d2-remediation-single-proof-epoch.v1",
            "epoch_index": self.epoch_index,
            "observed_at_utc": self.observed_at_utc,
            "account_wide_open_orders": list(self.open_orders),
            "balances": list(self.balances),
            "ledger_states": dict(self.ledger_states),
        }


@dataclass(frozen=True, slots=True)
class D2DispatchOutcome:
    """What happened to one authorized operation."""

    operation_id: str
    client_order_id: str
    order: D2BoundOrder
    request_params: dict[str, str]
    status: str
    broker_order_id: str | None = None
    ledger_state: str = ""
    readback_used: bool = False
    anomaly_reason: str | None = None


@dataclass(frozen=True, slots=True)
class D2ExecutionReport:
    remediation_id: str
    pre_snapshot_hash: str
    writer: str
    outcomes: tuple[D2DispatchOutcome, ...]
    proof_epochs: tuple[D2ProofEpoch, ...]
    broker_submit_count: int
    halted_reason: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": "d2-remediation-single-execution.v1",
            "writer": self.writer,
            "remediation_id": self.remediation_id,
            "pre_snapshot_hash": self.pre_snapshot_hash,
            "broker_submit_count": self.broker_submit_count,
            "halted_reason": self.halted_reason,
            "outcomes": [
                {
                    "operation_id": o.operation_id,
                    "client_order_id": o.client_order_id,
                    "request_params": o.request_params,
                    "status": o.status,
                    "broker_order_id": o.broker_order_id,
                    "ledger_state": o.ledger_state,
                    "readback_used": o.readback_used,
                    "anomaly_reason": o.anomaly_reason,
                }
                for o in self.outcomes
            ],
            "proof_epochs": [epoch.as_evidence() for epoch in self.proof_epochs],
        }


class _ClientOrderIdFactory(Protocol):
    def __call__(self, *, symbol: str) -> str: ...


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def d2_remediation_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """The dedicated, additional, default-off gate for this writer."""

    env = os.environ if environ is None else environ
    return _truthy(env.get(D2_REMEDIATION_ENABLED_ENV))


# --------------------------------------------------------------------------
# The writer
# --------------------------------------------------------------------------


@dataclass
class _DispatchClaims:
    """One dispatch per client_order_id — the structural no-retry guard.

    This is a claim set rather than "we happened not to write a retry loop":
    a second dispatch of the same id raises before the transport is reached,
    so a future edit that adds a loop still cannot double-send.
    """

    claimed: set[str] = field(default_factory=set)

    def claim(self, client_order_id: str) -> None:
        if client_order_id in self.claimed:
            raise D2BlindRetryRefused(
                D2ReasonCode.BLIND_RETRY_REFUSED,
                f"client_order_id={client_order_id!r} was already dispatched; "
                "an ambiguous outcome is resolved by readback, never by "
                "re-sending",
            )
        self.claimed.add(client_order_id)


class D2RemediationSingleWriter:
    """The ``d2_remediation_single`` writer.

    Construction binds the sealed payload; nothing else can supply orders.
    ``execute`` defaults to ``confirm=False`` and dispatches zero mutations.
    """

    writer_name: Final[str] = WRITER_NAME

    def __init__(
        self,
        *,
        execution_client: BinanceSpotDemoExecutionClient,
        sealed_payload: Mapping[str, Any],
        lease: PostgresAdvisoryKeysetLease,
        lease_grant: AdvisoryLeaseGrant,
        ledger: BinanceDemoLedgerService | None = None,
        now_fn: Callable[[], dt.datetime] | None = None,
        client_order_id_factory: _ClientOrderIdFactory | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if not d2_remediation_enabled(environ):
            raise D2RemediationDisabled(
                D2ReasonCode.DISABLED,
                f"{D2_REMEDIATION_ENABLED_ENV} is not truthy; this one-shot "
                "writer is default-disabled independently of "
                "BINANCE_SPOT_DEMO_ENABLED",
            )
        # Re-assert the host here even though the transport already pins it:
        # this module composes signed order params, so it proves for itself
        # that they are going to the Spot Demo host.
        self._assert_spot_demo_endpoint(execution_client)
        # The orders come from the seal, never from a caller argument. The
        # bound tuple is the module constant regardless of what parsed.
        self._orders = bind_sealed_orders(sealed_payload)
        if self._orders is not D2_BOUND_ORDERS:  # pragma: no cover - defensive
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                "bound order set is not the frozen constant",
            )
        self._client = execution_client
        self._ledger = ledger
        self._lease = lease
        self._lease_grant = lease_grant
        self._now_fn = now_fn or (lambda: dt.datetime.now(dt.UTC))
        self._client_order_id_factory = (
            client_order_id_factory or _default_client_order_id
        )
        self._claims = _DispatchClaims()
        self._submit_count = 0

    # -- preconditions -------------------------------------------------

    @staticmethod
    def _assert_spot_demo_endpoint(
        execution_client: BinanceSpotDemoExecutionClient,
    ) -> None:
        base_url = getattr(execution_client, "_base_url", "")
        host = urlsplit(str(base_url)).hostname or ""
        try:
            assert_spot_demo_host(host)
        except Exception as exc:
            raise D2UnauthorizedOperation(
                D2ReasonCode.HOST_NOT_SPOT_DEMO,
                f"execution client base_url host {host!r} is not a Spot Demo host",
            ) from exc

    async def _require_lease(self) -> None:
        """Re-prove the lease immediately before every broker mutation.

        Acquisition-time success is not carried forward: a transparently
        reconnected session owns nothing, and ``assert_owned`` is what
        notices.
        """

        lease = self._lease
        if lease is None or lease.released:
            raise D2LeaseNotHeld(
                D2ReasonCode.LEASE_NOT_HELD,
                "no live J3A physical-account lease; the D2 order path is "
                "unreachable without one",
            )
        expected_keys = d2_advisory_keyset()
        if tuple(self._lease_grant.keys) != expected_keys:
            raise D2LeaseNotHeld(
                D2ReasonCode.LEASE_SCOPE_MISMATCH,
                f"lease grant covers {self._lease_grant.keys!r}, but the "
                f"Binance Demo physical account needs {expected_keys!r}",
            )
        try:
            await lease.assert_owned(self._lease_grant)
        except D2RemediationError:  # pragma: no cover - defensive
            raise
        except Exception as exc:
            raise D2LeaseNotHeld(
                D2ReasonCode.LEASE_NOT_HELD,
                f"lease ownership could not be re-attested: {exc}",
            ) from exc

    # -- planning ------------------------------------------------------

    def plan(self) -> tuple[D2PlannedOperation, ...]:
        """Compose the three requests. Pure — no lease, no I/O, no signing."""

        return tuple(
            D2PlannedOperation(
                operation_id=operation_id,
                client_order_id=self._client_order_id_factory(symbol=order.symbol),
                order=order,
                request_params=order.request_params(),
            )
            for order, operation_id in zip(
                self._orders, D2_ALLOWED_OPERATION_IDS, strict=True
            )
        )

    # -- execution -----------------------------------------------------

    async def execute(
        self,
        *,
        confirm: bool = False,
        include_order_test: bool = True,
    ) -> D2DryRunReport | D2ExecutionReport:
        """Run the writer. ``confirm=False`` (the default) mutates nothing.

        The dry run walks the *whole* path — env gate, host re-assertion, seal
        binding, lease attestation, request composition, and (optionally) the
        non-mutating ``POST /api/v3/order/test`` shape check — and stops there.
        """

        await self._require_lease()
        operations = self.plan()
        if not confirm:
            return await self._dry_run(
                operations, include_order_test=include_order_test
            )
        return await self._confirmed_run(
            operations, include_order_test=include_order_test
        )

    async def _dry_run(
        self,
        operations: tuple[D2PlannedOperation, ...],
        *,
        include_order_test: bool,
    ) -> D2DryRunReport:
        tests: list[SpotDemoOrderTestResult] = []
        if include_order_test:
            for op in operations:
                tests.append(
                    await self._client.order_test(
                        symbol=op.order.symbol,
                        side=op.order.side,
                        order_type=op.order.order_type,
                        qty=op.order.quantity,
                        price=op.order.price,
                        time_in_force=op.order.time_in_force,
                    )
                )
        return D2DryRunReport(
            remediation_id=D2_REMEDIATION_ID,
            pre_snapshot_hash=D2_PRE_SNAPSHOT_HASH,
            writer=WRITER_NAME,
            venue_host=D2_VENUE_HOST,
            lease_attested=True,
            operations=operations,
            order_test_results=tuple(tests),
            broker_mutation_count=0,
        )

    async def _confirmed_run(
        self,
        operations: tuple[D2PlannedOperation, ...],
        *,
        include_order_test: bool,
    ) -> D2ExecutionReport:
        outcomes: list[D2DispatchOutcome] = []
        halted_reason: str | None = None
        for op in operations:
            outcome = await self._dispatch_one(
                op, include_order_test=include_order_test
            )
            outcomes.append(outcome)
            if outcome.status in {"anomaly", "unknown"}:
                # An unestablished outcome stops the run. Continuing would mean
                # mutating an account whose current state is not known.
                halted_reason = (
                    f"{op.operation_id}: {outcome.anomaly_reason or outcome.status}"
                )
                break
        epochs = await self._collect_proof_epochs(outcomes)
        return D2ExecutionReport(
            remediation_id=D2_REMEDIATION_ID,
            pre_snapshot_hash=D2_PRE_SNAPSHOT_HASH,
            writer=WRITER_NAME,
            outcomes=tuple(outcomes),
            proof_epochs=epochs,
            broker_submit_count=self._submit_count,
            halted_reason=halted_reason,
        )

    async def _dispatch_one(
        self, op: D2PlannedOperation, *, include_order_test: bool
    ) -> D2DispatchOutcome:
        order = op.order
        instrument_id = await self._resolve_instrument(order)
        await self._ledger_planned(op, instrument_id)
        await self._ledger_transition("previewed", op)
        if include_order_test:
            await self._client.order_test(
                symbol=order.symbol,
                side=order.side,
                order_type=order.order_type,
                qty=order.quantity,
                price=order.price,
                time_in_force=order.time_in_force,
            )
        await self._ledger_transition("validated", op)

        # Re-prove the lease immediately before the signed POST, not once at
        # the top of the run.
        await self._require_lease()
        self._claims.claim(op.client_order_id)
        self._submit_count += 1
        try:
            result = await self._client.submit_order(
                symbol=order.symbol,
                side=order.side,
                order_type=order.order_type,
                qty=order.quantity,
                price=order.price,
                time_in_force=order.time_in_force,
                client_order_id=op.client_order_id,
                confirm=True,
            )
        except Exception as exc:
            return await self._resolve_by_readback(op, submit_error=exc)
        if isinstance(result, SpotDemoDryRunResult):  # pragma: no cover - defensive
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                "submit_order returned a dry-run sentinel under confirm=True",
            )
        self._assert_echo(op, result)
        await self._ledger_transition(
            "submitted", op, broker_order_id=result.broker_order_id
        )
        return D2DispatchOutcome(
            operation_id=op.operation_id,
            client_order_id=op.client_order_id,
            order=order,
            request_params=op.request_params,
            status=str(result.status),
            broker_order_id=result.broker_order_id,
            ledger_state="submitted",
        )

    def _assert_echo(
        self, op: D2PlannedOperation, result: SpotDemoOrderSubmitResult
    ) -> None:
        """The broker must echo back exactly what was authorized.

        A response describing a different symbol, side, type, or quantity is
        not a success with cosmetic drift; it means the account was mutated in
        a way the seal does not cover.
        """

        order = op.order
        mismatches: list[str] = []
        if result.symbol != order.symbol:
            mismatches.append(f"symbol {result.symbol!r} != {order.symbol!r}")
        if result.side != order.side:
            mismatches.append(f"side {result.side!r} != {order.side!r}")
        if result.order_type != order.order_type:
            mismatches.append(f"type {result.order_type!r} != {order.order_type!r}")
        if result.qty != order.quantity:
            mismatches.append(f"qty {result.qty} != {order.quantity}")
        if result.executed_qty > order.quantity:
            mismatches.append(
                f"executedQty {result.executed_qty} exceeds authorized {order.quantity}"
            )
        if mismatches:
            raise D2RemediationError(
                D2ReasonCode.BROKER_ECHO_MISMATCH,
                f"{op.operation_id}: " + "; ".join(mismatches),
            )

    async def _resolve_by_readback(
        self, op: D2PlannedOperation, *, submit_error: BaseException
    ) -> D2DispatchOutcome:
        """Establish an ambiguous dispatch's outcome by *asking*, not re-sending.

        A local failure after a signed POST says nothing about whether the
        order reached Binance. The only safe move is one bounded read of the
        order by its client id. Anything short of a positive answer is an
        anomaly, because "not found" cannot be distinguished from "not yet
        visible" and both are outranked by the cost of a duplicate order.
        """

        try:
            status_body = await self._client.get_order_status(
                symbol=op.order.symbol, client_order_id=op.client_order_id
            )
        except BinanceDemoOrderNotFound:
            reason = (
                "submit_outcome_unknown_order_not_found_after_ambiguous_submit: "
                f"{submit_error!r}"
            )
            await self._ledger_anomaly(op, reason)
            return D2DispatchOutcome(
                operation_id=op.operation_id,
                client_order_id=op.client_order_id,
                order=op.order,
                request_params=op.request_params,
                status="anomaly",
                ledger_state="anomaly",
                readback_used=True,
                anomaly_reason=reason,
            )
        except Exception as readback_error:
            reason = (
                f"submit_outcome_unknown_readback_failed: {readback_error!r} "
                f"(original: {submit_error!r})"
            )
            await self._ledger_anomaly(op, reason)
            return D2DispatchOutcome(
                operation_id=op.operation_id,
                client_order_id=op.client_order_id,
                order=op.order,
                request_params=op.request_params,
                status="anomaly",
                ledger_state="anomaly",
                readback_used=True,
                anomaly_reason=reason,
            )

        broker_order_id = str(status_body.get("orderId", ""))
        echoed = SpotDemoOrderSubmitResult(
            client_order_id=str(status_body.get("clientOrderId", op.client_order_id)),
            broker_order_id=broker_order_id,
            symbol=str(status_body.get("symbol", "")),
            side=str(status_body.get("side", "")),
            order_type=str(status_body.get("type", "")),
            qty=_decimal(status_body.get("origQty", "0"), what="readback origQty"),
            executed_qty=_decimal(
                status_body.get("executedQty", "0"), what="readback executedQty"
            ),
            cummulative_quote_qty=Decimal("0"),
            status=str(status_body.get("status", "UNKNOWN")),
        )
        self._assert_echo(op, echoed)
        await self._ledger_transition("submitted", op, broker_order_id=broker_order_id)
        return D2DispatchOutcome(
            operation_id=op.operation_id,
            client_order_id=op.client_order_id,
            order=op.order,
            request_params=op.request_params,
            status=echoed.status,
            broker_order_id=broker_order_id,
            ledger_state="submitted",
            readback_used=True,
        )

    # -- proof epochs --------------------------------------------------

    async def _collect_proof_epochs(
        self, outcomes: Sequence[D2DispatchOutcome]
    ) -> tuple[D2ProofEpoch, ...]:
        """Two independent bounded observations, lease still held.

        Each epoch issues its own account-wide open-order read and its own
        per-asset balance reads; neither reuses the other's bytes.
        """

        epochs: list[D2ProofEpoch] = []
        for index in (1, 2):
            await self._require_lease()
            epochs.append(await self.collect_proof_epoch(index, outcomes))
        if len(epochs) != 2:  # pragma: no cover - defensive
            raise D2RemediationError(
                D2ReasonCode.PROOF_EPOCHS_INCOMPLETE,
                f"expected 2 proof epochs, got {len(epochs)}",
            )
        return tuple(epochs)

    async def collect_proof_epoch(
        self, epoch_index: int, outcomes: Sequence[D2DispatchOutcome]
    ) -> D2ProofEpoch:
        open_orders = await self._client.get_all_open_orders()
        balances: list[dict[str, str]] = []
        for asset in [order.asset for order in self._orders] + [D2_QUOTE_ASSET]:
            balance = await self._client.get_asset_balance(asset=asset)
            balances.append(
                {
                    "asset": balance.asset,
                    "free": format(balance.free, "f"),
                    "locked": format(balance.locked, "f"),
                }
            )
        ledger_states: dict[str, str] = {}
        if self._ledger is not None:
            for outcome in outcomes:
                row = await self._ledger.get_by_client_order_id(outcome.client_order_id)
                ledger_states[outcome.client_order_id] = (
                    "" if row is None else str(row.lifecycle_state)
                )
        return D2ProofEpoch(
            epoch_index=epoch_index,
            observed_at_utc=self._now_fn().isoformat(),
            open_orders=tuple(
                {
                    "symbol": entry.symbol,
                    "side": entry.side,
                    "client_order_id": entry.client_order_id,
                    "qty": format(entry.qty, "f"),
                    "status": entry.status,
                }
                for entry in open_orders.orders
            ),
            balances=tuple(balances),
            ledger_states=ledger_states,
        )

    # -- ledger (service only; the repository is never touched) ---------

    async def _resolve_instrument(self, order: D2BoundOrder) -> int:
        if self._ledger is None:
            return 0
        return await self._ledger.resolve_or_create_instrument(
            venue=D2_VENUE,
            product=D2_PRODUCT,
            venue_symbol=order.symbol,
            base_asset=order.asset,
            quote_asset=D2_QUOTE_ASSET,
        )

    async def _ledger_planned(self, op: D2PlannedOperation, instrument_id: int) -> None:
        if self._ledger is None:
            return
        await self._ledger.record_planned(
            instrument_id=instrument_id,
            product=D2_PRODUCT,
            venue_host=D2_VENUE_HOST,
            client_order_id=op.client_order_id,
            side=op.order.side,
            order_type=op.order.order_type,
            qty=op.order.quantity,
            price=op.order.price,
            extra_metadata=self._evidence_metadata(op),
            now=self._now_fn(),
        )

    async def _ledger_transition(
        self,
        state: str,
        op: D2PlannedOperation,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        if self._ledger is None:
            return
        now = self._now_fn()
        if state == "previewed":
            await self._ledger.record_previewed(
                client_order_id=op.client_order_id, now=now
            )
        elif state == "validated":
            await self._ledger.record_validated(
                client_order_id=op.client_order_id, now=now
            )
        elif state == "submitted":
            await self._ledger.record_submitted(
                client_order_id=op.client_order_id,
                broker_order_id=broker_order_id or "",
                now=now,
            )
        else:  # pragma: no cover - defensive
            raise D2RemediationError(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                f"unsupported ledger transition {state!r}",
            )

    async def _ledger_anomaly(self, op: D2PlannedOperation, reason: str) -> None:
        if self._ledger is None:
            return
        await self._ledger.record_anomaly(
            client_order_id=op.client_order_id,
            reason=reason,
            now=self._now_fn(),
        )

    def _evidence_metadata(self, op: D2PlannedOperation) -> dict[str, Any]:
        return {
            "writer": WRITER_NAME,
            "d2_exception_id": D2_EXCEPTION_ID,
            "remediation_id": D2_REMEDIATION_ID,
            "pre_snapshot_hash": D2_PRE_SNAPSHOT_HASH,
            "snapshot_seal_sha256": D2_SNAPSHOT_SEAL_SHA256,
            "binding_payload_sha256": D2_BINDING_PAYLOAD_SHA256,
            "operation_id": op.operation_id,
            "canary_or_strategy_use": "forbidden",
        }


def _default_client_order_id(*, symbol: str) -> str:
    """``d2rem-<symbol>-<12 hex>`` — inside the 36-char Binance constraint."""

    import uuid

    return f"{D2_CLIENT_ORDER_ID_PREFIX}-{symbol}-{uuid.uuid4().hex[:12]}"


__all__ = [
    "D2_ALLOWED_OPERATION_IDS",
    "D2_BINDING_PAYLOAD_SHA256",
    "D2_BOUND_ORDERS",
    "D2_CLIENT_ORDER_ID_PREFIX",
    "D2_EXCEPTION_ID",
    "D2_LANE_ID",
    "D2_ORDER_TYPE",
    "D2_PRE_SNAPSHOT_HASH",
    "D2_PRODUCT",
    "D2_QUOTE_ASSET",
    "D2_REMEDIATION_ENABLED_ENV",
    "D2_REMEDIATION_ID",
    "D2_SIDE",
    "D2_SNAPSHOT_SEAL_SHA256",
    "D2_TIME_IN_FORCE",
    "D2_VENUE",
    "D2_VENUE_HOST",
    "D2BoundOrder",
    "D2DispatchOutcome",
    "D2DryRunReport",
    "D2ExecutionReport",
    "D2LeaseNotHeld",
    "D2BlindRetryRefused",
    "D2OutcomeUnknown",
    "D2PlannedOperation",
    "D2ProofEpoch",
    "D2ReasonCode",
    "D2RemediationDisabled",
    "D2RemediationError",
    "D2RemediationSingleWriter",
    "D2SealBindingMismatch",
    "D2UnauthorizedOperation",
    "WRITER_NAME",
    "acquire_d2_lease",
    "bind_sealed_orders",
    "d2_advisory_keyset",
    "d2_physical_account_scope",
    "d2_remediation_enabled",
]
